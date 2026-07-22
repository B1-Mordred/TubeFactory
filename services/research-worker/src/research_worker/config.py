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
    task_queue: str = os.getenv("RESEARCH_TASK_QUEUE", "editorial-research")
    health_port: int = int(os.getenv("HEALTH_PORT", "8082"))
    database_host: str = os.getenv("DATABASE_HOST", "postgres")
    database_name: str = os.getenv("DATABASE_NAME", "youtuber")
    database_user: str = os.getenv("DATABASE_USER", "youtuber")
    minio_endpoint: str = os.getenv("MINIO_ENDPOINT", "minio:9000")
    minio_bucket: str = os.getenv("MINIO_BUCKET", "production-artifacts")
    searxng_endpoint: str = os.getenv("SEARXNG_ENDPOINT", "http://searxng:8080")
    searxng_general_engines: str = os.getenv(
        "SEARXNG_GENERAL_ENGINES", "bing,qwant news,bing news"
    )
    searxng_science_engines: str = os.getenv(
        "SEARXNG_SCIENCE_ENGINES",
        "bing,arxiv,pubmed",
    )
    openalex_endpoint: str = os.getenv(
        "OPENALEX_ENDPOINT", "https://api.openalex.org"
    )
    openalex_mailto: str = os.getenv("OPENALEX_MAILTO", "")
    firecrawl_endpoint: str = os.getenv("FIRECRAWL_ENDPOINT", "")
    acquisition_user_agent: str = os.getenv(
        "ACQUISITION_USER_AGENT", "EvidenceStudioResearch/0.1 (+self-hosted editorial research)"
    )
    acquisition_max_bytes: int = int(os.getenv("ACQUISITION_MAX_BYTES", "10000000"))
    acquisition_min_domain_interval_seconds: float = float(
        os.getenv("ACQUISITION_MIN_DOMAIN_INTERVAL_SECONDS", "1.0")
    )

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
