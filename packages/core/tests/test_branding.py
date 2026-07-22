import pytest

from editorial_core.branding import contrast_ratio, normalize_brand_kit


def test_brand_kit_expands_safe_channel_defaults():
    kit = normalize_brand_kit({}, channel_name="Faktisch Simpel")
    assert kit["schema_version"] == 1
    assert kit["logo_text"] == "FS"
    assert kit["heading_font"] == "serif"
    assert contrast_ratio(kit["text"], kit["background"]) >= 4.5


def test_brand_kit_derives_camel_case_channel_initials():
    assert normalize_brand_kit({}, channel_name="FaktischSimpel")["logo_text"] == "FS"
    assert normalize_brand_kit({}, channel_name="FakeBuster")["logo_text"] == "FB"


def test_brand_kit_preserves_supported_legacy_fields_and_normalizes_colors():
    kit = normalize_brand_kit(
        {
            "primary": "#174c3c",
            "accent": "#d6a43a",
            "background": "#f6f3ea",
            "visual_style": "Ruhig und klar.",
        },
        channel_name="FaktischSimpel",
    )
    assert kit["primary"] == "#174C3C"
    assert kit["visual_style"] == "Ruhig und klar."


def test_brand_kit_rejects_css_injection_unknown_fields_and_low_contrast():
    with pytest.raises(ValueError, match="hexadecimal"):
        normalize_brand_kit({"primary": "url(evil)"})
    with pytest.raises(ValueError, match="unknown"):
        normalize_brand_kit({"remote_font_url": "https://example.test/font.woff2"})
    with pytest.raises(ValueError, match="WCAG AA"):
        normalize_brand_kit({"text": "#FFFFFF", "background": "#FFFFFF"})
