from research_worker.activities import FIXTURE_SOURCES
from research_worker.workflows import (
    _bounded_extracted_sources,
    merge_synthesis_claims,
    target_coverage_units,
    FixtureResearchWorkflow,
    LiveDiscoveryWorkflow,
    LiveResearchDossierWorkflow,
    ScheduledSubjectDiscoveryWorkflow,
    SourceSemanticIndexWorkflow,
    SourceAcquisitionWorkflow,
)


def test_extracted_sources_are_bounded_deduplicated_and_compacted() -> None:
    sources = [
        {
            "source_document_id": f"source-{index}",
            "source_type": "primary" if index == 25 else "secondary",
            "content_hash": "duplicate" if index in {0, 1} else f"hash-{index}",
            "reputation": "{}" if index == 29 else {},
            "evidence": [{"relevance_score": index}],
            "chunks": [{"text": "large payload"}],
        }
        for index in range(30)
    ]

    compact = _bounded_extracted_sources(sources, include_chunks=False)

    assert len(compact) == 24
    assert compact[0]["source_document_id"] == "source-25"
    assert all(source["chunks"] == [] for source in compact)
    assert len({source["content_hash"] for source in compact}) == len(compact)


def test_extracted_sources_deduplicate_scholarly_url_aliases() -> None:
    sources = [
        {
            "source_document_id": "arxiv-abstract",
            "canonical_url": "https://arxiv.org/abs/2401.01234",
            "source_type": "primary",
            "content_hash": "abstract",
            "evidence": [{"relevance_score": 80}],
            "chunks": [],
        },
        {
            "source_document_id": "arxiv-pdf",
            "canonical_url": "https://arxiv.org/pdf/2401.01234.pdf",
            "source_type": "primary",
            "content_hash": "pdf",
            "evidence": [{"relevance_score": 70}],
            "chunks": [],
        },
    ]

    assert len(_bounded_extracted_sources(sources)) == 1


def test_extracted_sources_ignore_navigation_placeholders() -> None:
    sources = [
        {
            "source_document_id": "navigation",
            "title": "Skip to main content",
            "content_hash": "navigation",
            "evidence": [{"relevance_score": 99}],
            "chunks": [],
        },
        {
            "source_document_id": "study",
            "title": "A bounded geothermal lithium study",
            "content_hash": "study",
            "evidence": [{"relevance_score": 70}],
            "chunks": [],
        },
    ]

    assert [item["source_document_id"] for item in _bounded_extracted_sources(sources)] == [
        "study"
    ]


def test_synthesis_claim_bank_accumulates_and_merges_duplicate_evidence() -> None:
    current = {
        "claims": [
            {
                "statement": "Tiefe Geothermie erschliesst Wärme aus tiefen wasserführenden Schichten.",
                "claim_type": "fact",
                "central": True,
                "coverage_unit_ids": ["mechanism"],
                "evidence": [{"evidence_id": "a:1", "relationship": "supports"}],
            }
        ],
        "confidence": 0.8,
    }
    addition = {
        "claims": [
            {
                "statement": "Tiefe Geothermie erschliesst Wärme aus tiefen wasserführenden Schichten in Deutschland.",
                "claim_type": "fact",
                "central": False,
                "coverage_unit_ids": ["evidence"],
                "evidence": [{"evidence_id": "b:2", "relationship": "supports"}],
            },
            {
                "statement": "Gelöstes Lithium kann aus gefördertem Thermalwasser abgetrennt werden.",
                "claim_type": "fact",
                "central": False,
                "coverage_unit_ids": ["implications"],
                "evidence": [{"evidence_id": "c:3", "relationship": "supports"}],
            },
        ],
        "confidence": 0.9,
    }

    merged = merge_synthesis_claims(current, addition)

    assert len(merged["claims"]) == 2
    assert merged["claims"][0]["coverage_unit_ids"] == ["evidence", "mechanism"]
    assert len(merged["claims"][0]["evidence"]) == 2
    assert merged["confidence"] == 0.9


def test_target_units_include_missing_and_thin_coverage_only() -> None:
    plan = [
        {"id": "foundation", "question": "Foundation", "role": "foundation", "essential": True},
        {"id": "mechanism", "question": "Mechanism", "role": "mechanism", "essential": True},
        {"id": "limits", "question": "Limits", "role": "limits", "essential": True},
    ]
    readiness = {
        "coverage_units": [
            {"id": "foundation", "status": "covered"},
            {"id": "mechanism", "status": "covered"},
            {"id": "limits", "status": "missing"},
        ]
    }
    bank = {
        "claims": [
            {"coverage_unit_ids": ["foundation"]},
            {"coverage_unit_ids": ["foundation"]},
            {"coverage_unit_ids": ["mechanism"]},
        ]
    }

    assert [item["id"] for item in target_coverage_units(plan, readiness, bank)] == [
        "mechanism",
        "limits",
    ]


def test_fixture_workflow_and_hostile_fixture_are_registered() -> None:
    definition = getattr(FixtureResearchWorkflow, "__temporal_workflow_definition")
    assert definition.name == "fixture-research"
    assert any("ignore previous instructions" in source["body"] for source in FIXTURE_SOURCES)
    assert len(FIXTURE_SOURCES) == 4


def test_live_discovery_workflow_is_registered() -> None:
    definition = getattr(LiveDiscoveryWorkflow, "__temporal_workflow_definition")
    assert definition.name == "live-discovery"


def test_source_acquisition_workflow_is_registered() -> None:
    definition = getattr(SourceAcquisitionWorkflow, "__temporal_workflow_definition")
    assert definition.name == "source-acquisition"


def test_live_research_dossier_workflow_is_registered() -> None:
    definition = getattr(LiveResearchDossierWorkflow, "__temporal_workflow_definition")
    assert definition.name == "live-research-dossier"


def test_source_semantic_index_workflow_is_registered() -> None:
    definition = getattr(SourceSemanticIndexWorkflow, "__temporal_workflow_definition")
    assert definition.name == "source-semantic-index"


def test_scheduled_discovery_workflow_is_registered() -> None:
    definition = getattr(ScheduledSubjectDiscoveryWorkflow, "__temporal_workflow_definition")
    assert definition.name == "scheduled-subject-discovery"
