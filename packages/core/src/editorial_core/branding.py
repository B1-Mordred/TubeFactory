from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "logo_text",
        "tagline",
        "primary",
        "accent",
        "background",
        "surface",
        "text",
        "muted_text",
        "heading_font",
        "body_font",
        "corner_style",
        "motion_style",
        "image_treatment",
        "visual_style",
    }
)
_FONT_FAMILIES = frozenset({"sans", "serif", "rounded", "mono"})
_CORNER_STYLES = frozenset({"square", "soft", "rounded"})
_MOTION_STYLES = frozenset({"still", "calm", "dynamic"})
_IMAGE_TREATMENTS = frozenset({"clean", "editorial", "documentary", "cinematic"})


def _initials(channel_name: str) -> str:
    # Treat CamelCase Channel names like FaktischSimpel as two words as well.
    separated = re.sub(r"(?<=[a-zà-öø-ÿ0-9])(?=[A-ZÀ-ÖØ-Þ])", " ", channel_name)
    words = [word for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", separated) if word]
    if not words:
        return "TF"
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(word[0] for word in words[:2]).upper()


def _text(value: Any, *, name: str, maximum: int, default: str = "") -> str:
    result = default if value is None else str(value).strip()
    if len(result) > maximum:
        raise ValueError(f"brand_kit.{name} must contain at most {maximum} characters")
    if any(ord(character) < 32 and character not in "\n\t" for character in result):
        raise ValueError(f"brand_kit.{name} contains control characters")
    return result


def _color(value: Any, *, name: str, default: str) -> str:
    result = default if value is None else str(value).strip()
    if not _HEX_COLOR.fullmatch(result):
        raise ValueError(f"brand_kit.{name} must be a six-digit hexadecimal color")
    return result.upper()


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def normalize_brand_kit(value: Mapping[str, Any] | None, *, channel_name: str = "TubeFactory") -> dict[str, Any]:
    """Return the safe, complete Channel Brand Kit consumed by UI and render workers."""

    raw = dict(value or {})
    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"unknown brand_kit fields: {', '.join(unknown)}")
    schema_version = raw.get("schema_version", 1)
    if schema_version != 1:
        raise ValueError("brand_kit.schema_version must be 1")

    normalized = {
        "schema_version": 1,
        "logo_text": _text(raw.get("logo_text"), name="logo_text", maximum=12, default=_initials(channel_name)),
        "tagline": _text(raw.get("tagline"), name="tagline", maximum=160),
        "primary": _color(raw.get("primary"), name="primary", default="#174C3C"),
        "accent": _color(raw.get("accent"), name="accent", default="#D6A43A"),
        "background": _color(raw.get("background"), name="background", default="#F6F3EA"),
        "surface": _color(raw.get("surface"), name="surface", default="#FFFFFF"),
        "text": _color(raw.get("text"), name="text", default="#18201D"),
        "muted_text": _color(raw.get("muted_text"), name="muted_text", default="#56615C"),
        "heading_font": _text(raw.get("heading_font"), name="heading_font", maximum=20, default="serif"),
        "body_font": _text(raw.get("body_font"), name="body_font", maximum=20, default="sans"),
        "corner_style": _text(raw.get("corner_style"), name="corner_style", maximum=20, default="soft"),
        "motion_style": _text(raw.get("motion_style"), name="motion_style", maximum=20, default="calm"),
        "image_treatment": _text(raw.get("image_treatment"), name="image_treatment", maximum=20, default="editorial"),
        "visual_style": _text(
            raw.get("visual_style"),
            name="visual_style",
            maximum=1_000,
            default="Calm, clear and explanatory; visuals support the narration instead of decorating it.",
        ),
    }
    if not normalized["logo_text"]:
        raise ValueError("brand_kit.logo_text must not be empty")
    if not normalized["visual_style"]:
        raise ValueError("brand_kit.visual_style must not be empty")
    choices = (
        ("heading_font", _FONT_FAMILIES),
        ("body_font", _FONT_FAMILIES),
        ("corner_style", _CORNER_STYLES),
        ("motion_style", _MOTION_STYLES),
        ("image_treatment", _IMAGE_TREATMENTS),
    )
    for name, allowed in choices:
        if normalized[name] not in allowed:
            raise ValueError(f"brand_kit.{name} must be one of: {', '.join(sorted(allowed))}")

    contrast_pairs = (
        ("text", "background", 4.5),
        ("text", "surface", 4.5),
        ("muted_text", "background", 4.5),
        ("surface", "primary", 3.0),
    )
    failures = [
        f"{foreground}/{background} {contrast_ratio(normalized[foreground], normalized[background]):.2f}:1"
        for foreground, background, minimum in contrast_pairs
        if contrast_ratio(normalized[foreground], normalized[background]) < minimum
    ]
    if failures:
        raise ValueError("brand_kit colors must meet WCAG AA contrast: " + "; ".join(failures))
    return normalized
