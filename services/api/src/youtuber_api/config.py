from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(path: str, fallback: str) -> str:
    secret_path = Path(path)
    if secret_path.is_file():
        return secret_path.read_text(encoding="utf-8").strip()
    return fallback


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    app_name: str = "TubeFactory"
    app_environment: str = "development"
    log_level: str = "INFO"
    cookie_secure: bool = False
    database_host: str = "postgres"
    database_port: int = 5432
    database_name: str = "youtuber"
    database_user: str = "youtuber"
    database_password_file: str = "/run/secrets/postgres_password"
    database_password_fallback: str = "development-only-postgres"
    jwt_secret_file: str = "/run/secrets/jwt_secret"
    jwt_secret_fallback: str = "development-only-jwt-secret-change-me"
    jwt_ttl_minutes: int = Field(default=480, ge=5, le=1440)
    minio_endpoint: str = "minio:9000"
    minio_access_key_file: str = "/run/secrets/minio_access_key"
    minio_secret_key_file: str = "/run/secrets/minio_secret_key"
    minio_access_key_fallback: str = "development-minio"
    minio_secret_key_fallback: str = "development-only-minio-secret"
    minio_secure: bool = False
    minio_bucket: str = "production-artifacts"
    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "editorial-production"
    temporal_research_task_queue: str = "editorial-research"
    temporal_editorial_task_queue: str = "editorial-production-v2"
    temporal_publishing_task_queue: str = "youtube-publishing"
    publication_encryption_key_file: str = "/run/secrets/publication_encryption_key"
    publication_encryption_key_fallback: str = ""
    identity_encryption_key_file: str = "/run/secrets/identity_encryption_key"
    identity_encryption_key_fallback: str = ""
    youtube_oauth_client_id_file: str = "/run/secrets/youtube_oauth_client_id"
    youtube_oauth_client_secret_file: str = "/run/secrets/youtube_oauth_client_secret"
    publisher_gateway_endpoint: str = "http://publisher-worker:8084"
    publisher_gateway_token_file: str = "/run/secrets/publisher_gateway_token"
    otel_enabled: bool = False
    otel_exporter_otlp_traces_endpoint: str = "http://otel-collector:4318/v1/traces"
    otel_service_name: str = "youtuber-api"
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]
    auth_rate_limit_failures: int = Field(default=5, ge=2, le=100)
    auth_rate_limit_window_seconds: int = Field(default=900, ge=60, le=86400)
    auth_rate_limit_block_seconds: int = Field(default=900, ge=60, le=86400)

    @property
    def database_password(self) -> str:
        return _read_secret(self.database_password_file, self.database_password_fallback)

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def jwt_secret(self) -> str:
        return _read_secret(self.jwt_secret_file, self.jwt_secret_fallback)

    @property
    def minio_access_key(self) -> str:
        return _read_secret(self.minio_access_key_file, self.minio_access_key_fallback)

    @property
    def minio_secret_key(self) -> str:
        return _read_secret(self.minio_secret_key_file, self.minio_secret_key_fallback)

    @property
    def publication_encryption_key(self) -> str:
        value = _read_secret(self.publication_encryption_key_file, self.publication_encryption_key_fallback)
        if not value:
            raise RuntimeError("publication encryption key is not configured")
        return value

    @property
    def identity_encryption_key(self) -> str:
        value = _read_secret(self.identity_encryption_key_file, self.identity_encryption_key_fallback)
        if not value:
            raise RuntimeError("identity encryption key is not configured")
        return value

    @property
    def youtube_oauth_client_id(self) -> str:
        return _read_secret(self.youtube_oauth_client_id_file, "")

    @property
    def youtube_oauth_client_secret(self) -> str:
        return _read_secret(self.youtube_oauth_client_secret_file, "")

    @property
    def publisher_gateway_token(self) -> str:
        return _read_secret(self.publisher_gateway_token_file, "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
