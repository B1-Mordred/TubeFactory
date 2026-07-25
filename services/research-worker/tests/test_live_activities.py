from datetime import datetime, timezone

import pytest
from temporalio.exceptions import ApplicationError

from editorial_core.discovery import FindingCluster, SearchFinding, SubjectBrief, plan_search
from research_worker.acquisition import SearxngSearchResponse
import research_worker.live_activities as live_module
from research_worker.live_activities import (
    _candidate_filter_kind,
    _finding_signature,
    _discovery_strategies,
    _is_explainer_candidate,
    _is_misinformation_candidate,
    _matches_processed_claim,
    _publication_at,
    _score_live_cluster,
    _source_domains,
    _uses_explainer_candidate_filter,
    search_live_strategy,
)
from research_worker.rescore import _is_placeholder_score


def test_publication_at_normalizes_searxng_timestamp() -> None:
    parsed = _publication_at("2026-07-21T08:00:00Z")

    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-07-21T08:00:00+00:00"


def test_publication_at_ignores_unparseable_values() -> None:
    assert _publication_at("recently") is None


def test_source_domains_are_computed_for_persisted_cluster_trace() -> None:
    findings = (
        SearchFinding("https://one.example.test/report", "One", "Summary"),
        SearchFinding("https://two.example.test/study", "Two", "Summary"),
        SearchFinding("https://one.example.test/update", "Update", "Summary"),
    )

    assert _source_domains(findings) == {"one.example.test", "two.example.test"}


def test_explainer_candidate_filter_rejects_navigation_noise_but_keeps_questions() -> None:
    assert _is_explainer_candidate(
        {
            "url": "https://science.example.test/batteries-age",
            "title": "Warum Batterien mit der Zeit schwächer werden",
            "summary": "Messungen erklären die Wirkung wiederholter Ladezyklen.",
        }
    )
    assert _is_explainer_candidate(
        {
            "url": "https://health.example.test/study",
            "title": "Studie untersucht Zusammenhang zwischen Sitzen und Gesundheit",
            "summary": "Forschende ordnen die Daten ein.",
        }
    )
    assert not _is_explainer_candidate(
        {
            "url": "https://www.youtube.com/",
            "title": "YouTube",
            "summary": "Share your videos with friends, family, and the world.",
        }
    )
    assert not _is_explainer_candidate(
        {
            "url": "https://news.example.test/",
            "title": "Aktuelle Nachrichten aus Deutschland",
            "summary": "Politik, Wirtschaft, Sport und Wetter im Überblick.",
        }
    )
    assert not _is_explainer_candidate(
        {
            "url": "https://www.facebook.com/help/login",
            "title": "How to log in to Facebook",
            "summary": "Account help.",
        }
    )
    assert not _is_explainer_candidate(
        {
            "url": "https://answers.example.test/office/save",
            "title": "Issues with saving a document",
            "summary": "A product support question about opening a file.",
        }
    )


def test_all_explainer_targets_use_candidate_hygiene_filter() -> None:
    assert _uses_explainer_candidate_filter({"target": "simple_explainer"})
    assert _uses_explainer_candidate_filter({"target": "evidence_first_explainer"})
    assert not _uses_explainer_candidate_filter({"target": "news_commentary"})


def test_explainer_filter_uses_editorial_format_not_only_target() -> None:
    assert _uses_explainer_candidate_filter(
        {
            "format_policy": {"target": "standard"},
            "editorial_profile": {"format": "einfaches_erklaervideo"},
        }
    )


def test_manual_only_discovery_mode_disables_live_discovery() -> None:
    assert (
        _candidate_filter_kind(
            {
                "format_policy": {"target": "standard", "discovery_mode": "manual_only"},
                "editorial_profile": {"format": "einfaches_erklaervideo"},
            }
        )
        == "disabled"
    )


def test_explainer_filter_rejects_query_word_noise_from_observed_results() -> None:
    strategy = {
        "query": (
            "Warum Batterien mit der Zeit schwächer werden einfach erklärt "
            "-\"Kaufberatung\" -\"Preisvergleich\""
        )
    }

    assert not _is_explainer_candidate(
        {
            "url": "https://www.dwds.de/wb/warum",
            "title": "warum – Schreibung, Definition, Bedeutung, Etymologie, Synonyme ...",
            "summary": "warum Adv. ‘weshalb, aus welchem Grunde’.",
        },
        strategy,
    )
    assert not _is_explainer_candidate(
        {
            "url": "https://www.ardmediathek.de/video/tatort-warum",
            "title": "Tatort: Warum - hier anschauen - ARD Mediathek",
            "summary": "Sie hat Todesangst. Doch warum?",
        },
        strategy,
    )


def test_explainer_filter_rejects_product_or_cost_articles() -> None:
    assert not _is_explainer_candidate(
        {
            "url": "https://www.gamestar.de/artikel/oled-monitor-test",
            "title": "OLED-Monitore sind das Nonplusultra, doch in einer Disziplin tun sie sich schwer",
            "summary": "Der Test zeigt, wie gut dieser Monitor funktioniert.",
        },
        {"query": "Wie OLED Displays funktionieren einfach erklärt"},
    )
    assert not _is_explainer_candidate(
        {
            "url": "https://www.capital.de/immobilien/waermepumpen-wartung-kosten",
            "title": "Wärmepumpen-Wartung: Wie viel Geld der Service kostet",
            "summary": "Ein Experte erklärt, wie oft Eigentümer ihre Anlage checken lassen.",
        },
        {"query": "Wie Wärmepumpen funktionieren einfach erklärt"},
    )


def test_misinformation_filter_rejects_generic_noise_but_keeps_claim_checks() -> None:
    strategy = {
        "query": (
            "deutschsprachige Nachrichten Falschmeldung Faktencheck aktuell "
            "-\"Satire\" -\"Meinung\""
        )
    }

    assert not _is_misinformation_candidate(
        {
            "url": "https://de.wikipedia.org/wiki/YouTube",
            "title": "YouTube – Wikipedia",
            "summary": "YouTube ist ein Videoportal.",
        },
        strategy,
    )
    assert not _is_misinformation_candidate(
        {
            "url": "https://www.tagesschau.de/faktenfinder/desinformation-erkennen",
            "title": "Wie Desinformation zu erkennen ist",
            "summary": "Ein Leitfaden für Medienkompetenz.",
        },
        strategy,
    )
    assert _is_misinformation_candidate(
        {
            "url": "https://correctiv.org/faktencheck/2026/beispiel",
            "title": "Faktencheck widerlegt falsche Behauptung zu aktuellen Gesundheitsdaten",
            "summary": "Amtliche Zahlen korrigieren eine viral verbreitete Falschmeldung.",
        },
        strategy,
    )


@pytest.mark.parametrize(
    "title",
    (
        "UMSICHT-Wissenschaftspreis 2026 verliehen",
        "Workshop-Reihe für Zukunftsthemen",
        "Neue Unterrichtsmaterialien bringen Forschung in Schulen",
    ),
)
def test_explainer_filter_rejects_institutional_announcements(title: str) -> None:
    assert not _is_explainer_candidate(
        {
            "url": "https://research.example.test/announcement",
            "title": title,
            "summary": "Forschung und Technologie stehen im Mittelpunkt.",
        }
    )


def test_topic_radar_defers_evidence_strategies_until_after_selection() -> None:
    plan = plan_search(
        SubjectBrief(
            topic="Aktuelle Wissenschaft",
            research_goal="Erklärbare Themen finden",
            seed_queries=("site:science.example.test Forschung",),
        )
    )

    deferred = _discovery_strategies(
        plan, {"evidence_research_timing": "after_topic_selection"}
    )
    full = _discovery_strategies(plan, {})

    assert {item["purpose"] for item in deferred} == {"broad discovery"}
    assert {item["purpose"] for item in full} >= {"primary evidence", "falsification"}


def test_processed_claim_matches_normalized_title_and_summary() -> None:
    signatures = (_finding_signature("Behauptung widerlegt", "Die Quelle korrigiert den Bericht."),)

    assert _matches_processed_claim(
        SearchFinding(
            "https://example.test/update",
            "  BEHAUPTUNG   widerlegt ",
            "Die Quelle korrigiert den Bericht.",
        ),
        signatures,
    )


def test_processed_claim_matches_high_similarity_across_urls() -> None:
    signatures = (
        _finding_signature(
            "Falsche Behauptung zur Energieversorgung widerlegt",
            "Bundesnetzagentur nennt aktuelle Zahlen und korrigiert die verbreitete Darstellung.",
        ),
    )

    assert _matches_processed_claim(
        SearchFinding(
            "https://second.example.test/syndicated",
            "Falsche Behauptung zur Energieversorgung klar widerlegt",
            "Bundesnetzagentur nennt aktuelle Zahlen und korrigiert die verbreitete Darstellung.",
        ),
        signatures,
    )


def test_materially_different_update_is_not_suppressed() -> None:
    signatures = (
        _finding_signature(
            "Behauptung zur Energieversorgung widerlegt",
            "Bundesnetzagentur korrigiert den ursprünglichen Bericht.",
        ),
    )

    assert not _matches_processed_claim(
        SearchFinding(
            "https://example.test/material-update",
            "Neue Studie verändert Bewertung der Energieversorgung",
            "Unabhängige Forschende veröffentlichen neue Messdaten und eine abweichende Zeitreihe.",
        ),
        signatures,
    )


def test_live_scores_vary_with_observed_result_quality_and_explain_signals() -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    strong = SearchFinding(
        "https://authority.example.test/report",
        "Official 2026 health data corrects viral vaccine claim",
        "The national health agency publishes 12 measured outcomes and the underlying dataset.",
        published_at="2026-07-21T08:00:00Z",
        source_type="primary",
    )
    weak = SearchFinding(
        "https://blog.example.test/opinion",
        "Some thoughts about the news",
        "A short commentary.",
        source_type="secondary",
    )
    topic = "viral vaccine health claim evidence"
    strong_score = _score_live_cluster(
        FindingCluster((strong,), ("one result",)),
        {strong.canonical_url: ({}, {"purpose": "primary evidence", "query": topic}, 1)},
        (),
        topic=topic,
        subject_risk="high",
        lookback_days=7,
        total_unique_domains=8,
        now=now,
    )
    weak_score = _score_live_cluster(
        FindingCluster((weak,), ("one result",)),
        {weak.canonical_url: ({}, {"purpose": "broad discovery", "query": topic}, 9)},
        (),
        topic=topic,
        subject_risk="high",
        lookback_days=7,
        total_unique_domains=8,
        now=now,
    )

    assert strong_score.total > weak_score.total
    assert strong_score.positive != weak_score.positive
    assert strong_score.penalties != weak_score.penalties
    assert any("best search rank 1" in reason for reason in strong_score.reasoning)
    assert any("Final weighted score" in reason for reason in strong_score.reasoning)


def test_live_score_penalizes_similarity_to_prior_findings() -> None:
    finding = SearchFinding(
        "https://new.example.test/update",
        "Agency corrects energy supply claim",
        "Official figures contradict the repeated claim about national energy reserves.",
    )
    prior = (_finding_signature(finding.title, "Official figures contradict the claim about national energy reserves."),)
    common = {
        "topic": "energy supply claim",
        "subject_risk": "medium",
        "lookback_days": 30,
        "total_unique_domains": 4,
        "now": datetime(2026, 7, 21, tzinfo=timezone.utc),
    }
    fresh = _score_live_cluster(
        FindingCluster((finding,), ()),
        {finding.canonical_url: ({}, {"purpose": "broad discovery", "query": "energy supply claim"}, 2)},
        (),
        **common,
    )
    similar = _score_live_cluster(
        FindingCluster((finding,), ()),
        {finding.canonical_url: ({}, {"purpose": "broad discovery", "query": "energy supply claim"}, 2)},
        prior,
        **common,
    )

    assert similar.positive["novelty"] < fresh.positive["novelty"]
    assert similar.penalties["duplication"] > fresh.penalties["duplication"]
    assert similar.total < fresh.total


def test_legacy_score_without_source_provenance_uses_explicit_zero_source_inputs() -> None:
    finding = SearchFinding(
        "https://unavailable.invalid/opportunity/example",
        "Stored title about a documented energy claim",
        "Stored summary with enough detail to evaluate topic coverage without inventing a source.",
        source_type="unknown",
    )
    score = _score_live_cluster(
        FindingCluster((finding,), ()),
        {},
        (),
        topic="documented energy claim",
        subject_risk="medium",
        lookback_days=30,
        total_unique_domains=0,
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
        source_provenance_available=False,
    )

    assert any("0 result(s), 0 domain(s)" in reason for reason in score.reasoning)
    assert any("best search rank unavailable" in reason for reason in score.reasoning)
    assert any("no publication date or search rank" in reason for reason in score.reasoning)


def test_legacy_placeholder_detection_is_narrow_and_idempotent() -> None:
    positive = {
        "audience_fit": 60,
        "evidence_potential": 60,
        "novelty": 55,
        "timeliness": 60,
        "educational_value": 65,
        "visual_explainability": 50,
        "channel_differentiation": 50,
    }
    penalties = {"risk": 25, "estimated_cost": 13, "duplication": 0}

    assert _is_placeholder_score(positive, penalties)
    assert not _is_placeholder_score({**positive, "audience_fit": 61}, penalties)
    assert not _is_placeholder_score(positive, {**penalties, "duplication": 1})


@pytest.mark.asyncio
async def test_recent_search_falls_back_to_evergreen_and_reports_health(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_search(_endpoint: str, **kwargs):
        calls.append(kwargs)
        if kwargs["lookback_days"] is not None:
            return SearxngSearchResponse(
                results=(),
                requested_engines=tuple(kwargs["engines"]),
                responding_engines=(),
                unresponsive_engines=(),
                time_range="month",
            )
        return SearxngSearchResponse(
            results=(
                {
                    "url": "https://example.test/explainer",
                    "title": "A useful explanation",
                    "summary": "Evidence",
                    "published_at": None,
                    "source_type": "secondary",
                },
            ),
            requested_engines=tuple(kwargs["engines"]),
            responding_engines=("bing",),
            unresponsive_engines=(),
            time_range=None,
        )

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(live_module, "search_searxng", fake_search)
    monkeypatch.setattr(live_module.asyncio, "sleep", no_wait)
    result = await search_live_strategy(
        {
            "strategy": {"purpose": "broad discovery", "query": "Thema", "language": "de"},
            "freshness_policy": {"lookback_days": 21},
            "domain_policy": {},
        }
    )

    assert [call["lookback_days"] for call in calls] == [21, None]
    assert result["health"]["fallback_used"] is True
    assert result["health"]["freshness_mode"] == "recent_then_evergreen"
    assert result["results"][0]["title"] == "A useful explanation"


@pytest.mark.asyncio
async def test_empty_results_with_engine_failures_raise_retryable_source_error(monkeypatch) -> None:
    async def fake_search(_endpoint: str, **kwargs):
        return SearxngSearchResponse(
            results=(),
            requested_engines=tuple(kwargs["engines"]),
            responding_engines=(),
            unresponsive_engines=({"engine": "bing", "reason": "too many requests"},),
            time_range=None,
        )

    monkeypatch.setattr(live_module, "search_searxng", fake_search)
    with pytest.raises(ApplicationError) as caught:
        await search_live_strategy(
            {
                "strategy": {"purpose": "primary evidence", "query": "Thema", "language": "de"},
                "freshness_policy": {"lookback_days": 21},
                "domain_policy": {},
            }
        )

    assert caught.value.type == "SearchBackendUnavailable"
    assert caught.value.non_retryable is False
    assert caught.value.details[0]["unresponsive_engines"] == [
        {"engine": "bing", "reason": "too many requests"}
    ]
