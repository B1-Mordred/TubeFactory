from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from editorial_core.media import TypedWorkflowInput, substitute_workflow_inputs


class ComfyUIError(RuntimeError):
    pass


class ComfyUICapabilityError(ComfyUIError):
    pass


class ComfyUITransientError(ComfyUIError):
    pass


@dataclass(frozen=True)
class ComfyOutput:
    body: bytes
    filename: str
    subfolder: str
    storage_type: str


class ComfyUIClient:
    """Fixed-origin adapter for the documented ComfyUI HTTP execution lifecycle."""

    def __init__(self, endpoint: str, *, timeout_seconds: float = 300) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("ComfyUI endpoint must be a fixed HTTP(S) origin")
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def health_and_capabilities(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.endpoint, timeout=15, follow_redirects=False) as client:
            response = await client.get("/object_info")
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ComfyUICapabilityError("ComfyUI object inventory is invalid")
            return value

    async def validate_inventory(
        self, *, required_nodes: list[dict[str, Any]], required_models: list[dict[str, Any]]
    ) -> dict[str, Any]:
        inventory = await self.health_and_capabilities()
        missing = sorted(item["class_type"] for item in required_nodes if item["class_type"] not in inventory)
        if missing:
            raise ComfyUICapabilityError(f"required ComfyUI nodes are unavailable: {', '.join(missing)}")
        model_names = {item["name"] for item in required_models}
        advertised: set[str] = set()
        for node in inventory.values():
            required = node.get("input", {}).get("required", {}) if isinstance(node, dict) else {}
            for key in ("ckpt_name", "vae_name", "lora_name", "model_name"):
                values = required.get(key, [])
                if isinstance(values, list) and values and isinstance(values[0], list):
                    advertised.update(str(value) for value in values[0])
        unavailable = sorted(model_names - advertised)
        if unavailable:
            raise ComfyUICapabilityError(f"required ComfyUI models are unavailable: {', '.join(unavailable)}")
        return {"node_types": sorted(inventory), "models": sorted(advertised)}

    async def execute(
        self,
        *,
        workflow: Mapping[str, Any],
        typed_inputs: list[TypedWorkflowInput],
        values: Mapping[str, Any],
        client_id: str,
    ) -> list[ComfyOutput]:
        prompt = substitute_workflow_inputs(workflow, typed_inputs, values)
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        async with httpx.AsyncClient(base_url=self.endpoint, timeout=30, follow_redirects=False) as client:
            submitted = await client.post("/prompt", json={"prompt": prompt, "client_id": client_id})
            if submitted.status_code >= 500:
                raise ComfyUITransientError("ComfyUI rejected the prompt temporarily")
            submitted.raise_for_status()
            prompt_id = submitted.json().get("prompt_id")
            if not isinstance(prompt_id, str):
                raise ComfyUIError("ComfyUI response omitted prompt_id")
            try:
                while asyncio.get_running_loop().time() < deadline:
                    history_response = await client.get(f"/history/{prompt_id}")
                    history_response.raise_for_status()
                    history = history_response.json().get(prompt_id)
                    if history:
                        status = history.get("status", {})
                        if status.get("status_str") == "error":
                            raise ComfyUIError("ComfyUI execution failed")
                        references = []
                        for node in history.get("outputs", {}).values():
                            for kind in ("images", "gifs", "videos", "audio"):
                                references.extend(node.get(kind, []))
                        outputs: list[ComfyOutput] = []
                        for reference in references:
                            filename = str(reference.get("filename", ""))
                            subfolder = str(reference.get("subfolder", ""))
                            storage_type = str(reference.get("type", "output"))
                            if not filename or "/" in filename or "\\" in filename:
                                raise ComfyUIError("ComfyUI returned an unsafe output filename")
                            result = await client.get("/view", params={"filename": filename, "subfolder": subfolder, "type": storage_type})
                            result.raise_for_status()
                            outputs.append(ComfyOutput(result.content, filename, subfolder, storage_type))
                        if not outputs:
                            raise ComfyUIError("ComfyUI completed without contracted outputs")
                        return outputs
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                await client.post("/interrupt")
                raise
            await client.post("/interrupt")
            raise ComfyUITransientError("ComfyUI execution timed out")
