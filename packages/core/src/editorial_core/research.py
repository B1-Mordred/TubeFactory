from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import unquote


_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# A full stop is a boundary unless it is specifically surrounded by digits.
# This keeps measurements such as ``1.5`` intact while still ending a sentence
# after a year or other integer (for example, ``during 2025.``).
_SENTENCE = re.compile(r".+?(?:[!?]+|(?<!\d)\.+|\.+(?!\d)|\n|$)")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?(?:\s*%)?\b")
_INFERENCE_MARKERS = {
    "because",
    "cause",
    "caused",
    "causes",
    "consequently",
    "implies",
    "therefore",
    "thus",
}
_OPINION_MARKERS = {"arguably", "believe", "best", "should", "worst"}
_NEGATIONS = {"no", "not", "never", "neither", "without"}
_HOSTILE_MARKERS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "system prompt",
    "developer message",
    "reveal your secrets",
    "execute this command",
)


def scholarly_work_identity(url: str, *, doi: str | None = None) -> str | None:
    """Normalize common DOI, arXiv, and OpenAlex aliases to one work identity."""

    decoded_url = unquote(str(url)).strip()
    normalized_doi = unquote(str(doi or "")).strip().casefold()
    doi_match = re.search(r"doi\.org/(10\.\d{4,9}/[^\s?#]+)", decoded_url, re.I)
    if not normalized_doi and doi_match:
        normalized_doi = doi_match.group(1).casefold()
    for prefix in ("https://doi.org/", "http://doi.org/"):
        if normalized_doi.startswith(prefix):
            normalized_doi = normalized_doi[len(prefix) :]
            break
    if normalized_doi:
        arxiv_doi = re.fullmatch(r"10\.48550/arxiv\.(.+)", normalized_doi, re.I)
        if arxiv_doi:
            return f"arxiv:{re.sub(r'v\d+$', '', arxiv_doi.group(1).casefold())}"
        return f"doi:{normalized_doi}"
    arxiv_match = re.search(
        r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)(\d{4}\.\d{4,5}(?:v\d+)?)",
        decoded_url,
        re.I,
    )
    if arxiv_match:
        return f"arxiv:{re.sub(r'v\d+$', '', arxiv_match.group(1).casefold())}"
    openalex_match = re.search(r"(?:openalex\.org/works/|openalex\.org/)(W\d+)", decoded_url, re.I)
    if openalex_match:
        return f"openalex:{openalex_match.group(1).casefold()}"
    return None
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "is", "it", "its", "of", "on", "or", "that", "the", "their", "this", "to",
    "was", "were", "will", "with",
}


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in _WORD.findall(value)
        if len(token) > 1 and token.casefold() not in _STOP_WORDS
    }


@dataclass(frozen=True)
class ExtractedEvidence:
    exact_text: str
    prefix_text: str
    suffix_text: str
    location_anchor: str
    start_offset: int
    end_offset: int
    excerpt_hash: str
    relevance_score: int


@dataclass(frozen=True)
class SemanticChunk:
    text: str
    location_anchor: str
    start_offset: int
    end_offset: int
    chunk_hash: str
    token_count: int
    embedding: tuple[float, ...]


def feature_hash_embedding(text: str, *, dimensions: int = 64) -> tuple[float, ...]:
    """Create a deterministic local embedding without model or network side effects."""
    if dimensions < 8:
        raise ValueError("embedding dimensions must be at least 8")
    values = [0.0] * dimensions
    for token in sorted(_tokens(text)):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        values[bucket] += -1.0 if digest[4] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values) if norm else tuple(values)


def chunk_semantic_text(
    text: str,
    *,
    target_tokens: int = 120,
    overlap_tokens: int = 20,
    maximum_chunks: int = 500,
) -> tuple[SemanticChunk, ...]:
    if target_tokens < 20 or overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("invalid semantic chunk window")
    matches = list(_WORD.finditer(text))
    if not matches or maximum_chunks < 1:
        return ()
    chunks: list[SemanticChunk] = []
    step = target_tokens - overlap_tokens
    for token_offset in range(0, len(matches), step):
        window = matches[token_offset : token_offset + target_tokens]
        if not window:
            break
        start, end = window[0].start(), window[-1].end()
        exact = text[start:end]
        chunks.append(
            SemanticChunk(
                text=exact,
                location_anchor=f"chunk:{start}:{end}",
                start_offset=start,
                end_offset=end,
                chunk_hash=hashlib.sha256(exact.encode()).hexdigest(),
                token_count=len(window),
                embedding=feature_hash_embedding(exact),
            )
        )
        if len(chunks) >= maximum_chunks or token_offset + target_tokens >= len(matches):
            break
    return tuple(chunks)


def extract_evidence_sentences(
    text: str,
    *,
    topic: str,
    research_goal: str = "",
    maximum_sentences: int = 12,
) -> tuple[ExtractedEvidence, ...]:
    """Select bounded, exact source sentences without generating new factual text."""
    if maximum_sentences < 1:
        return ()
    subject_tokens = _tokens(f"{topic} {research_goal}")
    candidates: list[ExtractedEvidence] = []
    fallbacks: list[ExtractedEvidence] = []
    for match in _SENTENCE.finditer(text):
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip())
        exact = re.sub(r"\s+", " ", raw.strip())
        start = match.start() + leading
        end = match.end()
        while end > start and text[end - 1].isspace():
            end -= 1
        words = _WORD.findall(exact)
        lowered = exact.casefold()
        if not 7 <= len(words) <= 140 or not 35 <= len(exact) <= 900:
            continue
        if any(marker in lowered for marker in _HOSTILE_MARKERS):
            continue
        overlap = len(_tokens(exact) & subject_tokens)
        score = overlap * 12
        score += 8 if _NUMBER.search(exact) else 0
        score += 6 if _YEAR.search(exact) else 0
        score += min(8, len(words) // 12)
        excerpt = ExtractedEvidence(
            exact_text=exact,
            prefix_text=re.sub(r"\s+", " ", text[max(0, start - 120) : start].strip()),
            suffix_text=re.sub(r"\s+", " ", text[end : end + 120].strip()),
            location_anchor=f"text:{start}:{end}",
            start_offset=start,
            end_offset=end,
            excerpt_hash=hashlib.sha256(exact.encode()).hexdigest(),
            relevance_score=score,
        )
        fallbacks.append(excerpt)
        if overlap or _NUMBER.search(exact) or _YEAR.search(exact):
            candidates.append(excerpt)
    ranked = candidates or fallbacks
    ranked.sort(key=lambda item: (-item.relevance_score, item.start_offset, item.excerpt_hash))
    return tuple(ranked[:maximum_sentences])


def sentence_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) if union else 0.0


def evidence_relation(reference: str, candidate: str) -> str:
    """Return a conservative lexical relation; uncertainty becomes context, not support."""
    similarity = sentence_similarity(reference, candidate)
    if similarity < 0.30:
        return "context"
    reference_negated = bool(_tokens(reference) & _NEGATIONS)
    candidate_negated = bool(_tokens(candidate) & _NEGATIONS)
    if reference_negated != candidate_negated:
        return "contradicts"
    return "supports"


def classify_claim(statement: str) -> str:
    tokens = _tokens(statement)
    if tokens & _OPINION_MARKERS:
        return "opinion"
    if tokens & _INFERENCE_MARKERS:
        return "inference"
    return "fact"


def cluster_evidence_statements(
    statements: Iterable[str], *, similarity_threshold: float = 0.42
) -> tuple[tuple[int, ...], ...]:
    """Group sentence indexes while preserving first-seen representatives."""
    values = tuple(statements)
    clusters: list[list[int]] = []
    for index, statement in enumerate(values):
        selected: int | None = None
        selected_similarity = 0.0
        for cluster_index, cluster in enumerate(clusters):
            similarity = sentence_similarity(values[cluster[0]], statement)
            if similarity >= similarity_threshold and similarity > selected_similarity:
                selected = cluster_index
                selected_similarity = similarity
        if selected is None:
            clusters.append([index])
        else:
            clusters[selected].append(index)
    return tuple(tuple(cluster) for cluster in clusters)
