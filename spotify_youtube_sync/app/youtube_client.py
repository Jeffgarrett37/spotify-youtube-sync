"""YouTube Data API v3 client.

Wraps ``google-api-python-client``. Every quota-bearing call is metered through
a :class:`~app.quota.QuotaTracker` *before* it is issued so the engine can stop
cleanly the moment the day's budget is exhausted.

Verified quota costs (Google docs, 2026):
    search.list           100 units
    videos.list             1 unit   (any number of ids up to 50)
    playlistItems.list      1 unit
    playlistItems.insert   50 units
    playlistItems.update   50 units
    playlistItems.delete   50 units
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import google.auth.transport.requests
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .errors import (
    QuotaExceededError,
    RateLimitedError,
    YouTubeAuthError,
    YouTubeUnavailableError,
)
from .logging_setup import get_logger
from .models import YouTubeVideo
from .quota import QuotaTracker

log = get_logger("youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
_ISO_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def parse_iso8601_duration(text: str) -> float:
    m = _ISO_DURATION.fullmatch(text or "")
    if not m:
        return 0.0
    parts = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


class _TransientYouTubeError(Exception):
    pass


def _classify_http_error(exc: HttpError) -> Exception:
    status = getattr(exc.resp, "status", None)
    reason = ""
    try:
        body = json.loads(exc.content.decode("utf-8"))
        errors = body.get("error", {}).get("errors", [])
        if errors:
            reason = errors[0].get("reason", "")
        reason = reason or body.get("error", {}).get("status", "")
    except Exception:  # pragma: no cover - defensive
        pass

    if status == 403 and reason in ("quotaExceeded", "dailyLimitExceeded"):
        return QuotaExceededError(f"YouTube quota exceeded server-side ({reason})")
    if status == 403 and reason in (
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "servingLimitExceeded",
    ):
        return RateLimitedError(f"YouTube rate limited ({reason})", retry_after=5)
    if status in (500, 502, 503, 504):
        return _TransientYouTubeError(f"YouTube {status}")
    if status == 401:
        return YouTubeAuthError("YouTube credentials rejected (401)")
    return YouTubeUnavailableError(f"YouTube API error {status}: {reason or exc}")


class YouTubeClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
        quota: QuotaTracker,
        region_code: str = "US",
        service: Any | None = None,
    ) -> None:
        self._token_path = token_path
        self._client_id = client_id
        self._client_secret = client_secret
        self._quota = quota
        self._region_code = region_code
        self._service = service or self._build_service()

    # --- auth --------------------------------------------------------- #
    def _load_credentials(self) -> Credentials:
        if not self._token_path.exists():
            raise YouTubeAuthError(
                f"YouTube token file missing at {self._token_path}. Run the "
                "authorization helper (see docs/GOOGLE.md)."
            )
        try:
            data = json.loads(self._token_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise YouTubeAuthError(f"cannot read YouTube token file: {exc}") from exc
        if not data.get("refresh_token"):
            raise YouTubeAuthError("YouTube token file has no refresh_token")
        return Credentials(
            token=data.get("token"),
            refresh_token=data["refresh_token"],
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id") or self._client_id,
            client_secret=data.get("client_secret") or self._client_secret,
            scopes=data.get("scopes", SCOPES),
        )

    def _save_credentials(self, creds: Credentials) -> None:
        record = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        }
        tmp = self._token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(self._token_path)
        try:
            self._token_path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass

    def _build_service(self) -> Any:
        creds = self._load_credentials()
        if not creds.valid:
            try:
                creds.refresh(google.auth.transport.requests.Request())
            except RefreshError as exc:
                raise YouTubeAuthError(
                    "YouTube refresh token rejected (revoked/expired). Re-run the "
                    "authorization helper."
                ) from exc
            self._save_credentials(creds)
            log.info("Refreshed YouTube access token")
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    # --- request plumbing ------------------------------------------- #
    @retry(
        retry=retry_if_exception_type((_TransientYouTubeError, RateLimitedError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=40),
        reraise=True,
    )
    def _execute(self, request: Any, method: str, *, units: int | None = None) -> dict[str, Any]:
        # Meter first: raises QuotaExceededError (clean stop) if unaffordable.
        self._quota.require(method, units=units)
        try:
            result = request.execute(num_retries=0)
        except HttpError as exc:
            err = _classify_http_error(exc)
            if isinstance(err, RateLimitedError):
                time.sleep(min(err.retry_after or 5, 30))
            raise err from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise _TransientYouTubeError(f"network error: {exc}") from exc
        # Only charge quota once the call actually succeeded.
        self._quota.charge(method, units=units)
        return result

    # --- reads ---------------------------------------------------- #
    def list_playlist_items(self, playlist_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            req = self._service.playlistItems().list(
                part="snippet,contentDetails,status",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            )
            try:
                resp = self._execute(req, "playlistItems.list")
            except YouTubeUnavailableError as exc:
                if "404" in str(exc):
                    from .errors import PlaylistUnavailableError

                    raise PlaylistUnavailableError(
                        f"YouTube playlist {playlist_id} not found or not accessible"
                    ) from exc
                raise
            for it in resp.get("items", []):
                snip = it.get("snippet", {})
                cd = it.get("contentDetails", {})
                status = it.get("status", {})
                items.append(
                    {
                        "playlist_item_id": it.get("id"),
                        "video_id": (snip.get("resourceId") or {}).get("videoId")
                        or cd.get("videoId"),
                        "position": snip.get("position"),
                        "title": snip.get("title", ""),
                        "channel": snip.get("videoOwnerChannelTitle")
                        or snip.get("channelTitle", ""),
                        "privacy_status": status.get("privacyStatus", ""),
                    }
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        log.debug("YouTube playlist %s has %d items", playlist_id, len(items))
        return items

    def search_videos(self, query: str, max_results: int = 8) -> list[str]:
        req = self._service.search().list(
            part="snippet",
            q=query,
            type="video",
            maxResults=max(1, min(max_results, 25)),
            videoEmbeddable="true",
            regionCode=self._region_code,
            safeSearch="none",
        )
        resp = self._execute(req, "search.list")
        ids = [
            it["id"]["videoId"]
            for it in resp.get("items", [])
            if it.get("id", {}).get("videoId")
        ]
        return ids

    def get_videos(self, video_ids: list[str]) -> dict[str, YouTubeVideo]:
        out: dict[str, YouTubeVideo] = {}
        unique = [v for v in dict.fromkeys(video_ids) if v]
        for start in range(0, len(unique), 50):
            batch = unique[start : start + 50]
            req = self._service.videos().list(
                part="snippet,contentDetails,status", id=",".join(batch)
            )
            resp = self._execute(req, "videos.list")
            for it in resp.get("items", []):
                snip = it.get("snippet", {})
                cd = it.get("contentDetails", {})
                status = it.get("status", {})
                vid = it.get("id")
                if not vid:
                    continue
                out[vid] = YouTubeVideo(
                    video_id=vid,
                    title=snip.get("title", ""),
                    channel_title=snip.get("channelTitle", ""),
                    channel_id=snip.get("channelId"),
                    description=snip.get("description", ""),
                    duration_s=parse_iso8601_duration(cd.get("duration", "")) or None,
                    published_at=snip.get("publishedAt"),
                    is_available=status.get("uploadStatus") != "deleted",
                )
        return out

    def video_exists(self, video_id: str) -> bool:
        return video_id in self.get_videos([video_id])

    # --- writes ------------------------------------------------- #
    def insert_playlist_item(
        self, playlist_id: str, video_id: str, position: int | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }
        if position is not None:
            body["snippet"]["position"] = position
        req = self._service.playlistItems().insert(part="snippet", body=body)
        try:
            resp = self._execute(req, "playlistItems.insert")
        except YouTubeUnavailableError as exc:
            if position is not None and "manualSortRequired" in str(exc):
                log.warning(
                    "playlist not in manual-sort mode; inserting without position"
                )
                body["snippet"].pop("position", None)
                req = self._service.playlistItems().insert(part="snippet", body=body)
                resp = self._execute(req, "playlistItems.insert")
            else:
                raise
        return {
            "playlist_item_id": resp.get("id"),
            "position": resp.get("snippet", {}).get("position"),
        }

    def delete_playlist_item(self, playlist_item_id: str) -> None:
        req = self._service.playlistItems().delete(id=playlist_item_id)
        try:
            self._execute(req, "playlistItems.delete")
        except YouTubeUnavailableError as exc:
            if "404" in str(exc):
                log.info(
                    "playlist item %s already gone from YouTube", playlist_item_id
                )
                return
            raise

    def move_playlist_item(
        self, playlist_item_id: str, playlist_id: str, video_id: str, position: int
    ) -> None:
        body = {
            "id": playlist_item_id,
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
                "position": position,
            },
        }
        req = self._service.playlistItems().update(part="snippet", body=body)
        self._execute(req, "playlistItems.update")

    # --- misc -------------------------------------------------- #
    def check_playlist_writable(self, playlist_id: str) -> None:
        """Cheap-ish sanity check that the playlist exists and we own it."""
        req = self._service.playlists().list(part="snippet,status", id=playlist_id)
        resp = self._execute(req, "playlists.list")
        items = resp.get("items", [])
        if not items:
            from .errors import PlaylistUnavailableError

            raise PlaylistUnavailableError(
                f"YouTube playlist {playlist_id} not found (or not owned by the "
                "authorized account)"
            )
