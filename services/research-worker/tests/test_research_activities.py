from uuid import uuid4

from research_worker.research_activities import (
    detect_dependent_sources,
    prepare_ai_synthesized_claims,
)


def source(
    source_id: str,
    content_hash: str,
    chunk_hash: str,
    embedding: list[float],
    *,
    identity: str | None = None,
    evidence_text: str = "A detailed bounded passage with the same relevant facts and observations.",
):
    return {
        "source_document_id": source_id,
        "content_hash": content_hash,
        "domain": "example.test",
        "reputation": {"work_identity": identity} if identity else {},
        "evidence": [{"exact_text": evidence_text}],
        "chunks": [{"chunk_hash": chunk_hash, "embedding": embedding}],
    }


def test_semantically_near_duplicate_sources_are_marked_dependent() -> None:
    relationships, dependent = detect_dependent_sources(
        [
            source("source-a", "content-a", "chunk-a", [1.0, 0.0, 0.0]),
            source("source-b", "content-b", "chunk-b", [0.98, 0.10, 0.0]),
            source("source-c", "content-c", "chunk-c", [0.0, 0.0, 1.0]),
        ]
    )
    assert relationships == [
        {
            "source_document_id": "source-a",
            "related_source_document_id": "source-b",
            "relationship": "near_duplicate",
            "confidence": 99,
            "reason": "local feature-hash chunk cosine similarity 0.995",
        }
    ]
    assert dependent == {"source-a": "source-a", "source-b": "source-a"}


def test_shared_chunk_hash_is_an_exact_duplicate_even_with_different_content_hashes() -> None:
    relationships, dependent = detect_dependent_sources(
        [
            source("source-a", "content-a", "shared", [1.0, 0.0]),
            source("source-b", "content-b", "shared", [0.0, 1.0]),
        ]
    )
    assert relationships[0]["relationship"] == "exact_duplicate"
    assert relationships[0]["confidence"] == 100
    assert dependent == {"source-a": "source-a", "source-b": "source-a"}


def test_ai_synthesis_rejects_document_description_meta_claims() -> None:
    source_id = str(uuid4())
    excerpt = {
        "exact_text": "Die Studie vergleicht zwei technische Verfahren.",
        "excerpt_hash": "excerpt-1",
        "source": {
            "source_document_id": source_id,
            "source_type": "original study",
        },
    }
    assert prepare_ai_synthesized_claims(
        {
            "confidence": 0.8,
            "abstained": False,
            "claims": [
                {
                    "statement": "Der Lerninhalt könnte einen Vergleich der Verfahren umfassen.",
                    "claim_type": "fact",
                    "central": True,
                    "coverage_unit_ids": ["mechanism"],
                    "evidence": [
                        {
                            "evidence_id": f"{source_id}:excerpt-1",
                            "relationship": "supports",
                        }
                    ],
                }
            ],
        },
        [excerpt],
        {},
        minimum_independent=1,
        coverage_unit_ids={"mechanism"},
    ) == []


def test_distinct_scholarly_works_are_not_collapsed_by_catalog_similarity() -> None:
    relationships, dependency_groups = detect_dependent_sources(
        [
            source(
                "source-a",
                "content-a",
                "chunk-a",
                [1.0, 0.0],
                identity="doi:10.1/a",
            ),
            source(
                "source-b",
                "content-b",
                "chunk-b",
                [0.999, 0.01],
                identity="doi:10.1/b",
            ),
        ]
    )

    assert relationships == []
    assert dependency_groups == {}


def evidence_candidate(source_id: str, identity: str, excerpt_hash: str) -> dict:
    return {
        "exact_text": f"Exact evidence from {source_id} establishes a bounded relevant observation.",
        "excerpt_hash": excerpt_hash,
        "source": {
            "source_document_id": source_id,
            "source_type": "original study",
            "domain": "api.openalex.org",
            "reputation": {"work_identity": identity},
        },
    }


def test_ai_synthesis_counts_distinct_works_from_one_catalog_as_independent() -> None:
    candidates = [
        evidence_candidate("source-a", "doi:10.1/a", "excerpt-a"),
        evidence_candidate("source-b", "doi:10.1/b", "excerpt-b"),
    ]
    prepared = prepare_ai_synthesized_claims(
        {
            "confidence": 0.88,
            "abstained": False,
            "claims": [
                {
                    "statement": "Two distinct studies support a cautious bounded conclusion.",
                    "claim_type": "fact",
                    "central": True,
                    "evidence": [
                        {"evidence_id": "source-a:excerpt-a", "relationship": "supports"},
                        {"evidence_id": "source-b:excerpt-b", "relationship": "supports"},
                    ],
                }
            ],
        },
        candidates,
        {},
    )
    assert len(prepared) == 1
    assert [link["independent"] for link in prepared[0]["links"]] == [True, True]


def test_ai_synthesis_rejects_unknown_evidence_and_collapses_work_aliases() -> None:
    candidates = [
        evidence_candidate("source-a", "doi:10.1/a", "excerpt-a"),
        evidence_candidate("source-alias", "doi:10.1/a", "excerpt-alias"),
    ]
    prepared = prepare_ai_synthesized_claims(
        {
            "confidence": 0.9,
            "abstained": False,
            "claims": [
                {
                    "statement": "A proposed central claim remains subject to human review.",
                    "claim_type": "fact",
                    "central": True,
                    "evidence": [
                        {"evidence_id": "source-a:excerpt-a", "relationship": "supports"},
                        {"evidence_id": "source-alias:excerpt-alias", "relationship": "supports"},
                        {"evidence_id": "invented:missing", "relationship": "supports"},
                    ],
                }
            ],
        },
        candidates,
        {},
    )
    assert prepared == []


def test_one_representative_from_duplicate_group_can_count_as_independent() -> None:
    candidates = [
        evidence_candidate("source-a", "doi:10.1/a", "excerpt-a"),
        evidence_candidate("source-b", "doi:10.1/b", "excerpt-b"),
    ]
    prepared = prepare_ai_synthesized_claims(
        {
            "confidence": 0.9,
            "abstained": False,
            "claims": [
                {
                    "statement": "One cited representative stands for its duplicate source group.",
                    "claim_type": "fact",
                    "central": True,
                    "evidence": [
                        {"evidence_id": "source-a:excerpt-a", "relationship": "supports"},
                        {"evidence_id": "source-b:excerpt-b", "relationship": "supports"},
                    ],
                }
            ],
        },
        candidates,
        {"source-a": "source-a", "source-alias": "source-a"},
    )

    assert len(prepared) == 1
    assert [link["independent"] for link in prepared[0]["links"]] == [True, True]


def test_all_central_model_output_is_limited_and_weak_details_are_retained() -> None:
    candidates = [
        evidence_candidate("source-a", "doi:10.1/a", "excerpt-a"),
        evidence_candidate("source-b", "doi:10.1/b", "excerpt-b"),
    ]
    claims = []
    for index in range(6):
        evidence = [
            {"evidence_id": "source-a:excerpt-a", "relationship": "supports"},
            {"evidence_id": "source-b:excerpt-b", "relationship": "supports"},
        ] if index < 5 else [
            {"evidence_id": "source-a:excerpt-a", "relationship": "supports"}
        ]
        claims.append(
            {
                "statement": f"This is distinct bounded factual statement number {index} supported by exact evidence.",
                "claim_type": "fact",
                "central": True,
                "coverage_unit_ids": ["mechanism"],
                "evidence": evidence,
            }
        )

    prepared = prepare_ai_synthesized_claims(
        {"confidence": 0.9, "abstained": False, "claims": claims},
        candidates,
        {},
        coverage_unit_ids={"mechanism"},
    )

    assert len(prepared) == 6
    assert sum(1 for claim in prepared if claim["central"]) == 4
    assert prepared[-1]["central"] is False
