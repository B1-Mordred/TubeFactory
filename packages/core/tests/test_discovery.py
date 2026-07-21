from editorial_core.discovery import (
    AtomicClaim,
    ClaimType,
    EvidenceLink,
    EvidenceRelation,
    RiskLevel,
    SearchFinding,
    SubjectBrief,
    canonicalize_url,
    cluster_findings,
    deduplicate_findings,
    evaluate_research_completion,
    plan_search,
    score_opportunity,
)


def test_search_plan_has_primary_and_falsification_branches() -> None:
    plan = plan_search(
        SubjectBrief(
            topic="viral health claim",
            research_goal="determine whether the claim is supported",
            seed_queries=("viral health claim evidence",),
            negative_keywords=("advertisement",),
        )
    )
    assert len(plan.strategies) == 3
    assert any(strategy.purpose == "primary evidence" for strategy in plan.strategies)
    assert "correction" in plan.falsification_queries[0]


def test_fixture_discovery_deduplicates_clusters_and_scores_transparently() -> None:
    findings = (
        SearchFinding("https://example.test/report?utm_source=x", "Agency climate report", "Agency publishes climate evidence"),
        SearchFinding("https://example.test/report", "Same URL", "duplicate"),
        SearchFinding("https://journal.test/study", "Climate evidence study", "New study examines climate evidence"),
    )
    assert canonicalize_url(findings[0].url) == "https://example.test/report"
    unique = deduplicate_findings(findings)
    assert len(unique) == 2
    clusters = cluster_findings(unique, similarity_threshold=0.20)
    assert len(clusters) == 1
    assert "similarity" in clusters[0].grouping_reasons[1]
    score = score_opportunity(
        {
            "audience_fit": 85,
            "evidence_potential": 90,
            "novelty": 60,
            "timeliness": 70,
            "educational_value": 88,
            "visual_explainability": 70,
            "channel_differentiation": 64,
        },
        {"risk": 20, "estimated_cost": 10, "duplication": 5},
    )
    assert 0 <= score.total <= 100
    assert len(score.reasoning) == 10


def test_high_risk_claim_requires_independent_and_primary_support() -> None:
    claim = AtomicClaim(
        "claim-1",
        "The measured value increased by ten percent.",
        ClaimType.FACT,
        central=True,
        risk=RiskLevel.HIGH,
        evidence=(
            EvidenceLink("excerpt-1", "source-1", EvidenceRelation.SUPPORTS, True, True, True),
            EvidenceLink("excerpt-2", "source-2", EvidenceRelation.CONTRADICTS, True, False, True),
        ),
    )
    result = evaluate_research_completion((claim,))
    assert result.complete is False
    assert "requires 2" in result.blockers[0]
