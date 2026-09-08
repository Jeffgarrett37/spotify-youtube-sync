"""``python -m app authorize`` - complete OAuth from inside the add-on.

Intended for the case where you cannot / do not want to run the standalone
``scripts/authorize.py`` on another computer. Because Spotify only allows an
http redirect to a loopback literal, you run this over an SSH tunnel:

    ssh -L 8723:127.0.0.1:8723 root@homeassistant.local
    # then, inside the add-on shell / this process:
    python -m app authorize both

and open the printed URLs in a browser on the machine that has the tunnel.
Tokens are written straight into ``/data``.
"""

from __future__ import annotations

import sys

from .config import Config
from .logging_setup import configure, get_logger
from .oauth_flows import authorize_google, authorize_spotify

log = get_logger("authorize")


def run(argv: list[str]) -> int:
    config = Config.from_env()
    configure(config.log_level)

    valid = {"spotify", "google", "youtube", "both"}
    target = argv[0].lower() if argv and not argv[0].startswith("-") else "both"
    if target not in valid:
        print(__doc__)
        return 2
    open_browser = "--no-browser" not in argv

    if target in ("spotify", "both"):
        if not (config.spotify_client_id and config.spotify_client_secret):
            log.error("spotify_client_id / spotify_client_secret not configured")
            return 2
        print("\n=== Spotify authorization ===")
        authorize_spotify(
            config.spotify_client_id,
            config.spotify_client_secret,
            config.spotify_token_path,
            open_browser=open_browser,
        )

    if target in ("google", "youtube", "both"):
        if not (config.youtube_client_id and config.youtube_client_secret):
            log.error("youtube_client_id / youtube_client_secret not configured")
            return 2
        print("\n=== Google / YouTube authorization ===")
        authorize_google(
            config.youtube_client_id,
            config.youtube_client_secret,
            config.youtube_token_path,
            open_browser=open_browser,
        )

    print("\nDone. Restart the add-on to pick up the new token(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(sys.argv[1:]))
