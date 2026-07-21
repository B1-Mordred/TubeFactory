from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.common import RetryPolicy
from temporalio.service import RPCError, RPCStatusCode

from youtuber_api.config import get_settings


class SubjectScheduleGateway:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.settings = get_settings()

    @staticmethod
    def schedule_id(subject_profile_id: UUID) -> str:
        return f"subject-discovery-{subject_profile_id}"

    def _schedule(
        self, *, subject_profile_id: UUID, cron: str, timezone_name: str, paused: bool
    ) -> Schedule:
        schedule_id = self.schedule_id(subject_profile_id)
        return Schedule(
            action=ScheduleActionStartWorkflow(
                "scheduled-subject-discovery",
                {
                    "subject_profile_id": str(subject_profile_id),
                    "schedule_id": schedule_id,
                },
                id=f"scheduled-subject-discovery-{subject_profile_id}",
                task_queue=self.settings.temporal_research_task_queue,
                retry_policy=RetryPolicy(maximum_attempts=3),
            ),
            spec=ScheduleSpec(cron_expressions=[cron], time_zone_name=timezone_name),
            policy=SchedulePolicy(
                overlap=ScheduleOverlapPolicy.SKIP,
                catchup_window=timedelta(minutes=5),
                pause_on_failure=True,
            ),
            state=ScheduleState(
                note="Managed by TubeFactory subject profile reconciliation",
                paused=paused,
            ),
        )

    async def reconcile(
        self,
        *,
        subject_profile_id: UUID,
        cron: str | None,
        timezone_name: str,
        enabled: bool,
    ) -> dict[str, Any]:
        schedule_id = self.schedule_id(subject_profile_id)
        handle = self.client.get_schedule_handle(schedule_id)
        if cron is None:
            try:
                await handle.pause(note="Subject schedule disabled in its versioned profile")
            except RPCError as exc:
                if exc.status == RPCStatusCode.NOT_FOUND:
                    return self._missing(schedule_id, cron, timezone_name)
                raise
            return await self.describe(subject_profile_id)

        schedule = self._schedule(
            subject_profile_id=subject_profile_id,
            cron=cron,
            timezone_name=timezone_name,
            paused=not enabled,
        )
        try:
            await self.client.create_schedule(schedule_id, schedule)
        except ScheduleAlreadyRunningError:
            await handle.update(lambda _: ScheduleUpdate(schedule=schedule))
        return await self.describe(subject_profile_id)

    async def describe(self, subject_profile_id: UUID) -> dict[str, Any]:
        schedule_id = self.schedule_id(subject_profile_id)
        try:
            description = await self.client.get_schedule_handle(schedule_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return self._missing(schedule_id, None, "UTC")
            raise
        schedule = description.schedule
        return {
            "schedule_id": schedule_id,
            "exists": True,
            "paused": schedule.state.paused,
            "cron": schedule.spec.cron_expressions[0]
            if schedule.spec.cron_expressions
            else None,
            "timezone": schedule.spec.time_zone_name or "UTC",
            "action_count": description.info.num_actions,
            "next_action_times": [
                value.isoformat() for value in description.info.next_action_times[:5]
            ],
        }

    @staticmethod
    def _missing(
        schedule_id: str, cron: str | None, timezone_name: str
    ) -> dict[str, Any]:
        return {
            "schedule_id": schedule_id,
            "exists": False,
            "paused": True,
            "cron": cron,
            "timezone": timezone_name,
            "action_count": 0,
            "next_action_times": [],
        }
