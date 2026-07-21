from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from youtuber_api.models import Base
from youtuber_api.schemas import AnalyticsSnapshotWrite, ModelBenchmarkWrite, OriginalityReportWrite


def test_increment_five_tables_are_registered() -> None:
    required = {
        "analytics_metric_snapshots", "model_benchmark_runs", "model_recommendations",
        "model_recommendation_decisions", "model_recommendation_applications",
        "source_freshness_checks", "originality_reports", "correction_records",
        "budget_policy_versions", "budget_policy_heads", "budget_usage_records", "operational_evidence",
        "authentication_rate_limits", "oidc_configuration_versions", "oidc_configuration_heads",
        "oidc_authentication_states",
    }
    assert required <= set(Base.metadata.tables)


def test_analytics_schema_rejects_decreasing_period() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="analytics period"):
        AnalyticsSnapshotWrite(
            channel_profile_id=uuid4(), youtube_video_id="video_1",
            period_start=now, period_end=now, metrics={"views": 1},
        )


def test_benchmark_schema_requires_unique_candidates_and_complete_weights() -> None:
    model_id = uuid4()
    candidate = {
        "model_id": model_id, "quality_score": 90, "success_rate": 1,
        "p95_latency_ms": 100, "mean_cost_usd": Decimal("0.01"),
    }
    with pytest.raises(ValidationError, match="must be unique"):
        ModelBenchmarkWrite(
            task_type="script_writer", suite_key="ci-suite", suite_version="1",
            candidates=[candidate, candidate],
        )


def test_originality_schema_requires_ordered_thresholds() -> None:
    with pytest.raises(ValidationError, match="review threshold"):
        OriginalityReportWrite(
            script_version_id=uuid4(), comparisons={"source": 0.2},
            review_threshold=0.5, block_threshold=0.5,
        )
