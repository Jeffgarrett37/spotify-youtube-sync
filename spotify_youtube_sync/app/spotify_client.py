"""Minimal Spotify Web API client.

Hand-rolled (no spotipy) so we control exactly which endpoints and fields are
used and can adapt to the Feb/Mar-2026 development-mode changes:

* The playlist-items endpoint was renamed ``/playlists/{id}/tracks`` ->
  ``/playlists/{id}/items``. We try the new path and fall back to the old one.
* We never touch removed endpoints (batch ``/tracks``, ``/audio-features``,
  ``/recommendations``, browse, artist top-tracks, ...).
* Only read scopes are requested; this client has no write methods at all, so
  it *cannot* modify a Spotify playlist.

Auth: authorization-code flow with a persisted refresh token. Access tokens
are refreshed automatically; a rotated refresh token is written back to disk.
Secrets and tokens are never logged.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .errors import (
    IncompleteSpotifyFetchError,
    MalformedResponseError,
    PlaylistUnavailableError,
    SourceStateError,
    SpotifyAuthError,
    SpotifyUnavailableError,
)
from .logging_setup import get_logger
from .models import SpotifyArtist, SpotifyTrack
from .normalize import normalize_text

log = get_logger("spotify")

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
PAGE_LIMIT = 50  # max items per playlist page under the 2026 API
_ITEM_FIELDS = (
    "next,total,items(added_at,is_local,"
    "track(id,name,duration_ms,explicit,type,"
    "external_ids(isrc),album(name,album_type),artists(id,name)))"
)
_LIVE_HINTS = ("live", "unplugged", "live at", "live from", "en vivo", "en directo")
_REMIX_HINTS = ("remix", "mix)", "flip", "bootleg", "rework", "vip)", "- vip")


class _TransientSpotifyError(Exception):
    """Retryable network / 5xx / 429 condition."""


def _redact(text: str) -> str:  # pragma: no cover - trivial
    return text


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
        session: requests.Session | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise SpotifyAuthError("Spotify client_id / client_secret not configured")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_path = token_path
        self._session = session or requests.Session()
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._refresh_token: str | None = None
        self._load_token()

    # --- token handling ------------------------------------------------- #
    def _load_token(self) -> None:
        if not self._token_path.exists():
            raise SpotifyAuthError(
                f"Spotify token file missing at {self._token_path}. Run the "
                "authorization helper (see docs/SPOTIFY.md)."
            )
        try:
            data = json.loads(self._token_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SpotifyAuthError(f"cannot read Spotify token file: {exc}") from exc
        self._refresh_token = data.get("refresh_token")
        self._access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        # We do not trust a stale access token; force a refresh on first use
        # unless the file also carries an absolute expiry.
        self._expires_at = float(data.get("expires_at", 0.0))
        if not self._refresh_token:
            raise SpotifyAuthError("Spotify token file has no refresh_token")
        if expires_in and not self._expires_at:
            self._expires_at = 0.0  # refresh immediately

    def _save_token(self, payload: dict[str, Any]) -> None:
        # Spotify may rotate the refresh token; keep whichever we now hold.
        refresh = payload.get("refresh_token") or self._refresh_token
        record = {
            "access_token": payload["access_token"],
            "refresh_token": refresh,
            "scope": payload.get("scope", ""),
            "token_type": payload.get("token_type", "Bearer"),
            "expires_at": time.time() + int(payload.get("expires_in", 3600)) - 60,
        }
        tmp = self._token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(self._token_path)
        try:
            self._token_path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass
        self._access_token = record["access_token"]
        self._refresh_token = record["refresh_token"]
        self._expires_at = record["expires_at"]

    @retry(
        retry=retry_if_exception_type(_TransientSpotifyError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _refresh(self) -> None:
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        try:
            resp = self._session.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                headers={"Authorization": f"Basic {basic}"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise _TransientSpotifyError(f"token endpoint unreachable: {exc}") from exc
        if resp.status_code in (500, 502, 503, 504, 429):
            raise _TransientSpotifyError(f"token endpoint {resp.status_code}")
        if resp.status_code in (400, 401):
            # invalid_grant => refresh token revoked/expired
            raise SpotifyAuthError(
                "Spotify refresh token rejected (revoked or expired). Re-run the "
                "authorization helper."
            )
        resp.raise_for_status()
        self._save_token(resp.json())
        log.info("Refreshed Spotify access token")

    def _ensure_token(self) -> str:
        if not self._access_token or time.time() >= self._expires_at:
            self._refresh()
        assert self._access_token
        return self._access_token

    # --- request plumbing --------------------------------------------- #
    @retry(
        retry=retry_if_exception_type(_TransientSpotifyError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=45),
        reraise=True,
    )
    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._ensure_token()
        try:
            resp = self._session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise _TransientSpotifyError(f"network error: {exc}") from exc

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "2"))
            wait = min(retry_after, 60)
            log.warning("Spotify rate limited; sleeping %.0fs", wait)
            time.sleep(wait)
            raise _TransientSpotifyError("429 rate limited")
        if resp.status_code == 401:
            # token expired mid-flight; drop it and let tenacity retry
            self._access_token = None
            self._expires_at = 0.0
            raise _TransientSpotifyError("401 - token expired mid-request")
        if resp.status_code in (500, 502, 503, 504):
            raise _TransientSpotifyError(f"{resp.status_code} server error")
        if resp.status_code in (403, 404):
            raise PlaylistUnavailableError(
                f"Spotify returned {resp.status_code} for {url.split('?')[0]} "
                "(playlist deleted, private, or not visible to this account)"
            )
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise SpotifyUnavailableError(str(exc)) from exc
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise MalformedResponseError(f"Spotify sent non-JSON: {exc}") from exc

    # --- public API --------------------------------------------------- #
    def get_playlist_meta(self, playlist_id: str) -> dict[str, Any]:
        data = self._get(
            f"{API_BASE}/playlists/{playlist_id}",
            params={"fields": "snapshot_id,name,description,owner(id,display_name),"
                              "public,tracks(total)"},
        )
        if "snapshot_id" not in data:
            raise MalformedResponseError("playlist meta missing snapshot_id")
        return {
            "snapshot_id": data["snapshot_id"],
            "name": data.get("name", ""),
            "owner": (data.get("owner") or {}).get("id", ""),
            "total": int((data.get("tracks") or {}).get("total", 0)),
        }

    def _fetch_all_items(self, playlist_id: str) -> list[dict[str, Any]]:
        """Return every raw playlist item, following pagination."""
        # Prefer the 2026 path; fall back to the deprecated one.
        for path in ("items", "tracks"):
            url = f"{API_BASE}/playlists/{playlist_id}/{path}"
            try:
                first = self._get(
                    url, params={"limit": PAGE_LIMIT, "offset": 0, "fields": _ITEM_FIELDS}
                )
            except PlaylistUnavailableError:
                if path == "items":
                    continue  # try the old path before giving up
                raise
            break
        else:  # pragma: no cover - both failed with 404
            raise PlaylistUnavailableError("playlist items endpoint returned 404")

        items: list[dict[str, Any]] = list(first.get("items", []))
        declared_total = int(first.get("total", len(items)))
        next_url = first.get("next")
        guard = 0
        while next_url:
            guard += 1
            if guard > 10_000:  # pragma: no cover - runaway protection
                raise SourceStateError("pagination did not terminate")
            page = self._get(next_url, params={"fields": _ITEM_FIELDS})
            items.extend(page.get("items", []))
            next_url = page.get("next")

        if len(items) != declared_total:
            raise IncompleteSpotifyFetchError(
                f"expected {declared_total} playlist items, retrieved {len(items)}"
            )
        return items

    def get_playlist_tracks(self, playlist_id: str) -> tuple[str, list[SpotifyTrack]]:
        """Return ``(snapshot_id, ordered_tracks)`` for a *fully validated* fetch.

        Raises a ``SourceStateError`` subclass if the source state cannot be
        trusted (incomplete pagination, playlist changed mid-read, unavailable).
        The caller MUST treat any such error as "do not delete anything".
        """
        last_error: Exception | None = None
        for attempt in range(1, 4):
            meta_before = self.get_playlist_meta(playlist_id)
            raw_items = self._fetch_all_items(playlist_id)
            meta_after = self.get_playlist_meta(playlist_id)
            if meta_before["snapshot_id"] != meta_after["snapshot_id"]:
                last_error = SourceStateError(
                    "playlist changed while being read; retrying"
                )
                log.warning("%s (attempt %d/3)", last_error, attempt)
                time.sleep(2 * attempt)
                continue
            if len(raw_items) != meta_after["total"]:
                last_error = IncompleteSpotifyFetchError(
                    f"post-fetch count {len(raw_items)} != total {meta_after['total']}"
                )
                log.warning("%s (attempt %d/3)", last_error, attempt)
                time.sleep(2 * attempt)
                continue
            tracks = self._build_tracks(raw_items)
            log.info(
                "Spotify playlist '%s': %d items (%d playable tracks), snapshot %s",
                meta_after["name"], len(raw_items), len(tracks),
                meta_after["snapshot_id"][:10],
            )
            return meta_after["snapshot_id"], tracks

        assert last_error is not None
        raise last_error

    # --- parsing ------------------------------------------------------ #
    @staticmethod
    def _build_tracks(raw_items: list[dict[str, Any]]) -> list[SpotifyTrack]:
        tracks: list[SpotifyTrack] = []
        occ: dict[str, int] = {}
        for position, item in enumerate(raw_items):
            if item.get("is_local"):
                continue
            t = item.get("track")
            if not t or t.get("type") not in (None, "track"):
                continue
            track_id = t.get("id")
            if not track_id:
                continue  # local file / unavailable in this market
            name = t.get("name") or ""
            album = (t.get("album") or {}).get("name", "") or ""
            album_type = (t.get("album") or {}).get("album_type", "")
            artists = tuple(
                SpotifyArtist(id=a.get("id"), name=a.get("name", ""))
                for a in (t.get("artists") or [])
                if a.get("name")
            )
            isrc = (t.get("external_ids") or {}).get("isrc")
            hay = normalize_text(f"{name} {album}")
            is_live = any(h in hay for h in _LIVE_HINTS) or "live" in normalize_text(
                album if album_type == "compilation" else ""
            )
            is_remix = any(h in (name.lower()) for h in _REMIX_HINTS)
            n = occ.get(track_id, 0)
            occ[track_id] = n + 1
            tracks.append(
                SpotifyTrack(
                    track_id=track_id,
                    name=name,
                    artists=artists,
                    album=album,
                    duration_ms=int(t.get("duration_ms") or 0),
                    isrc=isrc.upper() if isrc else None,
                    explicit=bool(t.get("explicit")),
                    is_remix=is_remix,
                    is_live=is_live,
                    playlist_position=position,
                    occurrence=n,
                    added_at=item.get("added_at"),
                )
            )
        return tracks
