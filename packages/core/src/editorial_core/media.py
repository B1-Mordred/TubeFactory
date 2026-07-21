from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NODE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_WORKFLOW_TOP_LEVEL = frozenset({"last_node_id", "last_link_id", "nodes", "links", "groups", "config", "extra", "version"})


class MediaContractError(ValueError):
    pass


class QAVerdict(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class QAOverridePolicy(StrEnum):
    NEVER = "never"
    REASONED = "reasoned"


@dataclass(frozen=True)
class TypedWorkflowInput:
    name: str
    node_id: str
    input_name: str
    value_type: str
    required: bool = True


@dataclass(frozen=True)
class QAFinding:
    code: str
    verdict: QAVerdict
    message: str
    override_policy: QAOverridePolicy = QAOverridePolicy.REASONED


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def validate_comfy_api_workflow(
    document: Mapping[str, Any],
    *,
    typed_inputs: Iterable[TypedWorkflowInput],
    allowed_node_types: Iterable[str],
    allowed_models: Iterable[str],
) -> tuple[str, ...]:
    """Validate a ComfyUI API-format prompt and its explicitly mutable input surface.

    API-format workflows are node-id mappings. UI graph documents have a ``nodes`` array and
    are rejected so they cannot accidentally be sent to ``/prompt``.
    """
    errors: list[str] = []
    if set(document).issubset(_WORKFLOW_TOP_LEVEL) and isinstance(document.get("nodes"), list):
        return ("workflow must be ComfyUI API format, not UI graph format",)
    allow_nodes = frozenset(allowed_node_types)
    allow_models = frozenset(allowed_models)
    if not document:
        errors.append("workflow must contain at least one node")
    for node_id, node in document.items():
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            errors.append("workflow node IDs must be stable safe strings")
            continue
        if not isinstance(node, Mapping):
            errors.append(f"node {node_id} must be an object")
            continue
        class_type = node.get("class_type")
        if class_type not in allow_nodes:
            errors.append(f"node {node_id} uses unapproved type {class_type!r}")
        if not isinstance(node.get("inputs"), Mapping):
            errors.append(f"node {node_id} inputs must be an object")
        for key, value in node.get("inputs", {}).items() if isinstance(node.get("inputs"), Mapping) else ():
            if key in {"ckpt_name", "vae_name", "lora_name", "model_name"} and value not in allow_models:
                errors.append(f"node {node_id} references unapproved model {value!r}")
    seen_names: set[str] = set()
    for spec in typed_inputs:
        if spec.name in seen_names:
            errors.append(f"typed input {spec.name!r} is duplicated")
        seen_names.add(spec.name)
        node = document.get(spec.node_id)
        if not isinstance(node, Mapping) or spec.input_name not in node.get("inputs", {}):
            errors.append(f"typed input {spec.name!r} points to a missing node input")
        if spec.value_type not in {"string", "integer", "number", "boolean", "seed"}:
            errors.append(f"typed input {spec.name!r} has unsupported type")
    return tuple(errors)


def substitute_workflow_inputs(
    document: Mapping[str, Any],
    specs: Iterable[TypedWorkflowInput],
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Deep-copy a workflow and replace only declared, type-checked inputs."""
    copied = json.loads(json.dumps(document))
    by_name = {spec.name: spec for spec in specs}
    unknown = set(values) - set(by_name)
    if unknown:
        raise MediaContractError(f"undeclared workflow inputs: {', '.join(sorted(unknown))}")
    for name, spec in by_name.items():
        if name not in values:
            if spec.required:
                raise MediaContractError(f"required workflow input is missing: {name}")
            continue
        value = values[name]
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "seed": isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**64,
        }.get(spec.value_type, False)
        if not valid:
            raise MediaContractError(f"workflow input {name!r} must be {spec.value_type}")
        copied[spec.node_id]["inputs"][spec.input_name] = value
    return copied


def media_cache_key(*, workflow_hash: str, inputs: Mapping[str, Any], capability_hash: str) -> str:
    if not _SHA256.fullmatch(workflow_hash) or not _SHA256.fullmatch(capability_hash):
        raise MediaContractError("cache provenance must use SHA-256 hashes")
    return canonical_hash({"workflow_hash": workflow_hash, "inputs": inputs, "capability_hash": capability_hash})


def stable_voice_chunks(text: str, *, maximum_characters: int = 500) -> tuple[str, ...]:
    if maximum_characters < 8:
        raise MediaContractError("maximum chunk size is too small")
    words = text.split()
    if not words:
        raise MediaContractError("voice text must not be empty")
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) > maximum_characters and current:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    if any(len(chunk) > maximum_characters for chunk in chunks):
        raise MediaContractError("a single voice token exceeds the provider chunk limit")
    return tuple(chunks)


def effective_qa_verdict(
    findings: Iterable[QAFinding], *, overridden_codes: Iterable[str] = ()
) -> QAVerdict:
    overrides = frozenset(overridden_codes)
    values = tuple(findings)
    unknown = overrides - {finding.code for finding in values}
    if unknown:
        raise MediaContractError("override references an unknown QA finding")
    for finding in values:
        if finding.code in overrides and finding.override_policy is QAOverridePolicy.NEVER:
            raise MediaContractError(f"finding {finding.code!r} cannot be overridden")
    active = [finding for finding in values if finding.code not in overrides]
    if any(finding.verdict is QAVerdict.FAIL for finding in active):
        return QAVerdict.FAIL
    if any(finding.verdict is QAVerdict.WARN for finding in active):
        return QAVerdict.WARN
    return QAVerdict.PASS


def render_is_approvable(
    *,
    storyboard_hash: str,
    bound_storyboard_hash: str,
    manifest_hash: str,
    bound_manifest_hash: str,
    qa_verdict: QAVerdict,
) -> bool:
    return (
        _SHA256.fullmatch(storyboard_hash) is not None
        and storyboard_hash == bound_storyboard_hash
        and _SHA256.fullmatch(manifest_hash) is not None
        and manifest_hash == bound_manifest_hash
        and qa_verdict is not QAVerdict.FAIL
    )
