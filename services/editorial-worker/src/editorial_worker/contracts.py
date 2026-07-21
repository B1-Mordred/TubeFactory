from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Annotation(StrictModel):
    text: str = Field(min_length=1, max_length=20_000)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    kind: Literal["fact", "inference", "opinion", "quote", "editorial"]
    claim_ids: list[UUID] = Field(default_factory=list, max_length=100)
    evidence_excerpt_id: UUID | None = None

    @model_validator(mode="after")
    def ordered_offsets(self) -> Annotation:
        if self.end_offset <= self.start_offset:
            raise ValueError("annotation end offset must follow its start offset")
        return self


class ScriptSegment(StrictModel):
    segment_key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")
    segment_type: Literal[
        "hook", "thesis", "context", "evidence", "counterevidence",
        "conclusion", "uncertainty", "call_to_action",
    ]
    narration: str = Field(min_length=1, max_length=20_000)
    presentation_purpose: str = Field(min_length=1, max_length=2_000)
    duration_seconds: float = Field(gt=0, le=900)
    citation_display: dict[str, Any]
    annotations: list[Annotation] = Field(min_length=1, max_length=200)
    locked: bool = False


class ScriptDraft(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    segments: list[ScriptSegment] = Field(min_length=8, max_length=200)


class VerificationIssue(StrictModel):
    code: str = Field(min_length=1, max_length=120)
    severity: Literal["info", "warning", "error"]
    segment_key: str | None = None
    statement: str | None = None
    message: str = Field(min_length=1, max_length=4_000)


class VerifierOutput(StrictModel):
    valid: bool
    coverage_percent: int = Field(ge=0, le=100)
    issues: list[VerificationIssue] = Field(default_factory=list, max_length=1_000)


class StoryboardDraft(StrictModel):
    scenes: list[dict[str, Any]] = Field(min_length=1, max_length=500)
