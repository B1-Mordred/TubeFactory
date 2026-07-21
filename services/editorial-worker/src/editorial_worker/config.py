from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


def _secret(name: str) -> str:
    file_name = os.getenv(f"{name}_FILE")
    if not file_name:
        raise RuntimeError(f"{name}_FILE is required")
    return Path(file_name).read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Settings:
    temporal_address: str = os.getenv("TEMPORAL_ADDRESS", "temporal:7233")
    temporal_namespace: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue: str = os.getenv("EDITORIAL_TASK_QUEUE", "editorial-production-v2")
    health_port: int = int(os.getenv("HEALTH_PORT", "8083"))
    database_host: str = os.getenv("DATABASE_HOST", "postgres")
    database_name: str = os.getenv("DATABASE_NAME", "youtuber")
    database_user: str = os.getenv("DATABASE_USER", "youtuber")
    provider_secret_directory: str = os.getenv(
        "PROVIDER_SECRET_DIRECTORY", "/run/secrets/providers"
    )
    minio_endpoint: str = os.getenv("MINIO_ENDPOINT", "minio:9000")
    minio_bucket: str = os.getenv("MINIO_BUCKET", "production-artifacts")
    render_endpoint: str = os.getenv("RENDER_ENDPOINT", "http://render-service:8091")
    comfyui_endpoint: str = os.getenv("COMFYUI_ENDPOINT", "http://comfyui:8188")

    @property
    def database_dsn(self) -> str:
        password = quote(_secret("DATABASE_PASSWORD"), safe="")
        return (
            f"postgresql://{self.database_user}:{password}@"
            f"{self.database_host}/{self.database_name}"
        )

    @property
    def minio_access_key(self) -> str:
        return _secret("MINIO_ACCESS_KEY")

    @property
    def minio_secret_key(self) -> str:
        return _secret("MINIO_SECRET_KEY")

    def provider_secret(self, reference: str | None) -> str | None:
        if reference is None:
            return None
        if not reference or Path(reference).name != reference:
            raise RuntimeError("provider secret reference is not a safe mounted name")
        directory = Path(self.provider_secret_directory).resolve()
        path = (directory / reference).resolve()
        if path.parent != directory or not path.is_file() or path.is_symlink():
            raise RuntimeError("configured provider secret is unavailable")
        if path.stat().st_size > 16_384:
            raise RuntimeError("configured provider secret exceeds the size limit")
        return path.read_text(encoding="utf-8").strip()
