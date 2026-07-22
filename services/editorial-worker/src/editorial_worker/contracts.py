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


class ScriptContentSentence(StrictModel):
    text: str = Field(min_length=1, max_length=4_000)
    kind: Literal["fact", "inference", "editorial"]
    claim_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_excerpt_ids: list[str] = Field(default_factory=list, max_length=100)


class ScriptContentSegment(StrictModel):
    """Small model-facing contract; mechanical production fields are added locally."""

    segment_type: Literal[
        "hook", "thesis", "context", "evidence", "counterevidence",
        "conclusion", "uncertainty", "call_to_action",
    ]
    narration: str | None = Field(default=None, min_length=1, max_length=20_000)
    presentation_purpose: str = Field(min_length=1, max_length=2_000)
    claim_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_excerpt_ids: list[str] = Field(default_factory=list, max_length=100)
    sentences: list[ScriptContentSentence] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def one_content_form(self) -> ScriptContentSegment:
        if bool(self.narration) == bool(self.sentences):
            raise ValueError("provide either legacy narration or sentence-level content")
        return self


class ScriptContentDraft(StrictModel):
    """Semantic script content returned by the model before deterministic assembly."""

    title: str | None = Field(default=None, max_length=300)
    segments: list[ScriptContentSegment] = Field(min_length=8, max_length=200)


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


class StoryboardContentScene(StrictModel):
    """Creative scene choices returned by the model before local assembly."""

    narration_segment_id: UUID
    purpose: str = Field(min_length=1, max_length=2_000)
    visual_type: Literal[
        "title_card", "citation_card", "text", "diagram", "timeline", "chart",
        "source_screenshot", "licensed_media", "comfyui_image", "comfyui_video",
        "waveform", "branded_transition",
    ]
    visual_brief: str = Field(min_length=1, max_length=4_000)
    on_screen_text: list[str] = Field(default_factory=list, max_length=20)


class StoryboardContentDraft(StrictModel):
    scenes: list[StoryboardContentScene] = Field(min_length=1, max_length=500)
