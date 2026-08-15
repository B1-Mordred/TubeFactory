from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from editorial_core.editorial import StatementAnnotation, StatementKind


_SCENE_HEADER = re.compile(
    r"^S(?P<number>\d{1,3})\s*\|\s*(?P<start>\d{1,2}:\d{2})\s*[–-]\s*"
    r"(?P<end>\d{1,2}:\d{2})\s*\|\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_SECTION_LABELS = {
    "dramaturgische funktion": "dramaturgy",
    "voiceover": "voiceover",
    "bild/schnitt": "visual_direction",
    "bild / schnitt": "visual_direction",
    "on-screen-text": "on_screen_text",
    "on screen text": "on_screen_text",
    "benötigte assets": "assets",
    "benoetigte assets": "assets",
    "evidenz-/freigabehinweis": "approval_note",
    "evidenz-/freigabe-hinweis": "approval_note",
    "evidenz/freigabehinweis": "approval_note",
}
_SECTION_LINE = re.compile(r"^(?P<label>[^:\n]{3,80}):\s*(?P<value>.*)$")
_PLACEHOLDER = re.compile(r"\[[A-ZÄÖÜ0-9][A-ZÄÖÜ0-9_.:-]{1,80}\]")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

_SEGMENT_TYPES = (
    "hook",
    "thesis",
    "context",
    "evidence",
    "evidence",
    "evidence",
    "evidence",
    "evidence",
    "conclusion",
    "uncertainty",
    "counterevidence",
    "call_to_action",
)
_VISUAL_RHYTHM = (
    "title_card",
    "diagram",
    "timeline",
    "text",
    "diagram",
    "timeline",
    "text",
    "diagram",
    "timeline",
    "text",
    "diagram",
    "branded_transition",
)


@dataclass(frozen=True)
class ParsedDirectScene:
    scene_key: str
    order: int
    title: str
    start_seconds: int
    end_seconds: int
    duration_seconds: float
    dramaturgy: str
    voiceover: str
    visual_direction: str
    on_screen_text: tuple[str, ...]
    assets: tuple[str, ...]
    approval_note: str
    placeholder_tokens: tuple[str, ...]
    word_count: int


@dataclass(frozen=True)
class ParsedDirectScriptedVideo:
    title: str
    source_text: str
    scene_count: int
    total_duration_seconds: float
    word_count: int
    target_wpm_min: int
    target_wpm_max: int
    placeholder_tokens: tuple[str, ...]
    scenes: tuple[ParsedDirectScene, ...]

    def preview(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "scene_count": self.scene_count,
            "total_duration_seconds": self.total_duration_seconds,
            "word_count": self.word_count,
            "target_wpm_min": self.target_wpm_min,
            "target_wpm_max": self.target_wpm_max,
            "placeholder_tokens": list(self.placeholder_tokens),
            "scenes": [
                {
                    "scene_key": scene.scene_key,
                    "order": scene.order,
                    "title": scene.title,
                    "duration_seconds": scene.duration_seconds,
                    "word_count": scene.word_count,
                    "placeholder_tokens": list(scene.placeholder_tokens),
                    "on_screen_text": list(scene.on_screen_text),
                    "assets": list(scene.assets),
                    "approval_note": scene.approval_note,
                }
                for scene in self.scenes
            ],
        }


def parse_direct_scripted_video(
    source_text: str,
    *,
    title: str | None = None,
    target_wpm_min: int = 108,
    target_wpm_max: int = 116,
) -> ParsedDirectScriptedVideo:
    text = source_text.strip()
    if len(text) < 200:
        raise ValueError("direct scripted-video input must contain at least 200 characters")
    if target_wpm_min < 60 or target_wpm_max > 220 or target_wpm_min > target_wpm_max:
        raise ValueError("target WPM range is outside the supported narration bounds")

    raw_scenes = _split_scenes(text)
    if not raw_scenes:
        raise ValueError("direct scripted-video input must contain scene headers like S01 | 00:00–00:54 | Title")
    scenes = tuple(_parse_scene(raw, index) for index, raw in enumerate(raw_scenes, 1))
    seen = set()
    for scene in scenes:
        if scene.scene_key in seen:
            raise ValueError(f"duplicate scene key: {scene.scene_key}")
        seen.add(scene.scene_key)
        if not scene.voiceover:
            raise ValueError(f"{scene.scene_key} is missing Voiceover text")
        if scene.end_seconds <= scene.start_seconds:
            raise ValueError(f"{scene.scene_key} has an invalid time range")
    placeholders = tuple(sorted({token for scene in scenes for token in scene.placeholder_tokens}))
    return ParsedDirectScriptedVideo(
        title=(title or scenes[0].title).strip(),
        source_text=text,
        scene_count=len(scenes),
        total_duration_seconds=sum(scene.duration_seconds for scene in scenes),
        word_count=sum(scene.word_count for scene in scenes),
        target_wpm_min=target_wpm_min,
        target_wpm_max=target_wpm_max,
        placeholder_tokens=placeholders,
        scenes=scenes,
    )


def direct_script_draft_document(parsed: ParsedDirectScriptedVideo) -> dict[str, Any]:
    return {
        "title": parsed.title,
        "segments": [
            {
                "segment_key": scene.scene_key,
                "segment_type": _segment_type(scene.order),
                "narration": scene.voiceover,
                "presentation_purpose": _segment_purpose(scene),
                "duration_seconds": scene.duration_seconds,
                "citation_display": {
                    "mode": "not_applicable",
                    "source_kind": "direct_scripted_video",
                    "label": "No evidence checking for this operator-supplied script.",
                },
                "annotations": [
                    {
                        "text": annotation.text,
                        "start_offset": annotation.start_offset,
                        "end_offset": annotation.end_offset,
                        "kind": annotation.kind.value,
                        "claim_ids": [],
                        "evidence_excerpt_id": None,
                    }
                    for annotation in (_full_editorial_annotation(scene.voiceover),)
                ],
                "locked": False,
            }
            for scene in parsed.scenes
        ],
    }


def direct_storyboard_templates(parsed: ParsedDirectScriptedVideo) -> list[dict[str, Any]]:
    return [
        {
            "scene_key": scene.scene_key,
            "segment_key": scene.scene_key,
            "order": scene.order,
            "purpose": _scene_purpose(scene),
            "duration": scene.duration_seconds,
            "claim_ids": [],
            "visual_type": _visual_type(scene),
            "visual_brief": _visual_brief(scene),
            "on_screen_text": list(scene.on_screen_text) or [scene.title],
            "citation_style": "not applicable; direct scripted-video input has no evidence source list",
            "source_ids": [],
            "asset_requests": _asset_requests(scene),
            "transition": "Ruhiger Schnitt mit konsistenter Management-Präsentationsgrafik.",
            "music_sfx_policy": "Dezent, sachlich, ohne dramatisierende Signalwirkung.",
            "synthetic_media_flag": True,
            "accessibility_notes": "Kontrastreiche Darstellung; zentrale Aussagen werden durch Sprechertext oder On-Screen-Text erklärt.",
        }
        for scene in parsed.scenes
    ]


def direct_import_report(parsed: ParsedDirectScriptedVideo) -> dict[str, Any]:
    return {
        "valid": True,
        "deterministic_valid": True,
        "requires_independent_verification": False,
        "evidence_required": False,
        "claim_coverage_applicable": False,
        "mode": "direct_scripted_video",
        "source_kind": "direct_scripted_video",
        "coverage_percent": 0,
        "issues": [],
        "placeholder_tokens": list(parsed.placeholder_tokens),
        "unresolved_placeholders": list(parsed.placeholder_tokens),
        "parser": {
            "scene_count": parsed.scene_count,
            "word_count": parsed.word_count,
            "total_duration_seconds": parsed.total_duration_seconds,
            "target_wpm": [parsed.target_wpm_min, parsed.target_wpm_max],
        },
        "direct_storyboard": {"scenes": direct_storyboard_templates(parsed)},
    }


def _split_scenes(text: str) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if _SCENE_HEADER.match(line.strip()):
            if current:
                chunks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        chunks.append(current)
    return chunks


def _parse_scene(lines: list[str], fallback_order: int) -> ParsedDirectScene:
    header = _SCENE_HEADER.match(lines[0].strip())
    if header is None:
        raise ValueError("invalid scene header")
    order = int(header.group("number") or fallback_order)
    fields: dict[str, list[str]] = {
        "dramaturgy": [],
        "voiceover": [],
        "visual_direction": [],
        "on_screen_text": [],
        "assets": [],
        "approval_note": [],
    }
    current: str | None = None
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        match = _SECTION_LINE.match(line)
        if match:
            label = _normalize_label(match.group("label"))
            mapped = _SECTION_LABELS.get(label)
            if mapped:
                current = mapped
                value = match.group("value").strip()
                if value:
                    fields[current].append(value)
                continue
        if current:
            fields[current].append(line)
    start = _time_to_seconds(header.group("start"))
    end = _time_to_seconds(header.group("end"))
    voiceover = _join_prose(fields["voiceover"])
    combined_text = "\n".join(lines)
    return ParsedDirectScene(
        scene_key=f"s{order:02d}",
        order=order,
        title=header.group("title").strip(),
        start_seconds=start,
        end_seconds=end,
        duration_seconds=float(end - start),
        dramaturgy=_join_prose(fields["dramaturgy"]),
        voiceover=voiceover,
        visual_direction=_join_prose(fields["visual_direction"]),
        on_screen_text=tuple(_split_list(" ".join(fields["on_screen_text"]), separator=r"\s*/\s*")),
        assets=tuple(_split_list(" ".join(fields["assets"]), separator=r"\s*;\s*")),
        approval_note=_join_prose(fields["approval_note"]),
        placeholder_tokens=tuple(sorted(set(_PLACEHOLDER.findall(combined_text)))),
        word_count=len(_WORD.findall(voiceover)),
    )


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().strip())


def _time_to_seconds(value: str) -> int:
    minute, second = value.split(":", 1)
    return int(minute) * 60 + int(second)


def _join_prose(lines: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(line.strip() for line in lines if line.strip())).strip()


def _split_list(value: str, *, separator: str) -> list[str]:
    if not value.strip():
        return []
    parts = re.split(separator, value)
    return [part.strip(" .") for part in parts if part.strip(" .")]


def _segment_type(order: int) -> str:
    if order <= len(_SEGMENT_TYPES):
        return _SEGMENT_TYPES[order - 1]
    return "evidence"


def _visual_type(scene: ParsedDirectScene) -> str:
    if scene.order <= len(_VISUAL_RHYTHM):
        return _VISUAL_RHYTHM[scene.order - 1]
    return "diagram" if scene.order % 2 else "text"


def _segment_purpose(scene: ParsedDirectScene) -> str:
    parts = [scene.title]
    if scene.dramaturgy:
        parts.append(scene.dramaturgy)
    return " — ".join(parts)


def _scene_purpose(scene: ParsedDirectScene) -> str:
    if scene.dramaturgy:
        return f"{scene.title}: {scene.dramaturgy}"
    return scene.title


def _visual_brief(scene: ParsedDirectScene) -> str:
    text = scene.visual_direction or scene.title
    cues = " · ".join(scene.on_screen_text[:3])
    return (
        f"{scene.title}. {text} "
        f"On-Screen-Schwerpunkt: {cues or scene.title}. "
        "Einheitlicher, ruhiger Corporate-Erklärstil; keine dramatisierenden Chaosbilder."
    )


def _asset_requests(scene: ParsedDirectScene) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for asset in scene.assets:
        requests.append({"kind": "required_asset", "description": asset})
    if scene.approval_note:
        requests.append({"kind": "approval_note", "description": scene.approval_note})
    if scene.placeholder_tokens:
        requests.append(
            {
                "kind": "placeholder_blocker",
                "tokens": list(scene.placeholder_tokens),
                "description": "Resolve these placeholders before final media production.",
            }
        )
    return requests


def _full_editorial_annotation(narration: str) -> StatementAnnotation:
    return StatementAnnotation(
        text=narration,
        start_offset=0,
        end_offset=len(narration),
        kind=StatementKind.EDITORIAL,
        claim_ids=(),
        evidence_excerpt_id=None,
    )
