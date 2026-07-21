from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator

import httpx


class YouTubeProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    resource: dict[str, Any]


class FakeYouTubeProvider:
    """Stateful deterministic provider used by unit/acceptance tests and dry-runs."""

    def __init__(self) -> None:
        self.videos: dict[str, dict[str, Any]] = {}
        self.uploads_created = 0
        self.schedules_created = 0

    async def upload_private(self, *, idempotency_key: str, body: dict[str, Any], **_: Any) -> UploadResult:
        video_id = "dry_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:11]
        if video_id not in self.videos:
            self.uploads_created += 1
            self.videos[video_id] = {
                "id": video_id, "snippet": body["snippet"],
                "status": {**body["status"], "uploadStatus": "processed"},
                "processingDetails": {"processingStatus": "succeeded"},
                "captions": [], "thumbnail": False,
            }
        return UploadResult(video_id, self.videos[video_id])

    async def upload_caption(self, video_id: str, language: str, name: str, body: bytes, mime: str) -> dict[str, Any]:
        track_id = "caption_" + hashlib.sha256(video_id.encode() + body).hexdigest()[:12]
        tracks = self.videos[video_id]["captions"]
        if track_id not in {item["id"] for item in tracks}:
            tracks.append({"id": track_id, "language": language, "name": name, "mime": mime})
        return tracks[-1]

    async def set_thumbnail(self, video_id: str, body: bytes, mime: str) -> dict[str, Any]:
        self.videos[video_id]["thumbnail"] = True
        return {"video_id": video_id, "mime": mime, "sha256": hashlib.sha256(body).hexdigest()}

    async def reconcile(self, video_id: str) -> dict[str, Any]:
        return self.videos[video_id]

    async def schedule(self, video_id: str, publish_at: datetime) -> dict[str, Any]:
        current = self.videos[video_id]["status"].get("publishAt")
        value = publish_at.isoformat().replace("+00:00", "Z")
        if current != value:
            self.schedules_created += 1
            self.videos[video_id]["status"].update({"privacyStatus": "private", "publishAt": value})
        return self.videos[video_id]


class YouTubeAPI:
    def __init__(self, access_token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._token = access_token
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, read=900))
        self._owns_client = client is None

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    async def refresh_token(refresh_token: str, client_id: str, client_secret: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": refresh_token, "grant_type": "refresh_token",
            })
        if response.status_code != 200:
            raise YouTubeProviderError(f"OAuth refresh failed with HTTP {response.status_code}")
        return str(response.json()["access_token"])

    async def initiate_resumable(self, body: dict[str, Any], *, byte_size: int, mime: str) -> str:
        response = await self._client.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={**self.auth, "Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(byte_size), "X-Upload-Content-Type": mime},
            json=body,
        )
        location = response.headers.get("Location")
        if response.status_code not in {200, 201} or not location:
            raise YouTubeProviderError(f"resumable initiation failed with HTTP {response.status_code}")
        return location

    @staticmethod
    def _offset(response: httpx.Response) -> int:
        match = re.fullmatch(r"bytes=0-(\d+)", response.headers.get("Range", ""))
        return int(match.group(1)) + 1 if match else 0

    async def query_upload(self, session_url: str, byte_size: int) -> tuple[int, UploadResult | None]:
        response = await self._client.put(session_url, headers={**self.auth, "Content-Length": "0", "Content-Range": f"bytes */{byte_size}"}, content=b"")
        if response.status_code in {200, 201}:
            resource = response.json()
            return byte_size, UploadResult(resource["id"], resource)
        if response.status_code == 308:
            return self._offset(response), None
        if response.status_code == 404:
            raise YouTubeProviderError("resumable session expired before completion")
        raise YouTubeProviderError(f"resumable status failed with HTTP {response.status_code}")

    async def upload_from_url(self, session_url: str, source_url: str, *, byte_size: int, mime: str) -> UploadResult:
        offset, completed = await self.query_upload(session_url, byte_size)
        if completed:
            return completed
        while offset < byte_size:
            source_headers = {"Range": f"bytes={offset}-{byte_size - 1}"} if offset else {}
            async with self._client.stream("GET", source_url, headers=source_headers) as source:
                source.raise_for_status()
                response = await self._client.put(
                    session_url,
                    headers={**self.auth, "Content-Type": mime, "Content-Length": str(byte_size - offset), "Content-Range": f"bytes {offset}-{byte_size - 1}/{byte_size}"},
                    content=source.aiter_bytes(),
                )
            if response.status_code in {200, 201}:
                resource = response.json()
                return UploadResult(resource["id"], resource)
            if response.status_code == 308:
                next_offset = self._offset(response)
                if next_offset <= offset:
                    raise YouTubeProviderError("resumable upload made no progress")
                offset = next_offset
                continue
            if response.status_code in {500, 502, 503, 504}:
                offset, completed = await self.query_upload(session_url, byte_size)
                if completed:
                    return completed
                continue
            raise YouTubeProviderError(f"video upload failed with HTTP {response.status_code}")
        _, completed = await self.query_upload(session_url, byte_size)
        if completed is None:
            raise YouTubeProviderError("upload reached final byte without a completion resource")
        return completed

    async def _multipart(self, endpoint: str, metadata: dict[str, Any], body: bytes, mime: str, params: dict[str, str]) -> dict[str, Any]:
        boundary = "evidence-studio-" + hashlib.sha256(body).hexdigest()[:24]
        payload = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata, separators=(",", ":")).encode()
            + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
            + body + f"\r\n--{boundary}--\r\n".encode()
        )
        response = await self._client.post(endpoint, params={"uploadType": "multipart", **params}, headers={**self.auth, "Content-Type": f"multipart/related; boundary={boundary}"}, content=payload)
        if response.status_code == 409:
            return {"id": None, "status": "already_exists"}
        if response.status_code not in {200, 201}:
            raise YouTubeProviderError(f"media attachment failed with HTTP {response.status_code}")
        return response.json()

    async def upload_caption(self, video_id: str, language: str, name: str, body: bytes, mime: str) -> dict[str, Any]:
        return await self._multipart(
            "https://www.googleapis.com/upload/youtube/v3/captions",
            {"snippet": {"videoId": video_id, "language": language, "name": name, "isDraft": False}},
            body, mime, {"part": "snippet"},
        )

    async def set_thumbnail(self, video_id: str, body: bytes, mime: str) -> dict[str, Any]:
        response = await self._client.post(
            "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
            params={"videoId": video_id, "uploadType": "media"}, headers={**self.auth, "Content-Type": mime}, content=body,
        )
        if response.status_code != 200:
            raise YouTubeProviderError(f"thumbnail upload failed with HTTP {response.status_code}")
        return response.json()

    async def reconcile(self, video_id: str) -> dict[str, Any]:
        response = await self._client.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "status,processingDetails", "id": video_id}, headers=self.auth)
        if response.status_code != 200 or not response.json().get("items"):
            raise YouTubeProviderError(f"video reconciliation failed with HTTP {response.status_code}")
        return response.json()["items"][0]

    async def schedule(
        self, video_id: str, publish_at: datetime, current_status: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        preserved = {
            key: current_status[key]
            for key in (
                "embeddable", "license", "publicStatsViewable",
                "selfDeclaredMadeForKids", "containsSyntheticMedia",
            )
            if current_status is not None and key in current_status
        }
        status = {
            **preserved,
            "privacyStatus": "private",
            "publishAt": publish_at.isoformat().replace("+00:00", "Z"),
        }
        response = await self._client.put(
            "https://www.googleapis.com/youtube/v3/videos", params={"part": "status"}, headers=self.auth,
            json={"id": video_id, "status": status},
        )
        if response.status_code != 200:
            raise YouTubeProviderError(f"video scheduling failed with HTTP {response.status_code}")
        return response.json()
