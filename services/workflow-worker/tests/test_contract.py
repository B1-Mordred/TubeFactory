from workflow_worker.workflows import DurableProbeWorkflow


def test_durable_probe_contract_name_is_stable() -> None:
    definition = getattr(DurableProbeWorkflow, "__temporal_workflow_definition")
    assert definition.name == "durable-probe"
