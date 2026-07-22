import pytest

from editorial_worker import review_activities
from editorial_worker.review_activities import (
    _invoke_model_with_heartbeat,
    _actionable_review_feedback,
    _normalize_evidence_plan,
    _source_independence_key,
    _structured_synthesis_sources,
)


def test_abstained_review_feedback_is_not_recycled_as_evidence_guidance() -> None:
    assert _actionable_review_feedback(
        {"abstained": True, "counterevidence_gaps": ["No claims were supplied"]}
    ) == {}
    actionable = {"abstained": False, "counterevidence_gaps": ["Missing comparator"]}
    assert _actionable_review_feedback(actionable) == actionable


@pytest.mark.asyncio
async def test_model_heartbeat_wrapper_returns_model_result(monkeypatch) -> None:
    async def fake_invoke(request: dict) -> dict:
        return {"task_type": request["task_type"]}

    monkeypatch.setattr(review_activities, "invoke_model", fake_invoke)

    assert await _invoke_model_with_heartbeat({"task_type": "evidence_reviewer"}) == {
        "task_type": "evidence_reviewer"
    }


def test_enrichment_plan_preserves_coverage_units_and_filters_query_ids() -> None:
    existing = [
        {"id": "foundation", "question": "What is the foundation?", "role": "foundation", "essential": True, "status": "covered"},
        {"id": "limits", "question": "What are the important limits?", "role": "limits", "essential": True, "status": "missing"},
    ]
    output = {
        "coverage_units": [{"id": "invented", "question": "Replacement?", "role": "evidence", "essential": True}],
        "queries": [{"query": "bounded query", "role": "counterevidence", "language": "en", "coverage_unit_ids": ["invented"]}],
        "confidence": 0.8,
        "abstained": False,
        "uncertainty": [],
    }

    normalized = _normalize_evidence_plan(output, existing)

    assert [item["id"] for item in normalized["coverage_units"]] == ["foundation", "limits"]
    assert normalized["queries"][0]["coverage_unit_ids"] == ["limits"]


def test_synthesis_sources_exclude_evidence_already_used_by_claim_bank() -> None:
    source = {
        "source_document_id": "source-a",
        "title": "Study A",
        "publisher": "Publisher",
        "source_type": "original study",
        "content_hash": "content-a",
        "evidence": [
            {"excerpt_hash": "used", "exact_text": "Already used evidence", "relevance_score": 90},
            {"excerpt_hash": "new", "exact_text": "New atomic evidence", "relevance_score": 80},
        ],
    }
    bank = [
        {
            "evidence": [
                {"evidence_id": "source-a:used", "relationship": "supports"}
            ]
        }
    ]

    structured = _structured_synthesis_sources([source], bank)

    assert [item["evidence_id"] for item in structured[0]["evidence"]] == [
        "source-a:new"
    ]


def test_synthesis_sources_are_bounded_for_temporal_and_model_payloads() -> None:
    sources = [
        {
            "source_document_id": f"source-{source_index}",
            "title": f"Study {source_index}",
            "publisher": "Publisher",
            "source_type": "original study",
            "content_hash": f"content-{source_index}",
            "evidence": [
                {
                    "excerpt_hash": f"excerpt-{evidence_index}",
                    "exact_text": "x" * 5_000,
                    "relevance_score": 100 - evidence_index,
                }
                for evidence_index in range(8)
            ],
        }
        for source_index in range(24)
    ]

    structured = _structured_synthesis_sources(sources, [])

    assert len(structured) == 16
    assert all(len(source["evidence"]) == 4 for source in structured)
    assert all(
        len(evidence["exact_excerpt"]) == 2_400
        for source in structured
        for evidence in source["evidence"]
    )


def test_synthesis_sources_prioritize_previously_unused_works() -> None:
    sources = [
        {
            "source_document_id": f"source-{index}",
            "title": f"Study {index}",
            "publisher": "Publisher",
            "source_type": "original study",
            "content_hash": f"content-{index}",
            "evidence": [
                {
                    "excerpt_hash": "excerpt",
                    "exact_text": f"Evidence from work {index}",
                    "relevance_score": 90,
                }
            ],
        }
        for index in range(20)
    ]
    bank = [
        {
            "evidence": [
                {
                    "evidence_id": f"source-{index}:excerpt",
                    "relationship": "supports",
                }
            ]
        }
        for index in range(10)
    ]

    structured = _structured_synthesis_sources(sources, bank)

    assert [source["source_document_id"] for source in structured[:10]] == [
        f"source-{index}" for index in range(10, 20)
    ]


def test_source_independence_prefers_work_then_content_then_document() -> None:
    assert _source_independence_key(
        {
            "source_document_id": "source-a",
            "content_hash": "same-content",
            "reputation": {"work_identity": "doi:10.1/a"},
        }
    ) == "doi:10.1/a"
    assert _source_independence_key(
        {
            "source_document_id": "source-a",
            "content_hash": "same-content",
            "reputation": {},
        }
    ) == "content:same-content"
    assert _source_independence_key(
        {"source_document_id": "source-a", "reputation": {}}
    ) == "document:source-a"


def test_source_independence_collapses_arxiv_and_doi_aliases() -> None:
    assert _source_independence_key(
        {
            "source_document_id": "source-a",
            "canonical_url": "https://arxiv.org/abs/2201.05048v1",
            "content_hash": "content-a",
            "reputation": {},
        }
    ) == _source_independence_key(
        {
            "source_document_id": "source-b",
            "canonical_url": "https://doi.org/10.48550/arXiv.2201.05048",
            "content_hash": "content-b",
            "reputation": {},
        }
    )
