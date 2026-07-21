from __future__ import annotations

import base64
import hashlib
import secrets

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from editorial_core.publishing import PublishingContractError, validate_granted_scopes
from publisher_worker.config import Settings


app = FastAPI(title="Private publisher gateway", docs_url=None, openapi_url=None)


class ExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=3, max_length=4096)
    redirect_uri: str = Field(min_length=10, max_length=2000)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/oauth/exchange")
async def exchange(
    payload: ExchangeRequest,
    x_publisher_gateway_token: str = Header(default="", alias="X-Publisher-Gateway-Token"),
) -> dict:
    settings = Settings()
    if not secrets.compare_digest(x_publisher_gateway_token, settings.gateway_token):
        raise HTTPException(status_code=401, detail="Invalid gateway credential")
    if not settings.youtube_client_id or not settings.youtube_client_secret:
        raise HTTPException(status_code=503, detail="OAuth client is not configured")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": settings.youtube_client_id, "client_secret": settings.youtube_client_secret,
            "code": payload.code, "grant_type": "authorization_code", "redirect_uri": payload.redirect_uri,
        })
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="Google token exchange failed")
        tokens = response.json()
        refresh_token = tokens.get("refresh_token")
        scopes = str(tokens.get("scope", "")).split()
        try:
            validate_granted_scopes(scopes)
        except PublishingContractError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not refresh_token:
            raise HTTPException(status_code=400, detail="Offline refresh token was not returned")
        channel_response = await client.get(
            "https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        items = channel_response.json().get("items", []) if channel_response.status_code == 200 else []
        if not items:
            raise HTTPException(status_code=502, detail="Authorized YouTube channel could not be resolved")
    channel = items[0]
    encrypted = Fernet(settings.encryption_key.encode()).encrypt(refresh_token.encode())
    return {
        "refresh_token_encrypted": base64.b64encode(encrypted).decode(),
        "token_fingerprint": hashlib.sha256(refresh_token.encode()).hexdigest(),
        "scopes": scopes,
        "youtube_channel_id": channel["id"],
        "youtube_channel_title": channel["snippet"]["title"],
    }
