"""Tiny status/control server exposed through Home Assistant ingress.

Routes (all relative to the ingress base path):

    GET  /            HTML dashboard
    GET  /health      full status JSON
    GET  /healthz     "ok" (used by the add-on watchdog)
    POST /sync        trigger a sync now
    POST /unmatched/clear          clear the whole unmatched store
    POST /override/add            body: spotify_track_id, youtube_video_id
    POST /override/delete         body: spotify_track_id

No secrets are ever rendered. Ingress provides authentication.
"""

from __future__ import annotations

import html
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .logging_setup import get_logger

log = get_logger("http")

StatusProvider = Callable[[], dict[str, Any]]
TriggerFn = Callable[[str], bool]


class Controls:
    def __init__(
        self,
        status_provider: StatusProvider,
        trigger_sync: TriggerFn,
        clear_unmatched: Callable[[], int],
        add_override: Callable[[str, str], None],
        delete_override: Callable[[str], bool],
    ) -> None:
        self.status_provider = status_provider
        self.trigger_sync = trigger_sync
        self.clear_unmatched = clear_unmatched
        self.add_override = add_override
        self.delete_override = delete_override


def _handler_factory(controls: Controls) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "spotify-youtube-sync/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        # --- helpers ------------------------------------------------ #
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(code, json.dumps(payload, indent=2).encode(), "application/json")

        def _redirect_home(self) -> None:
            base = self.headers.get("X-Ingress-Path", "") or "."
            self.send_response(303)
            self.send_header("Location", base or ".")
            self.end_headers()

        def _body_params(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode() if length else ""
            return {k: v[0] for k, v in parse_qs(raw).items()}

        # --- verbs ------------------------------------------------- #
        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path.endswith("/healthz") or path == "/healthz":
                self._send(200, b"ok", "text/plain")
                return
            if path.endswith("/health") or path.endswith("/status"):
                self._json(200, controls.status_provider())
                return
            if path == "/" or path.endswith("/index.html") or path == "":
                self._send(200, _render_page(controls.status_provider()).encode(),
                           "text/html; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/")
            params = self._body_params()
            if path.endswith("/sync"):
                ok = controls.trigger_sync("manual (web)")
                log.info("manual sync requested via web UI: %s",
                         "accepted" if ok else "already running")
            elif path.endswith("/unmatched/clear"):
                n = controls.clear_unmatched()
                log.info("cleared %d unmatched entries via web UI", n)
            elif path.endswith("/override/add"):
                sid = params.get("spotify_track_id", "").strip()
                vid = params.get("youtube_video_id", "").strip()
                if sid and vid:
                    controls.add_override(sid, vid)
                    log.info("override added via web UI: %s -> %s", sid, vid)
            elif path.endswith("/override/delete"):
                sid = params.get("spotify_track_id", "").strip()
                if sid:
                    controls.delete_override(sid)
                    log.info("override removed via web UI: %s", sid)
            else:
                self._send(404, b"not found", "text/plain")
                return
            self._redirect_home()

    return Handler


class StatusServer:
    def __init__(self, port: int, controls: Controls) -> None:
        self._port = port
        self._httpd = ThreadingHTTPServer(("0.0.0.0", port), _handler_factory(controls))
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="status-http", daemon=True
        )
        self._thread.start()
        log.info("status server listening on :%d (via Home Assistant ingress)", self._port)

    def stop(self) -> None:
        self._httpd.shutdown()
        if self._thread:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# HTML rendering (no framework, no external assets - CSP friendly).
# --------------------------------------------------------------------------- #
def _badge(ok: bool, label_ok: str = "OK", label_bad: str = "ATTENTION") -> str:
    color = "#1a7f37" if ok else "#b3261e"
    text = label_ok if ok else label_bad
    return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px">{text}</span>'


def _render_page(status: dict[str, Any]) -> str:
    e = html.escape
    q = status.get("quota", {})
    last = status.get("last_sync") or {}
    rows = "".join(
        f"<tr><td>{e(str(s.get('started_at','')))}</td><td>{e(str(s.get('status','')))}</td>"
        f"<td>{s.get('additions',0)}</td><td>{s.get('removals',0)}</td>"
        f"<td>{s.get('unmatched',0)}</td><td>{s.get('quota_spent',0)}</td>"
        f"<td>{e(str(s.get('message','') or ''))}</td></tr>"
        for s in status.get("recent_syncs", [])
    )
    unmatched_rows = "".join(
        f"<tr><td>{e(str(u.get('artist','')))} - {e(str(u.get('title','')))}</td>"
        f"<td>{e(str(u.get('reason','') or ''))}</td>"
        f"<td>{e(str(u.get('best_candidate_id') or ''))}</td>"
        f"<td>{u.get('retry_count',0)}</td></tr>"
        for u in status.get("unmatched", [])
    ) or '<tr><td colspan="4">none</td></tr>'
    override_rows = "".join(
        f"<tr><td><code>{e(k)}</code></td><td><code>{e(v)}</code></td>"
        f'<td><form method="post" action="override/delete" style="margin:0">'
        f'<input type="hidden" name="spotify_track_id" value="{e(k)}">'
        f'<button>remove</button></form></td></tr>'
        for k, v in (status.get("overrides") or {}).items()
    ) or '<tr><td colspan="3">none</td></tr>'

    tp = status.get("tokens_present", {})
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Spotify to YouTube Sync</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px;background:#f6f7f9;color:#1c1c1e}}
 h1{{font-size:20px;margin:0 0 12px}} h2{{font-size:15px;margin:20px 0 6px}}
 .card{{background:#fff;border:1px solid #e3e3e6;border-radius:10px;padding:14px;margin-bottom:12px}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 td,th{{border-bottom:1px solid #ececef;padding:5px 8px;text-align:left;vertical-align:top}}
 button{{background:#0b57d0;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer}}
 code{{background:#f0f0f3;padding:1px 4px;border-radius:4px;font-size:12px}}
 .kv{{display:grid;grid-template-columns:180px 1fr;gap:4px 12px;font-size:13px}}
 input[type=text]{{padding:6px;border:1px solid #ccc;border-radius:6px;font-size:13px}}
</style></head><body>
<h1>Spotify &rarr; YouTube Playlist Sync {_badge(bool(status.get('healthy')))}</h1>

<div class="card">
 <div class="kv">
  <div>Configured</div><div>{_badge(bool(status.get('configured')), 'yes','no')}</div>
  <div>Spotify token</div><div>{_badge(bool(tp.get('spotify')), 'present','missing')}</div>
  <div>YouTube token</div><div>{_badge(bool(tp.get('youtube')), 'present','missing')}</div>
  <div>Dry run</div><div>{e(str(status.get('dry_run')))}</div>
  <div>Strict mirror</div><div>{e(str(status.get('strict_mirror')))}</div>
  <div>Scheduler running</div><div>{_badge(bool(status.get('scheduler_running')), 'yes','no')}</div>
  <div>Last attempt</div><div>{e(str(status.get('last_attempt_at')))}</div>
  <div>Last success</div><div>{e(str(status.get('last_success_at')))}</div>
  <div>Last result</div><div>{e(str(status.get('last_result')))}</div>
  <div>Next sync</div><div>{e(str(status.get('next_sync_at')))}</div>
  <div>Managed items</div><div>{status.get('managed_items_active',0)}</div>
  <div>Cached mappings</div><div>{status.get('cached_mappings',0)}</div>
  <div>Unmatched</div><div>{status.get('unmatched_count',0)}</div>
  <div>YouTube quota today</div><div>{q.get('spent_today',0)} / {q.get('daily_budget',0)} units ({q.get('pacific_day','')} PT)</div>
 </div>
 <p style="margin:12px 0 0">
  <form method="post" action="sync" style="display:inline">
   <button {'disabled' if status.get('scheduler_running') else ''}>Sync now</button>
  </form>
 </p>
 <p style="font-size:12px;color:#666">Last sync: {e(str(last.get('message','') or 'n/a'))}</p>
</div>

<div class="card">
 <h2>Manual mapping override</h2>
 <form method="post" action="override/add">
  <input type="text" name="spotify_track_id" placeholder="Spotify track ID" size="26" required>
  &rarr;
  <input type="text" name="youtube_video_id" placeholder="YouTube video ID" size="16" required>
  <button>add / update</button>
 </form>
 <table style="margin-top:8px"><tr><th>Spotify track</th><th>YouTube video</th><th></th></tr>
 {override_rows}</table>
</div>

<div class="card">
 <h2>Unmatched / review required</h2>
 <form method="post" action="unmatched/clear"><button>clear &amp; retry all</button></form>
 <table style="margin-top:8px"><tr><th>Track</th><th>Reason</th><th>Best candidate</th><th>Tries</th></tr>
 {unmatched_rows}</table>
</div>

<div class="card">
 <h2>Recent syncs</h2>
 <table><tr><th>Started</th><th>Status</th><th>+</th><th>-</th><th>unmatched</th><th>quota</th><th>message</th></tr>
 {rows or '<tr><td colspan="7">no syncs yet</td></tr>'}</table>
</div>

<p style="font-size:11px;color:#888">Spotify is the source of truth. This add-on only removes YouTube items it added itself.</p>
</body></html>"""
