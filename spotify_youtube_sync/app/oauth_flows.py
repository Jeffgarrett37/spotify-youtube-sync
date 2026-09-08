"""Reusable, headless-friendly OAuth authorization-code flows.

Used by ``scripts/authorize.py`` (run once on a computer with a browser) and by
``python -m app authorize`` (run inside the add-on via an SSH tunnel). Both
produce the same token JSON files that the running add-on consumes from
``/data``.

Nothing here is imported by the sync hot path.
"""

from __future__ import annotations

import base64
import http.server
import json
import secrets
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SCOPES = ["playlist-read-private", "playlist-read-collaborative"]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Loopback redirect. Spotify requires an explicit loopback literal (not
# "localhost") for http redirect URIs; Google accepts the same.
DEFAULT_REDIRECT_HOST = "127.0.0.1"
DEFAULT_REDIRECT_PORT = 8723


@dataclass
class _Capture:
    code: str | None = None
    state: str | None = None
    error: str | None = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    capture: _Capture

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.capture.code = params.get("code", [None])[0]
        self.capture.state = params.get("state", [None])[0]
        self.capture.error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<h2>Authorization received.</h2>"
            "<p>You can close this tab and return to the terminal.</p>"
            if self.capture.code
            else f"<h2>Authorization failed:</h2><pre>{self.capture.error}</pre>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_: Any) -> None:  # silence
        return


def _serve_until_code(
    auth_url: str, params: dict[str, str], host: str, port: int, open_browser: bool
) -> str:
    capture = _Capture()
    _CallbackHandler.capture = capture
    server = http.server.HTTPServer((host, port), _CallbackHandler)
    full_url = auth_url + "?" + urllib.parse.urlencode(params)
    print("\n1) Open this URL in a browser (any device that can reach this host):\n")
    print("   " + full_url + "\n")
    if open_browser:
        try:
            webbrowser.open(full_url)
        except Exception:  # pragma: no cover
            pass
    print(f"2) Waiting for the redirect to http://{host}:{port}/ ...")
    while capture.code is None and capture.error is None:
        server.handle_request()
    server.server_close()
    if capture.error:
        raise RuntimeError(f"authorization denied: {capture.error}")
    if capture.state != params["state"]:
        raise RuntimeError("state mismatch - aborting")
    assert capture.code
    return capture.code


# --------------------------------------------------------------------------- #
# Spotify
# --------------------------------------------------------------------------- #
def authorize_spotify(
    client_id: str,
    client_secret: str,
    out_path: Path,
    redirect_host: str = DEFAULT_REDIRECT_HOST,
    redirect_port: int = DEFAULT_REDIRECT_PORT,
    open_browser: bool = True,
) -> None:
    redirect_uri = f"http://{redirect_host}:{redirect_port}/"
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(SPOTIFY_SCOPES),
        "state": state,
        "show_dialog": "true",
    }
    code = _serve_until_code(
        SPOTIFY_AUTH_URL, params, redirect_host, redirect_port, open_browser
    )
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Authorization": f"Basic {basic}"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "refresh_token" not in payload:
        raise RuntimeError("Spotify did not return a refresh token")
    _write_token(
        out_path,
        {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "scope": payload.get("scope", " ".join(SPOTIFY_SCOPES)),
            "token_type": payload.get("token_type", "Bearer"),
            "expires_in": payload.get("expires_in", 3600),
        },
    )
    print(f"\nSpotify token written to {out_path}")


# --------------------------------------------------------------------------- #
# Google / YouTube
# --------------------------------------------------------------------------- #
def authorize_google(
    client_id: str,
    client_secret: str,
    out_path: Path,
    redirect_host: str = DEFAULT_REDIRECT_HOST,
    redirect_port: int = DEFAULT_REDIRECT_PORT,
    open_browser: bool = True,
) -> None:
    redirect_uri = f"http://{redirect_host}:{redirect_port}/"
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(GOOGLE_SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    code = _serve_until_code(
        GOOGLE_AUTH_URL, params, redirect_host, redirect_port, open_browser
    )
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "refresh_token" not in payload:
        raise RuntimeError(
            "Google did not return a refresh token. Remove this app's access at "
            "https://myaccount.google.com/permissions and retry (prompt=consent)."
        )
    _write_token(
        out_path,
        {
            "token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "token_uri": GOOGLE_TOKEN_URL,
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": GOOGLE_SCOPES,
            "expires_in": payload.get("expires_in", 3600),
        },
    )
    print(f"\nGoogle/YouTube token written to {out_path}")


def _write_token(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - windows
        pass
