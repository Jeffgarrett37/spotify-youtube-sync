#!/usr/bin/env python3
"""Standalone OAuth helper - run this ONCE on a computer that has a browser.

It produces two files:

    spotify_token.json
    youtube_token.json

Copy both into the add-on's config folder
(\\\\homeassistant\\addon_configs\\<slug>\\  over Samba, or /addon_configs/<slug>/
over SSH) and (re)start the add-on. The add-on imports them into its persistent
/data on first boot.

Usage:
    pip install -r scripts/requirements-auth.txt
    python scripts/authorize.py both \\
        --spotify-client-id XXX --spotify-client-secret YYY \\
        --google-client-secrets /path/to/client_secret.json \\
        --out ./tokens

You can also pass --google-client-id / --google-client-secret instead of a
client_secret.json file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spotify_youtube_sync"))

from app.oauth_flows import authorize_google, authorize_spotify  # noqa: E402


def _google_creds(args: argparse.Namespace) -> tuple[str, str]:
    if args.google_client_secrets:
        data = json.loads(Path(args.google_client_secrets).read_text())
        node = data.get("installed") or data.get("web") or {}
        cid = node.get("client_id")
        secret = node.get("client_secret")
        if not cid or not secret:
            sys.exit("client_secret.json missing client_id/client_secret")
        return cid, secret
    if args.google_client_id and args.google_client_secret:
        return args.google_client_id, args.google_client_secret
    sys.exit("provide --google-client-secrets or --google-client-id/-secret")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", choices=["spotify", "google", "youtube", "both"], nargs="?", default="both")
    p.add_argument("--spotify-client-id")
    p.add_argument("--spotify-client-secret")
    p.add_argument("--google-client-secrets", help="path to Google client_secret.json (Desktop app)")
    p.add_argument("--google-client-id")
    p.add_argument("--google-client-secret")
    p.add_argument("--out", default="./tokens", help="output directory (default ./tokens)")
    p.add_argument("--redirect-port", type=int, default=8723)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    open_browser = not args.no_browser

    if args.target in ("spotify", "both"):
        if not (args.spotify_client_id and args.spotify_client_secret):
            sys.exit("--spotify-client-id and --spotify-client-secret are required")
        authorize_spotify(
            args.spotify_client_id,
            args.spotify_client_secret,
            out / "spotify_token.json",
            redirect_port=args.redirect_port,
            open_browser=open_browser,
        )

    if args.target in ("google", "youtube", "both"):
        cid, secret = _google_creds(args)
        authorize_google(
            cid,
            secret,
            out / "youtube_token.json",
            redirect_port=args.redirect_port,
            open_browser=open_browser,
        )

    print(f"\nTokens written to {out.resolve()}")
    print("Copy them into the add-on config folder and restart the add-on.")


if __name__ == "__main__":
    main()
