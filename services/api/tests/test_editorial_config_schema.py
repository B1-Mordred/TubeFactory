import pytest
from pydantic import ValidationError

from youtuber_api.schemas import (
    AIModelWrite,
    PromptTemplateWrite,
    ProviderWrite,
    ScriptRegenerationStart,
    TaskModelAssignmentWrite,
    ScriptVersionEdit,
)


def test_fake_provider_is_forced_to_local_credential_free_policy() -> None:
    provider = ProviderWrite(
        slug="fixture-writer",
        name="Fixture writer",
        driver_type="fake",
        location="local",
        data_policy="local_only",
    )
    assert provider.endpoint is None
    with pytest.raises(ValidationError, match="fake providers"):
        ProviderWrite(
            slug="bad-fixture",
            name="Bad fixture",
            driver_type="fake",
            endpoint="https://provider.example",
            location="remote",
            data_policy="remote_allowed",
        )


def test_remote_provider_requires_https_and_mounted_secret_reference() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        ProviderWrite(
            slug="remote-models",
            name="Remote models",
            driver_type="openai_compatible",
            endpoint="http://provider.example/v1",
            location="remote",
            authentication_scheme="bearer",
            secret_reference="provider_api_key",
            data_policy="remote_after_redaction",
        )
    with pytest.raises(ValidationError, match="secret reference"):
        ProviderWrite(
            slug="remote-models",
            name="Remote models",
            driver_type="openai_compatible",
            endpoint="https://provider.example/v1",
            location="remote",
            authentication_scheme="bearer",
            data_policy="remote_after_redaction",
        )


def test_model_assignment_and_prompt_contracts_are_strict() -> None:
    model_id = "21cb77e7-4de6-44f6-8987-a5fe83b84eb9"
    model = AIModelWrite(
        provider_id=model_id,
        model_name="fixture-v1",
        display_name="Fixture v1",
        visible=True,
        enabled=True,
        model_version="1",
        context_limit=4096,
        output_limit=2048,
    )
    assert model.enabled
    with pytest.raises(ValidationError, match="fallback"):
        TaskModelAssignmentWrite(
            task_type="script_writer",
            primary_model_id=model_id,
            fallback_model_ids=[model_id],
            comment="Invalid duplicate routing model",
        )
    with pytest.raises(ValidationError, match="structured_input_json"):
        PromptTemplateWrite(
            template_key="script.writer",
            task_type="script_writer",
            system_instructions="Use only the supplied approved dossier claims.",
            template="Return JSON matching {{response_schema_json}} but omit the required input.",
            input_schema={"type": "object"},
            response_schema={"type": "object"},
            comment="Missing input boundary",
        )


def test_script_editor_rejects_unknown_or_incomplete_segment_documents() -> None:
    with pytest.raises(ValidationError):
        ScriptVersionEdit.model_validate(
            {
                "expected_version": 1,
                "expected_hash": "0" * 64,
                "title": "Edited",
                "segments": [{"narration": "Incomplete."}],
                "comment": "A sufficiently descriptive edit comment.",
                "unexpected": True,
            }
        )


def test_script_regeneration_selection_must_be_unique_and_explicit() -> None:
    value = ScriptRegenerationStart(
        expected_version=2,
        expected_hash="a" * 64,
        segment_keys=["hook", "conclusion"],
        instruction="Make the selected language clearer without adding facts.",
        idempotency_key="regen-selection-1",
    )
    assert value.segment_keys == ["hook", "conclusion"]
    with pytest.raises(ValidationError, match="unique"):
        ScriptRegenerationStart(
            expected_version=2,
            expected_hash="a" * 64,
            segment_keys=["hook", "hook"],
            instruction="Make the selected language clearer without adding facts.",
            idempotency_key="regen-selection-2",
        )
