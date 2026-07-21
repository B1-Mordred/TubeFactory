from types import SimpleNamespace
from datetime import datetime, timezone

from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowExecutionStatus

from youtuber_api.research_gateway import TemporalResearchGateway, _execution_status_name
from youtuber_api.schemas import ResearchWorkflowCancel, ResearchWorkflowRetry


class FakeHandle:
    def __init__(self, execution_status: WorkflowExecutionStatus) -> None:
        self.execution_status = execution_status
        self.cancelled = False

    async def describe(self):
        return SimpleNamespace(status=self.execution_status)

    async def query(self, name: str):
        assert name == "status"
        return {
            "workflow_id": "workflow-12345678",
            "state": "ACQUIRING",
            "progress": 55,
            "result": None,
        }

    async def cancel(self) -> None:
        self.cancelled = True

    async def fetch_history_events(self):
        scheduled = HistoryEvent(
            event_id=5,
            event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
        )
        scheduled.event_time.FromDatetime(datetime(2026, 7, 20, tzinfo=timezone.utc))
        scheduled.activity_task_scheduled_event_attributes.activity_type.name = (
            "search-live-strategy"
        )
        completed = HistoryEvent(
            event_id=7,
            event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
        )
        completed.event_time.FromDatetime(datetime(2026, 7, 20, tzinfo=timezone.utc))
        completed.activity_task_completed_event_attributes.scheduled_event_id = 5
        yield scheduled
        yield completed


class FakeClient:
    def __init__(self, handle: FakeHandle) -> None:
        self.handle = handle

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        assert workflow_id == "workflow-12345678"
        return self.handle


def gateway_with(handle: FakeHandle) -> TemporalResearchGateway:
    gateway = object.__new__(TemporalResearchGateway)
    gateway.client = FakeClient(handle)
    return gateway


async def test_describe_preserves_completed_business_state() -> None:
    status = await gateway_with(FakeHandle(WorkflowExecutionStatus.COMPLETED)).describe(
        "workflow-12345678"
    )
    assert status["state"] == "ACQUIRING"
    assert status["execution_status"] == "COMPLETED"
    assert status["retryable"] is False


async def test_describe_makes_cancelled_execution_retryable() -> None:
    status = await gateway_with(FakeHandle(WorkflowExecutionStatus.CANCELED)).describe(
        "workflow-12345678"
    )
    assert status["state"] == "CANCELLED"
    assert status["execution_status"] == "CANCELLED"
    assert status["retryable"] is True


async def test_cancel_delegates_to_temporal() -> None:
    handle = FakeHandle(WorkflowExecutionStatus.RUNNING)
    await gateway_with(handle).cancel("workflow-12345678")
    assert handle.cancelled is True


async def test_safe_logs_expose_whitelisted_activity_labels_without_payloads() -> None:
    logs = await gateway_with(FakeHandle(WorkflowExecutionStatus.RUNNING)).safe_logs(
        "workflow-12345678"
    )
    assert [entry["message"] for entry in logs] == [
        "Private metasearch query scheduled.",
        "Private metasearch query completed.",
    ]
    assert all("query" not in entry for entry in logs)


def test_execution_status_mapping_and_control_request_validation() -> None:
    assert _execution_status_name(WorkflowExecutionStatus.CANCELED) == "CANCELLED"
    cancel = ResearchWorkflowCancel(reason="Operator cancelled this bounded research run.")
    retry = ResearchWorkflowRetry(
        idempotency_key="retry-key-12345678",
        reason="The transient provider outage has cleared.",
    )
    assert cancel.reason.startswith("Operator")
    assert retry.idempotency_key.startswith("retry-key")
