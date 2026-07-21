from research_worker.activities import FIXTURE_SOURCES
from research_worker.workflows import (
    FixtureResearchWorkflow,
    LiveDiscoveryWorkflow,
    LiveResearchDossierWorkflow,
    ScheduledSubjectDiscoveryWorkflow,
    SourceSemanticIndexWorkflow,
    SourceAcquisitionWorkflow,
)


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
