from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


def _secret(name: str, *, required: bool = True) -> str:
    file_name = os.getenv(f"{name}_FILE")
    if not file_name:
        if required:
            raise RuntimeError(f"{name}_FILE is required")
        return ""
    path = Path(file_name)
    if not path.is_file() and not required:
        return ""
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Settings:
    temporal_address: str = os.getenv("TEMPORAL_ADDRESS", "temporal:7233")
    temporal_namespace: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue: str = os.getenv("PUBLISHING_TASK_QUEUE", "youtube-publishing")
    health_port: int = int(os.getenv("HEALTH_PORT", "8084"))
    database_host: str = os.getenv("DATABASE_HOST", "postgres")
    database_name: str = os.getenv("DATABASE_NAME", "youtuber")
    database_user: str = os.getenv("DATABASE_USER", "youtuber")
    minio_endpoint: str = os.getenv("MINIO_ENDPOINT", "minio:9000")
    minio_bucket: str = os.getenv("MINIO_BUCKET", "production-artifacts")

    @property
    def database_dsn(self) -> str:
        password = quote(_secret("DATABASE_PASSWORD"), safe="")
        return f"postgresql://{self.database_user}:{password}@{self.database_host}/{self.database_name}"

    @property
    def minio_access_key(self) -> str:
        return _secret("MINIO_ACCESS_KEY")

    @property
    def minio_secret_key(self) -> str:
        return _secret("MINIO_SECRET_KEY")

    @property
    def encryption_key(self) -> str:
        return _secret("PUBLICATION_ENCRYPTION_KEY")

    @property
    def youtube_client_id(self) -> str:
        return _secret("YOUTUBE_OAUTH_CLIENT_ID", required=False)

    @property
    def youtube_client_secret(self) -> str:
        return _secret("YOUTUBE_OAUTH_CLIENT_SECRET", required=False)

    @property
    def gateway_token(self) -> str:
        return _secret("PUBLISHER_GATEWAY_TOKEN")
