from datetime import datetime, timezone

from editorial_core.discovery import FindingCluster, SearchFinding
from research_worker.live_activities import (
    _finding_signature,
    _matches_processed_claim,
    _publication_at,
    _score_live_cluster,
)
from research_worker.rescore import _is_placeholder_score


def test_publication_at_normalizes_searxng_timestamp() -> None:
    parsed = _publication_at("2026-07-21T08:00:00Z")

    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-07-21T08:00:00+00:00"


def test_publication_at_ignores_unparseable_values() -> None:
    assert _publication_at("recently") is None


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
