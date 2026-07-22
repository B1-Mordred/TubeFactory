import pytest

from editorial_worker.provider_activities import _discovery_url, _model_names


def test_openai_compatible_model_inventory_is_normalized_and_sorted() -> None:
    assert _discovery_url("openai_compatible", "http://ai.example/v1/") == (
        "http://ai.example/v1/models"
    )
    assert _model_names(
        "openai_compatible",
        {"data": [{"id": "gemma4-e4b:64k"}, {"id": "Alpha"}, {"id": "Alpha"}]},
    ) == ["Alpha", "gemma4-e4b:64k"]


def test_ollama_inventory_and_unsupported_driver_are_explicit() -> None:
    assert _discovery_url("ollama", "http://ollama:11434") == (
        "http://ollama:11434/api/tags"
    )
    assert _model_names("ollama", {"models": [{"name": "qwen3:latest"}]}) == [
        "qwen3:latest"
    ]
    with pytest.raises(ValueError, match="does not expose"):
        _discovery_url("anthropic", "https://api.example/v1")
