from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from html import unescape
from html.parser import HTMLParser
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4
from urllib.parse import quote, urlencode, urljoin, urlsplit

import aiohttp
import asyncpg
from minio import Minio
from temporalio import activity
from temporalio.exceptions import ApplicationError

from editorial_core.discovery import canonicalize_url
from editorial_core.research import chunk_semantic_text, scholarly_work_identity
from research_worker.acquisition import (
    DomainPolicy,
    DomainThrottle,
    RobotsDenied,
    acquire_public_source,
    search_searxng,
)
from research_worker.config import Settings
from research_worker.extraction import extract_source_document
from research_worker.firecrawl import render_public_html


_settings = Settings()
_domain_throttle = DomainThrottle(_settings.acquisition_min_domain_interval_seconds)

_PRIMARY_CITATION_PATTERNS = (
    "doi.org/",
    "pubmed.ncbi.nlm.nih.gov/",
    "pmc.ncbi.nlm.nih.gov/articles/",
    "ncbi.nlm.nih.gov/pmc/articles/",
    "clinicaltrials.gov/study/",
    "acpjournals.org/doi/",
    "nature.com/articles/",
    "sciencedirect.com/science/article/",
    "onlinelibrary.wiley.com/doi/",
    "arxiv.org/abs/",
    "openalex.org/works/",
    "api.crossref.org/works/",
    "ebi.ac.uk/europepmc/",
)
_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_PUBMED_PATTERN = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.IGNORECASE)
_PMC_PATTERN = re.compile(r"(?:pmc\.ncbi\.nlm\.nih\.gov/articles/|ncbi\.nlm\.nih\.gov/pmc/articles/)(PMC\d+)", re.IGNORECASE)
_CLINICAL_TRIAL_PATTERN = re.compile(r"clinicaltrials\.gov/study/(NCT\d+)", re.IGNORECASE)
_ARXIV_PATTERN = re.compile(r"arxiv\.org/abs/([\w.\-/]+)", re.IGNORECASE)
_AUTHORITATIVE_HOST_PARTS = (
    ".gov.",
    ".gov",
    ".edu",
    ".ac.",
    ".int",
    ".europa.eu",
    ".bund.de",
    ".admin.ch",
    ".gv.at",
)
_PRIMARY_TITLE_TERMS = (
    "study",
    "studie",
    "report",
    "bericht",
    "dataset",
    "datensatz",
    "statistics",
    "statistik",
    "standard",
    "specification",
    "documentation",
    "dokumentation",
    "law",
    "gesetz",
    "regulation",
    "verordnung",
)
_RELEVANCE_STOP_WORDS = {
    "aber",
    "auch",
    "bei",
    "das",
    "dem",
    "den",
    "der",
    "die",
    "ein",
    "eine",
    "einer",
    "für",
    "herausforderungen",
    "chancen",
    "deutschland",
    "germany",
    "mehr",
    "mit",
    "oder",
    "the",
    "und",
    "von",
    "was",
    "wie",
    "warum",
    "wieso",
    "with",
    "zusammenhang",
    "zusammenhängen",
}
_BOOTSTRAP_QUERY_STOP_WORDS = (
    _RELEVANCE_STOP_WORDS
    - {"deutschland", "germany"}
    | {
        "alltag",
        "alltagsnahen",
        "andererseits",
        "anschaulich",
        "anschauliche",
        "anspruchsvoll",
        "belastbare",
        "bootstrap",
        "diskutiert",
        "dossier",
        "einfach",
        "einfache",
        "einfaches",
        "einerseits",
        "enthält",
        "enthaelt",
        "erklärthemen",
        "erklärvideo",
        "erklärvideos",
        "erklären",
        "erklärt",
        "faktischsimpel",
        "freigabe",
        "funktionieren",
        "funktioniert",
        "gelöste",
        "geloeste",
        "geeignet",
        "handverlesene",
        "heißes",
        "heisses",
        "klaren",
        "liefert",
        "medizinische",
        "möglich",
        "moeglich",
        "niedrig-riskante",
        "öffentlich",
        "politische",
        "pfad",
        "praktisch",
        "quellen",
        "quelle",
        "rechtliche",
        "regional",
        "repräsentativer",
        "script",
        "snapshots",
        "storyboard",
        "technik",
        "testfall",
        "thema",
        "themen",
        "übergehen",
        "visuell",
        "workflow",
        "zeitlose",
        "zunehmend",
    }
)
_SOCIAL_OR_EXPORT_HOSTS = {
    "bibsonomy.org",
    "www.bibsonomy.org",
    "reddit.com",
    "www.reddit.com",
    "facebook.com",
    "www.facebook.com",
    "twitter.com",
    "x.com",
}
_REVIEW_TITLE_TERMS = {"review", "overview", "literature", "vergleich", "meta-analysis"}
_DEFAULT_SOURCE_LINK_LIMIT = 20


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a" or self._href is not None:
            return
        self._href = next(
            (value for name, value in attrs if name.casefold() == "href"), None
        )
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


def _linked_primary_citations(body: bytes, base_url: str) -> tuple[dict[str, str], ...]:
    parser = _AnchorParser()
    parser.feed(body.decode("utf-8", "replace"))
    citations: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, anchor_text in parser.links:
        absolute = unescape(urljoin(base_url, href)).strip()
        parsed = urlsplit(absolute)
        hostname = (parsed.hostname or "").casefold()
        direct_target = f"{hostname}{parsed.path}".casefold()
        if hostname in _SOCIAL_OR_EXPORT_HOSTS or not any(
            pattern in direct_target for pattern in _PRIMARY_CITATION_PATTERNS
        ):
            continue
        try:
            canonical = canonicalize_url(absolute)
        except ValueError:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        citations.append(
            {
                "url": canonical,
                "title": anchor_text[:1000]
                or f"Primary evidence at {urlsplit(canonical).hostname}",
                "source_type": "primary",
                "resolution_strategy": "explicit_primary_link",
            }
        )
    decoded = unescape(body.decode("utf-8", "replace"))
    for match in _DOI_PATTERN.finditer(decoded):
        doi = match.group(0).rstrip(".,;:)]}\"")
        canonical = f"https://doi.org/{doi}"
        if canonical.casefold() in {item.casefold() for item in seen}:
            continue
        seen.add(canonical)
        citations.append(
            {
                "url": canonical,
                "title": f"Primary evidence identified by DOI {doi}",
                "source_type": "primary",
                "resolution_strategy": "embedded_doi",
            }
        )
    return tuple(citations[:12])


def _doi_from_citation_url(url: str) -> str | None:
    lowered = url.casefold()
    marker = "/doi/"
    if "doi.org/" in lowered:
        marker = "doi.org/"
    if marker not in lowered:
        return None
    offset = lowered.index(marker) + len(marker)
    doi = url[offset:].split("?", 1)[0].split("#", 1)[0].strip("/ ")
    return doi if doi.startswith("10.") and "/" in doi else None


def _registry_query_for_citation(citation: dict[str, str]) -> str | None:
    doi = _doi_from_citation_url(citation["url"])
    if doi is not None:
        return f"DOI:{doi}"
    pubmed_match = _PUBMED_PATTERN.search(citation["url"])
    if pubmed_match:
        return f"EXT_ID:{pubmed_match.group(1)}"
    return None


async def _resolve_registry_candidate(
    session: aiohttp.ClientSession,
    citation: dict[str, str],
    *,
    europe_pmc_endpoint: str = "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
    crossref_endpoint: str = "https://api.crossref.org/works",
) -> dict[str, str] | None:
    """Resolve common scholarly identifiers to an acquisition-safe public record.

    Europe PMC is preferred when it indexes the record because its core response
    includes abstracts and study metadata. Crossref is the discipline-neutral DOI
    fallback. Direct citations remain available when neither registry has a record.
    """

    registry_query = _registry_query_for_citation(citation)
    if registry_query is None:
        return None
    query = urlencode({"query": registry_query, "format": "json", "resultType": "core"})
    europe_pmc_url = f"{europe_pmc_endpoint}?{query}"
    try:
        async with session.get(europe_pmc_url) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        payload = {}
    if payload.get("hitCount", 0) or payload.get("resultList", {}).get("result"):
        return {
            "url": europe_pmc_url,
            "title": f"Europe PMC primary-study record: {citation['title']}",
            "source_type": "primary",
            "resolution_strategy": "europe_pmc_registry",
        }

    doi = _doi_from_citation_url(citation["url"])
    if doi is None:
        return None
    crossref_url = f"{crossref_endpoint.rstrip('/')}/{quote(doi, safe='')}"
    try:
        async with session.get(crossref_url) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    if not payload.get("message"):
        return None
    return {
        "url": crossref_url,
        "title": f"Crossref primary-publication record: {citation['title']}",
        "source_type": "primary",
        "resolution_strategy": "crossref_registry",
    }


def _public_api_candidate(citation: dict[str, str]) -> dict[str, str] | None:
    """Map non-DOI registry links to their public machine-readable record."""

    pmc_match = _PMC_PATTERN.search(citation["url"])
    if pmc_match:
        pmcid = pmc_match.group(1).upper()
        return {
            "url": f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
            "title": f"Europe PMC full-text record: {citation['title']}",
            "source_type": "primary",
            "resolution_strategy": "europe_pmc_full_text",
        }
    trial_match = _CLINICAL_TRIAL_PATTERN.search(citation["url"])
    if trial_match:
        nct_id = trial_match.group(1).upper()
        return {
            "url": f"https://clinicaltrials.gov/api/v2/studies/{nct_id}",
            "title": f"ClinicalTrials.gov study record: {citation['title']}",
            "source_type": "primary",
            "resolution_strategy": "clinicaltrials_api",
        }
    arxiv_match = _ARXIV_PATTERN.search(citation["url"])
    if arxiv_match:
        identifier = arxiv_match.group(1)
        return {
            "url": f"https://export.arxiv.org/api/query?{urlencode({'id_list': identifier})}",
            "title": f"arXiv primary-paper record: {citation['title']}",
            "source_type": "primary",
            "resolution_strategy": "arxiv_api",
        }
    return None


def _generic_evidence_queries(title: str, summary: str) -> tuple[str, ...]:
    """Build bounded, language-tolerant evidence and falsification searches."""

    title_terms = [
        term
        for term in re.findall(r"[\wÄÖÜäöüß-]{5,}", title, flags=re.UNICODE)
        if term.casefold() not in _RELEVANCE_STOP_WORDS
    ][:6]
    summary_terms = [
        term
        for term in re.findall(r"[\wÄÖÜäöüß-]{5,}", summary, flags=re.UNICODE)
        if term.casefold() not in _RELEVANCE_STOP_WORDS
        and term.casefold() not in {item.casefold() for item in title_terms}
    ][:4]
    concept = " ".join(title_terms or summary_terms)[:240]
    context = " ".join(summary_terms)[:160]
    return (
        f"{concept} original study report data official source Originalquelle Studie Bericht Daten",
        f"{concept} {context} evidence documentation statistics Beleg Dokumentation Statistik",
        f"{concept} limitations risks correction critique counterevidence Grenzen Risiken Kritik Gegenbeleg",
    )


def _json_list(value: Any) -> list[str]:
    raw = _json(value) or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _append_negative_terms(query: str, negative_keywords: list[str]) -> str:
    terms = [
        f'-"{keyword}"'
        for keyword in negative_keywords[:8]
        if len(keyword) <= 80 and '"' not in keyword
    ]
    suffix = " ".join(terms)
    return f"{query} {suffix}".strip()


def _bootstrap_query_terms(value: str, *, limit: int) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for term in re.findall(r"[\wÄÖÜäöüß-]{5,}", value, flags=re.UNICODE):
        normalized = term.casefold().strip("-")
        if normalized in _BOOTSTRAP_QUERY_STOP_WORDS or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _relevant_seed_queries(
    title: str, summary: str, seed_queries: list[str]
) -> tuple[str, ...]:
    opportunity_terms = _relevance_tokens(f"{title} {summary}")
    relevant: list[str] = []
    for seed in seed_queries:
        if _relevance_tokens(seed) & opportunity_terms:
            relevant.append(seed)
    return tuple(relevant[:4])


def _topic_source_bootstrap_queries(
    *,
    title: str,
    summary: str,
    subject_topic: str,
    research_goal: str,
    seed_queries: list[str],
    related_concepts: list[str],
    negative_keywords: list[str],
) -> tuple[str, ...]:
    """Build topic-specific source searches for curated/manual Opportunities.

    These queries are used before any source snapshot exists. They intentionally
    start from the human-approved Opportunity text, then add subject context so
    hand-picked explainer topics are not forced through live-discovery seeds.
    """

    title = " ".join(title.split())
    summary = " ".join(summary.split())
    title_terms = _bootstrap_query_terms(title, limit=5)
    summary_terms = _bootstrap_query_terms(summary, limit=8)
    topic_terms = _bootstrap_query_terms(subject_topic, limit=4)
    goal_terms = _bootstrap_query_terms(research_goal, limit=4)
    opportunity_terms = _relevance_tokens(f"{title} {summary}")
    matching_related = [
        concept
        for concept in related_concepts
        if _relevance_tokens(concept) & opportunity_terms
    ][:3]
    concept_terms = list(
        dict.fromkeys([*title_terms, *summary_terms, *matching_related])
    )
    fallback_terms = list(dict.fromkeys([*topic_terms, *goal_terms, *summary_terms]))
    concept = " ".join(concept_terms or fallback_terms)[:220]
    focused = " ".join(
        dict.fromkeys([*title_terms[:3], *summary_terms[:5], *matching_related[:2]])
    )[:220]
    queries: list[str] = []
    if concept:
        queries.extend(
            [
                f"{concept} Forschung Bericht Daten Quelle",
                f"{focused or concept} Funktionsweise Erklärung Hintergrund",
                f"{focused or concept} Studie technische Dokumentation",
            ]
        )
    context = " ".join(dict.fromkeys([*topic_terms, *goal_terms, *summary_terms[:3]]))[:220]
    if context and context.casefold() != concept.casefold():
        queries.append(f"{context} vertrauenswürdige Quellen")
    for seed in _relevant_seed_queries(title, summary, seed_queries):
        queries.append(seed)
    return tuple(
        dict.fromkeys(
            _append_negative_terms(query[:280], negative_keywords)
            for query in queries
            if query.strip()
        )
    )[:6]


def _scholarly_work_identity(url: str, *, doi: str | None = None) -> str | None:
    return scholarly_work_identity(url, doi=doi or _doi_from_citation_url(url))


def _openalex_abstract(work: dict[str, Any]) -> str:
    inverted = work.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    positioned = [
        (position, token)
        for token, positions in inverted.items()
        if isinstance(token, str) and isinstance(positions, list)
        for position in positions
        if isinstance(position, int) and position >= 0
    ]
    return " ".join(token for _, token in sorted(positioned))


async def _openalex_search_candidates(
    settings: Settings,
    *,
    title: str,
    summary: str,
    domain_policy: DomainPolicy,
    planned_queries: tuple[str, ...] = (),
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Find distinct scholarly works through a discipline-neutral catalog."""

    default_query = _generic_evidence_queries(title, summary)[0].split(" original study", 1)[0]
    queries = tuple(dict.fromkeys((*planned_queries[:5], default_query)))
    payloads: list[tuple[str, dict[str, Any]]] = []
    errors: list[str] = []
    for query in queries:
        params = {"search": query, "per-page": "12"}
        if settings.openalex_mailto:
            params["mailto"] = settings.openalex_mailto
        endpoint = settings.openalex_endpoint.rstrip("/") + "/works?" + urlencode(params)
        try:
            response = await acquire_public_source(
                endpoint,
                user_agent=settings.acquisition_user_agent,
                policy=domain_policy,
                throttle=_domain_throttle,
                maximum_bytes=min(settings.acquisition_max_bytes, 2_000_000),
            )
            payloads.append((query, json.loads(response.body)))
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, RobotsDenied) as exc:
            errors.append(str(exc)[:300])
    candidates: list[dict[str, str]] = []
    seen_identities: set[str] = set()
    for query, payload in payloads:
      for work in payload.get("results", []):
        if not isinstance(work, dict):
            continue
        work_id = str(work.get("id", "")).rsplit("/", 1)[-1]
        display_name = str(work.get("display_name", "")).strip()
        if not re.fullmatch(r"W\d+", work_id, re.I) or not display_name:
            continue
        abstract = _openalex_abstract(work)
        relevance_score, shared_title_terms = _candidate_relevance(
            title, summary, display_name, abstract
        )
        query_relevance, shared_query_terms = _candidate_relevance(
            query, "", display_name, abstract
        )
        if (
            (shared_title_terms < 1 or relevance_score < 0.10)
            and (shared_query_terms < 2 or query_relevance < 0.12)
        ):
            continue
        doi = str(work.get("doi") or "") or None
        work_identity = _scholarly_work_identity(str(work.get("id", "")), doi=doi)
        if work_identity is None or work_identity in seen_identities:
            continue
        seen_identities.add(work_identity)
        lowered_title = display_name.casefold()
        source_type = (
            "secondary"
            if any(term in lowered_title for term in _REVIEW_TITLE_TERMS)
            else "original study"
        )
        candidates.append(
            {
                "url": f"{settings.openalex_endpoint.rstrip('/')}/works/{work_id}",
                "title": display_name[:1000],
                "source_type": source_type,
                "resolution_strategy": "openalex_scholarly_catalog",
                "relevance_score": f"{max(relevance_score, query_relevance):.3f}",
                "work_identity": work_identity,
                "source_role": "independent_scholarly_work",
                "catalog_query": query,
            }
        )
        if len(candidates) >= 8:
            break
      if len(candidates) >= 8:
          break
    return candidates, {
        "catalog": "openalex",
        "queries": list(queries),
        "result_count": sum(len(payload.get("results", [])) for _, payload in payloads),
        "accepted_count": len(candidates),
        "errors": errors,
    }


def _classify_search_candidate(url: str, title: str, summary: str) -> str:
    lowered_url = url.casefold()
    lowered_text = f"{title} {summary}".casefold()
    if any(pattern in lowered_url for pattern in _PRIMARY_CITATION_PATTERNS):
        return "primary"
    hostname = (urlsplit(url).hostname or "").casefold()
    authoritative = any(part in f".{hostname}" for part in _AUTHORITATIVE_HOST_PARTS)
    primary_document = any(term in lowered_text for term in _PRIMARY_TITLE_TERMS)
    if authoritative and primary_document:
        return "primary"
    if authoritative:
        return "authoritative"
    return "secondary"


def _relevance_tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[\wÄÖÜäöüß-]{4,}", value, flags=re.UNICODE)
        if token.casefold() not in _RELEVANCE_STOP_WORDS
    }


def _candidate_relevance(
    opportunity_title: str,
    opportunity_summary: str,
    candidate_title: str,
    candidate_summary: str,
) -> tuple[float, int]:
    central = _relevance_tokens(opportunity_title)
    context = central | _relevance_tokens(opportunity_summary)
    candidate = _relevance_tokens(f"{candidate_title} {candidate_summary}")
    shared_central = central & candidate
    shared_context = context & candidate
    if not central:
        return 0.0, 0
    score = min(
        1.0,
        (len(shared_central) / len(central)) * 0.8
        + (len(shared_context) / max(1, len(context))) * 0.2,
    )
    return score, len(shared_central)


async def _generic_search_candidates(
    settings: Settings,
    *,
    title: str,
    summary: str,
    language: str,
    domain_policy: DomainPolicy,
    planned_queries: tuple[str, ...] = (),
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    engines = tuple(
        dict.fromkeys(
            part.strip()
            for part in (
                settings.searxng_general_engines + "," + settings.searxng_science_engines
            ).split(",")
            if part.strip()
        )
    )
    queries = tuple(
        dict.fromkeys((*planned_queries[:5], *_generic_evidence_queries(title, summary)))
    )[:6]
    query_outcomes: list[Any] = []
    for query in queries:
        try:
            query_outcomes.append(
                await search_searxng(
                    settings.searxng_endpoint,
                    query=query,
                    language=language,
                    policy=domain_policy,
                    maximum_results=5,
                    engines=engines,
                )
            )
        except Exception as exc:  # recorded below as bounded source-health evidence
            query_outcomes.append(exc)
        if query != queries[-1]:
            await asyncio.sleep(1)
    openalex_candidates, openalex_health = await _openalex_search_candidates(
        settings,
        title=title,
        summary=summary,
        domain_policy=domain_policy,
        planned_queries=planned_queries,
    )
    candidates: list[dict[str, str]] = []
    health: list[dict[str, Any]] = [openalex_health]
    seen: set[str] = set()
    seen_identities: set[str] = set()
    for candidate in openalex_candidates:
        seen.add(candidate["url"])
        if candidate.get("work_identity"):
            seen_identities.add(candidate["work_identity"])
        candidates.append(candidate)
    for query, outcome in zip(
        queries, query_outcomes, strict=True
    ):
        if isinstance(outcome, BaseException):
            health.append({"query": query, "error": str(outcome)[:300]})
            continue
        health.append(
            {
                "query": query,
                "requested_engines": list(outcome.requested_engines),
                "responding_engines": list(outcome.responding_engines),
                "unresponsive_engines": list(outcome.unresponsive_engines),
                "result_count": len(outcome.results),
            }
        )
        for result in outcome.results:
            url = str(result["url"])
            if url in seen:
                continue
            work_identity = _scholarly_work_identity(url)
            if work_identity and work_identity in seen_identities:
                continue
            seen.add(url)
            relevance_score, shared_title_terms = _candidate_relevance(
                title,
                summary,
                str(result["title"]),
                str(result.get("summary", "")),
            )
            minimum_shared_terms = 1 if len(_relevance_tokens(title)) <= 3 else 2
            if relevance_score < 0.32 or shared_title_terms < minimum_shared_terms:
                continue
            candidates.append(
                {
                    "url": url,
                    "title": str(result["title"]),
                    "snippet": str(result.get("summary", "")),
                    "search_query": query,
                    "source_type": _classify_search_candidate(
                        url, str(result["title"]), str(result.get("summary", ""))
                    ),
                    "resolution_strategy": "generic_evidence_search",
                    "relevance_score": f"{relevance_score:.3f}",
                    "work_identity": work_identity,
                    "source_role": "independent_web_evidence",
                }
            )
            if work_identity:
                seen_identities.add(work_identity)
            if len(candidates) >= 12:
                break
        if len(candidates) >= 12:
            break
    return candidates, health


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _vector_literal(values: tuple[float, ...]) -> str:
    return "[" + ",".join(f"{value:.10f}" for value in values) + "]"


async def _put_object(
    client: Minio, bucket: str, key: str, body: bytes, content_type: str
) -> None:
    await asyncio.to_thread(
        client.put_object,
        bucket,
        key,
        io.BytesIO(body),
        len(body),
        content_type=content_type,
    )


def _source_manifest(rows: list[asyncpg.Record]) -> list[dict[str, str]]:
    return [
        {
            "source_document_id": str(row["source_document_id"]),
            "url": row["canonical_url"],
            "title": row["title"],
        }
        for row in rows
    ]


async def _load_opportunity_source_rows(
    connection: asyncpg.Connection,
    opportunity_id: UUID,
    *,
    limit: int = 20,
) -> list[asyncpg.Record]:
    return list(
        await connection.fetch(
            """SELECT os.source_document_id, sd.canonical_url, sd.title
               FROM opportunity_sources os
               JOIN source_documents sd ON sd.id=os.source_document_id
               WHERE os.opportunity_id=$1
               ORDER BY os.result_rank, sd.canonical_url
               LIMIT $2""",
            opportunity_id,
            limit,
        )
    )


async def _existing_source_identities(
    connection: asyncpg.Connection, opportunity_id: UUID
) -> tuple[set[str], set[str]]:
    existing_urls = set(
        await connection.fetchval(
            """SELECT coalesce(array_agg(sd.canonical_url), ARRAY[]::text[])
               FROM opportunity_sources os
               JOIN source_documents sd ON sd.id=os.source_document_id
               WHERE os.opportunity_id=$1""",
            opportunity_id,
        )
    )
    existing_identities: set[str] = set()
    existing_rows = await connection.fetch(
        """SELECT sd.canonical_url,sd.reputation
           FROM opportunity_sources os
           JOIN source_documents sd ON sd.id=os.source_document_id
           WHERE os.opportunity_id=$1""",
        opportunity_id,
    )
    for existing in existing_rows:
        reputation = _json(existing["reputation"]) or {}
        identity = reputation.get("work_identity") or _scholarly_work_identity(
            str(existing["canonical_url"])
        )
        if identity:
            existing_identities.add(str(identity))
    return existing_urls, existing_identities


async def _persist_opportunity_source_candidates(
    connection: asyncpg.Connection,
    *,
    opportunity_id: UUID,
    candidates: list[dict[str, str]],
    default_search_purpose: str,
    lock_name: str,
    bootstrap_sources: bool = False,
    limit: int = _DEFAULT_SOURCE_LINK_LIMIT,
) -> list[dict[str, str]]:
    existing_urls, existing_identities = await _existing_source_identities(
        connection, opportunity_id
    )
    now = datetime.now(timezone.utc)
    linked: list[dict[str, str]] = []
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            lock_name,
        )
        next_rank = int(
            await connection.fetchval(
                """SELECT COALESCE(MAX(result_rank),0)+1
                   FROM opportunity_sources WHERE opportunity_id=$1""",
                opportunity_id,
            )
        )
        for candidate in candidates[:limit]:
            try:
                canonical_url = canonicalize_url(candidate["url"])
            except (KeyError, ValueError):
                continue
            if canonical_url in existing_urls:
                continue
            work_identity = candidate.get("work_identity") or _scholarly_work_identity(
                canonical_url
            )
            if work_identity and work_identity in existing_identities:
                continue
            hostname = urlsplit(canonical_url).hostname or ""
            title = str(candidate.get("title") or f"Source at {hostname}")[:1000]
            source_id = await connection.fetchval(
                """INSERT INTO source_documents
                (id,version,created_at,updated_at,deleted_at,canonical_url,title,author,
                 publisher,source_type,publication_at,event_at,reputation,domain)
                VALUES($1,1,$2,$2,NULL,$3,$4,NULL,$5,$6,NULL,NULL,$7::jsonb,$8)
                ON CONFLICT (canonical_url) DO UPDATE SET updated_at=source_documents.updated_at
                RETURNING id""",
                uuid4(),
                now,
                canonical_url,
                title,
                hostname,
                candidate.get("source_type", "secondary"),
                json.dumps(
                    {
                        "discovered_via": candidate.get(
                            "resolution_strategy", "topic_source_bootstrap"
                        ),
                        "popularity_is_proof": False,
                        "not_yet_acquired": True,
                        "relevance_score": candidate.get("relevance_score"),
                        "work_identity": work_identity,
                        "source_role": candidate.get("source_role"),
                        "bootstrap_sources": bootstrap_sources,
                    }
                ),
                hostname,
            )
            link_id = await connection.fetchval(
                """INSERT INTO opportunity_sources
                (id,opportunity_id,source_document_id,search_purpose,search_query,
                 result_rank,snippet,created_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (opportunity_id,source_document_id) DO NOTHING
                RETURNING id""",
                uuid4(),
                opportunity_id,
                source_id,
                str(candidate.get("search_purpose") or default_search_purpose)[:80],
                str(candidate.get("search_query") or candidate.get("catalog_query") or ""),
                next_rank,
                str(candidate.get("snippet") or title),
                now,
            )
            if link_id is None:
                continue
            existing_urls.add(canonical_url)
            if work_identity:
                existing_identities.add(work_identity)
            linked.append(
                {
                    "source_document_id": str(source_id),
                    "url": canonical_url,
                    "title": title,
                }
            )
            next_rank += 1
    return linked


@activity.defn(name="load-approved-opportunity-sources")
async def load_approved_opportunity_sources(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        opportunity = await connection.fetchrow(
            """SELECT o.id, o.subject_profile_id, o.title, o.summary, o.editorial_rationale,
                      o.decision, s.topic, s.research_goal, s.seed_queries,
                      s.related_concepts, s.negative_keywords, s.languages, s.domain_policy
               FROM opportunities o
               JOIN subject_profiles s ON s.id=o.subject_profile_id
               WHERE o.id=$1 AND o.deleted_at IS NULL AND s.deleted_at IS NULL""",
            UUID(str(request["opportunity_id"])),
        )
        if opportunity is None:
            raise ApplicationError("opportunity does not exist", non_retryable=True)
        if opportunity["decision"] != "approved":
            raise ApplicationError(
                "opportunity must be approved before source acquisition", non_retryable=True
            )
        rows = await _load_opportunity_source_rows(connection, opportunity["id"])
        bootstrap_sources = False
        if not rows:
            if not request.get("bootstrap_sources"):
                raise ApplicationError(
                    "approved opportunity has no linked source candidates", non_retryable=True
                )
            bootstrap_sources = True
            languages = _json_list(opportunity["languages"]) or ["all"]
            planned_queries = _topic_source_bootstrap_queries(
                title=str(opportunity["title"]),
                summary=" ".join(
                    [
                        str(opportunity["summary"] or ""),
                        str(opportunity["editorial_rationale"] or ""),
                    ]
                ),
                subject_topic=str(opportunity["topic"] or ""),
                research_goal=str(opportunity["research_goal"] or ""),
                seed_queries=_json_list(opportunity["seed_queries"]),
                related_concepts=_json_list(opportunity["related_concepts"]),
                negative_keywords=_json_list(opportunity["negative_keywords"]),
            )
            candidates, search_health = await _generic_search_candidates(
                settings,
                title=str(opportunity["title"]),
                summary=" ".join(
                    [
                        str(opportunity["summary"] or ""),
                        str(opportunity["editorial_rationale"] or ""),
                        str(opportunity["topic"] or ""),
                        str(opportunity["research_goal"] or ""),
                    ]
                ),
                language=str(languages[0]),
                domain_policy=DomainPolicy.from_mapping(_json(opportunity["domain_policy"])),
                planned_queries=planned_queries,
            )
            for candidate in candidates:
                candidate.setdefault("resolution_strategy", "topic_source_bootstrap")
                candidate.setdefault("search_purpose", "topic source bootstrap")
            linked = await _persist_opportunity_source_candidates(
                connection,
                opportunity_id=opportunity["id"],
                candidates=candidates,
                default_search_purpose="topic source bootstrap",
                lock_name=f"research.topic_source_bootstrap:{opportunity['id']}",
                bootstrap_sources=True,
                limit=12,
            )
            rows = await _load_opportunity_source_rows(connection, opportunity["id"])
            if not rows:
                raise ApplicationError(
                    "approved opportunity has no linked source candidates and bootstrap "
                    "discovery found no usable public sources",
                    non_retryable=True,
                )
        else:
            search_health = []
            linked = []
            planned_queries = ()
        result = {
            "opportunity_id": str(opportunity["id"]),
            "subject_profile_id": str(opportunity["subject_profile_id"]),
            "domain_policy": _json(opportunity["domain_policy"]) or {"allow": [], "block": []},
            "sources": _source_manifest(rows),
            "bootstrap_sources": bootstrap_sources,
            "bootstrap_linked_source_count": len(linked),
            "bootstrap_queries": list(planned_queries),
            "bootstrap_search_health": search_health,
        }
        return result
    finally:
        await connection.close()


@activity.defn(name="acquire-public-source")
async def acquire_source_candidate(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    source_document_id = UUID(str(request["source_document_id"]))
    idempotency_key = f"{request['workflow_id']}:{source_document_id}"
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        existing = await connection.fetchval(
            """SELECT result FROM idempotency_records
               WHERE scope='research.source_acquisition' AND idempotency_key=$1
                 AND status='succeeded'""",
            idempotency_key,
        )
        if existing:
            return _json(existing)
    finally:
        await connection.close()

    policy = DomainPolicy.from_mapping(request.get("domain_policy"))
    try:
        acquired = await acquire_public_source(
            str(request["url"]),
            policy=policy,
            throttle=_domain_throttle,
            user_agent=settings.acquisition_user_agent,
            maximum_bytes=settings.acquisition_max_bytes,
        )
    except (RobotsDenied, ValueError) as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except aiohttp.ClientError as exc:
        raise ApplicationError(f"source request failed: {type(exc).__name__}") from exc

    try:
        extracted = await asyncio.to_thread(
            extract_source_document,
            acquired.body,
            acquired.content_type,
            acquired.final_url,
        )
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    rendered_body: bytes | None = None
    browser_fallback_attempted = False
    browser_fallback_error: str | None = None
    if (
        acquired.content_type in {"text/html", "application/xhtml+xml"}
        and len(extracted.text) < 200
        and settings.firecrawl_endpoint
    ):
        browser_fallback_attempted = True
        try:
            rendered = await render_public_html(
                settings.firecrawl_endpoint,
                url=acquired.final_url,
                policy=policy,
                user_agent=settings.acquisition_user_agent,
                maximum_html_bytes=settings.acquisition_max_bytes,
            )
            rendered_extraction = await asyncio.to_thread(
                extract_source_document,
                rendered.body,
                "text/html",
                rendered.final_url,
            )
            if len(rendered_extraction.text) > len(extracted.text):
                rendered_body = rendered.body
                extracted = replace(
                    rendered_extraction,
                    extractor=f"firecrawl-rendered+{rendered_extraction.extractor}",
                    metadata={
                        **rendered_extraction.metadata,
                        "render_status_code": rendered.status_code,
                    },
                )
        except (aiohttp.ClientError, ValueError, asyncio.TimeoutError) as exc:
            # The direct immutable source remains useful and reviewable. A best-effort
            # browser failure is recorded rather than turning an approved HTTP source
            # into an endlessly retrying activity.
            browser_fallback_error = f"{type(exc).__name__}: {str(exc)[:300]}"

    normalized = extracted.text.encode("utf-8")
    semantic_chunks = chunk_semantic_text(extracted.text)
    raw_hash = hashlib.sha256(acquired.body).hexdigest()
    rendered_hash = hashlib.sha256(rendered_body).hexdigest() if rendered_body else None
    if rendered_body:
        content_hash = hashlib.sha256(
            b"raw\x00" + acquired.body + b"rendered\x00" + rendered_body
        ).hexdigest()
    else:
        content_hash = raw_hash
    base_key = f"research/snapshots/{content_hash}"
    raw_key = f"{base_key}/raw"
    normalized_key = f"{base_key}/normalized.txt"
    rendered_key = f"{base_key}/rendered.html" if rendered_body else None
    minio = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    await _put_object(
        minio, settings.minio_bucket, raw_key, acquired.body, acquired.content_type
    )
    await _put_object(
        minio, settings.minio_bucket, normalized_key, normalized, "text/plain; charset=utf-8"
    )
    if rendered_body and rendered_key:
        await _put_object(
            minio, settings.minio_bucket, rendered_key, rendered_body, "text/html; charset=utf-8"
        )

    now = datetime.now(timezone.utc)
    request_hash = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"source:{source_document_id}"
            )
            existing = await connection.fetchval(
                """SELECT result FROM idempotency_records
                   WHERE scope='research.source_acquisition' AND idempotency_key=$1
                     AND status='succeeded'""",
                idempotency_key,
            )
            if existing:
                return _json(existing)
            snapshot_id = await connection.fetchval(
                """SELECT id FROM source_snapshots
                   WHERE source_document_id=$1 AND content_hash=$2""",
                source_document_id,
                content_hash,
            )
            if snapshot_id is None:
                snapshot_number = await connection.fetchval(
                    """SELECT COALESCE(MAX(snapshot_number),0)+1
                       FROM source_snapshots WHERE source_document_id=$1""",
                    source_document_id,
                )
                snapshot_id = uuid4()
                await connection.execute(
                    """INSERT INTO source_snapshots
                    (id,source_document_id,snapshot_number,raw_object_key,normalized_object_key,
                     screenshot_object_key,content_hash,mime_type,byte_size,retrieved_at,
                     redirect_chain,extraction_metadata,injection_markers)
                    VALUES($1,$2,$3,$4,$5,NULL,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12::jsonb)""",
                    snapshot_id,
                    source_document_id,
                    snapshot_number,
                    raw_key,
                    normalized_key,
                    content_hash,
                    acquired.content_type,
                    len(acquired.body),
                    now,
                    json.dumps(acquired.redirect_chain),
                    json.dumps(
                        {
                            "extractor": extracted.extractor,
                            "active_elements_removed": extracted.removed_active_elements,
                            "document_metadata": extracted.metadata,
                            "raw_content_hash": raw_hash,
                            "rendered_content_hash": rendered_hash,
                            "rendered_object_key": rendered_key,
                            "browser_fallback_attempted": browser_fallback_attempted,
                            "browser_fallback_used": rendered_body is not None,
                            "browser_fallback_error": browser_fallback_error,
                            "robots_checked": True,
                            "robots_cache_hit": acquired.robots_cache_hit,
                            "dns_connection_pinned": True,
                            "tls_verified": acquired.final_url.startswith("https://"),
                        }
                    ),
                    json.dumps(extracted.injection_markers),
                )
                metadata = extracted.metadata
                publication_date = metadata.get("publication_date")
                await connection.execute(
                    """UPDATE source_documents
                       SET title=COALESCE(title,$2), author=COALESCE(author,$3),
                           publisher=COALESCE(publisher,$4),
                           publication_at=COALESCE(publication_at,
                             CASE WHEN $5::text ~ '^\\d{4}-\\d{2}-\\d{2}$'
                                  THEN ($5::text || 'T00:00:00Z')::timestamptz
                                  ELSE NULL END),
                           updated_at=$6, version=version+1
                       WHERE id=$1""",
                    source_document_id,
                    metadata.get("title"),
                    metadata.get("author"),
                    metadata.get("publisher"),
                    publication_date,
                    now,
                )
            for chunk_number, chunk in enumerate(semantic_chunks, start=1):
                await connection.execute(
                    """INSERT INTO semantic_chunks
                    (id,source_snapshot_id,chunk_number,text,location_anchor,start_offset,
                     end_offset,chunk_hash,token_count,embedding_model,embedding,created_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'feature-hash-v1',$10::vector,$11)
                    ON CONFLICT (source_snapshot_id,chunk_number) DO NOTHING""",
                    uuid4(),
                    snapshot_id,
                    chunk_number,
                    chunk.text,
                    chunk.location_anchor,
                    chunk.start_offset,
                    chunk.end_offset,
                    chunk.chunk_hash,
                    chunk.token_count,
                    _vector_literal(chunk.embedding),
                    now,
                )
            result = {
                "source_document_id": str(source_document_id),
                "source_snapshot_id": str(snapshot_id),
                "content_hash": content_hash,
                "content_type": acquired.content_type,
                "byte_size": len(acquired.body),
                "final_url": acquired.final_url,
                "injection_markers": list(extracted.injection_markers),
                "semantic_chunk_count": len(semantic_chunks),
                "extractor": extracted.extractor,
                "browser_fallback_used": rendered_body is not None,
            }
            await connection.execute(
                """INSERT INTO idempotency_records
                (id,scope,idempotency_key,request_hash,status,external_id,result,created_at,updated_at)
                VALUES($1,'research.source_acquisition',$2,$3,'succeeded',$4,$5::jsonb,$6,$6)""",
                uuid4(),
                idempotency_key,
                request_hash,
                str(snapshot_id),
                json.dumps(result),
                now,
            )
        return result
    finally:
        await connection.close()


@activity.defn(name="discover-linked-primary-sources")
async def discover_linked_primary_sources(request: dict[str, Any]) -> dict[str, Any]:
    """Enrich any opportunity with linked and independently searched evidence."""

    settings = Settings()
    opportunity_id = UUID(str(request["opportunity_id"]))
    snapshots = request.get("snapshots", [])
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        context = await connection.fetchrow(
            """SELECT o.title, o.summary, s.languages, s.domain_policy
               FROM opportunities o
               JOIN subject_profiles s ON s.id=o.subject_profile_id
               WHERE o.id=$1 AND o.deleted_at IS NULL AND s.deleted_at IS NULL""",
            opportunity_id,
        )
        if context is None:
            raise ApplicationError("opportunity does not exist", non_retryable=True)
        candidates: list[dict[str, str]] = []
        for snapshot in snapshots:
            if str(snapshot.get("content_type", "")).split(";", 1)[0] not in {
                "text/html",
                "application/xhtml+xml",
            }:
                continue
            raw_object_key = await connection.fetchval(
                "SELECT raw_object_key FROM source_snapshots WHERE id=$1",
                UUID(str(snapshot["source_snapshot_id"])),
            )
            if raw_object_key is None:
                continue
            response = await asyncio.to_thread(
                client.get_object, settings.minio_bucket, raw_object_key
            )
            try:
                body = await asyncio.to_thread(response.read)
            finally:
                response.close()
                response.release_conn()
            candidates.extend(_linked_primary_citations(body, str(snapshot["final_url"])))

        languages = _json(context["languages"]) or ["all"]
        search_plan = request.get("evidence_search_plan") or {}
        planned_queries = tuple(
            str(item.get("query", "")).strip()
            for item in search_plan.get("queries", [])
            if str(item.get("query", "")).strip()
        )
        searched_candidates, search_health = await _generic_search_candidates(
            settings,
            title=str(context["title"]),
            summary=str(context["summary"]),
            language=str(languages[0]),
            domain_policy=DomainPolicy.from_mapping(_json(context["domain_policy"])),
            planned_queries=planned_queries,
        )
        candidates.extend(searched_candidates)

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": settings.acquisition_user_agent},
        ) as http_session:
            registry_candidates = await asyncio.gather(
                *[
                    _resolve_registry_candidate(http_session, candidate)
                    for candidate in candidates
                ]
            )
        candidates.extend(
            candidate for candidate in registry_candidates if candidate is not None
        )
        candidates.extend(
            resolved
            for candidate in tuple(candidates)
            if (resolved := _public_api_candidate(candidate)) is not None
        )

        for candidate in candidates:
            candidate.setdefault(
                "search_purpose",
                "generic evidence enrichment"
                if candidate.get("resolution_strategy") == "generic_evidence_search"
                else "linked primary evidence",
            )
        linked = await _persist_opportunity_source_candidates(
            connection,
            opportunity_id=opportunity_id,
            candidates=candidates,
            default_search_purpose="linked primary evidence",
            lock_name=f"research.citation_enrichment:{opportunity_id}",
        )
        return {
            "sources": linked,
            "linked_source_count": len(linked),
            "search_health": search_health,
        }
    finally:
        await connection.close()


@activity.defn(name="complete-source-acquisition")
async def complete_source_acquisition(request: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    now = datetime.now(timezone.utc)
    opportunity_id = UUID(str(request["opportunity_id"]))
    actor_id = UUID(str(request["actor_id"]))
    workflow_id = str(request["workflow_id"])
    snapshots = request.get("snapshots", [])
    connection = await asyncpg.connect(settings.database_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"acquisition:{workflow_id}"
            )
            existing = await connection.fetchrow(
                "SELECT id, progress FROM research_runs WHERE workflow_id=$1", workflow_id
            )
            if existing:
                return {
                    "research_run_id": str(existing["id"]),
                    "snapshot_count": len(_json(existing["progress"]).get("snapshot_ids", [])),
                }
            decision = await connection.fetchval(
                "SELECT decision FROM opportunities WHERE id=$1 AND deleted_at IS NULL",
                opportunity_id,
            )
            if decision != "approved":
                raise ApplicationError(
                    "opportunity approval was withdrawn before acquisition completed",
                    non_retryable=True,
                )
            research_run_id = uuid4()
            await connection.execute(
                """INSERT INTO research_runs
                (id,version,created_at,updated_at,deleted_at,opportunity_id,workflow_id,state,
                 research_plan,progress,correlation_id,started_by,completed_at)
                VALUES($1,1,$2,$2,NULL,$3,$4,'RESEARCHING',$5::jsonb,$6::jsonb,$7,$8,NULL)""",
                research_run_id,
                now,
                opportunity_id,
                workflow_id,
                json.dumps(
                    {
                        "phase": "evidence extraction",
                        "source_snapshot_ids": [item["source_snapshot_id"] for item in snapshots],
                    }
                ),
                json.dumps(
                    {
                        "percent": 25,
                        "sources_acquired": len(snapshots),
                        "snapshot_ids": [item["source_snapshot_id"] for item in snapshots],
                    }
                ),
                str(request["correlation_id"]),
                actor_id,
            )
            await connection.execute(
                """INSERT INTO workflow_transitions
                (id,aggregate_type,aggregate_id,from_stage,to_stage,reason,actor_id,
                 correlation_id,occurred_at)
                VALUES($1,'opportunity',$2,'SHORTLISTED','RESEARCHING',
                       'approved sources acquired into immutable snapshots',$3,$4,$5)""",
                uuid4(),
                opportunity_id,
                actor_id,
                str(request["correlation_id"]),
                now,
            )
        return {"research_run_id": str(research_run_id), "snapshot_count": len(snapshots)}
    finally:
        await connection.close()
