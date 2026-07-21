from datetime import timezone

from editorial_core.discovery import SearchFinding
from research_worker.live_activities import (
    _finding_signature,
    _matches_processed_claim,
    _publication_at,
)


def test_publication_at_normalizes_searxng_timestamp() -> None:
    parsed = _publication_at("2026-07-21T08:00:00Z")

    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-07-21T08:00:00+00:00"


def test_publication_at_ignores_unparseable_values() -> None:
    assert _publication_at("recently") is None


def test_processed_claim_matches_normalized_title_and_summary() -> None:
    signatures = (_finding_signature("Behauptung widerlegt", "Die Quelle korrigiert den Bericht."),)

    assert _matches_processed_claim(
        SearchFinding(
            "https://example.test/update",
            "  BEHAUPTUNG   widerlegt ",
            "Die Quelle korrigiert den Bericht.",
        ),
        signatures,
    )


def test_processed_claim_matches_high_similarity_across_urls() -> None:
    signatures = (
        _finding_signature(
            "Falsche Behauptung zur Energieversorgung widerlegt",
            "Bundesnetzagentur nennt aktuelle Zahlen und korrigiert die verbreitete Darstellung.",
        ),
    )

    assert _matches_processed_claim(
        SearchFinding(
            "https://second.example.test/syndicated",
            "Falsche Behauptung zur Energieversorgung klar widerlegt",
            "Bundesnetzagentur nennt aktuelle Zahlen und korrigiert die verbreitete Darstellung.",
        ),
        signatures,
    )


def test_materially_different_update_is_not_suppressed() -> None:
    signatures = (
        _finding_signature(
            "Behauptung zur Energieversorgung widerlegt",
            "Bundesnetzagentur korrigiert den ursprünglichen Bericht.",
        ),
    )

    assert not _matches_processed_claim(
        SearchFinding(
            "https://example.test/material-update",
            "Neue Studie verändert Bewertung der Energieversorgung",
            "Unabhängige Forschende veröffentlichen neue Messdaten und eine abweichende Zeitreihe.",
        ),
        signatures,
    )
