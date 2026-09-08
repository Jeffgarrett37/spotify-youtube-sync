"""Application wiring: database + scheduler + ingress status server."""

from __future__ import annotations

import signal
import threading
import time
from datetime import UTC, datetime

from .config import Config
from .database import Database
from .errors import AuthError, ConfigError
from .httpserver import Controls, StatusServer
from .logging_setup import configure, get_logger
from .models import SyncOutcome
from .quota import QuotaTracker
from .scheduler import Scheduler
from .spotify_client import SpotifyClient
from .status import build_status
from .sync_engine import SyncEngine
from .youtube_client import YouTubeClient

log = get_logger("main")


class Application:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.scheduler = Scheduler(
            self.run_once, self.db, config.sync_interval_hours
        )
        self._server: StatusServer | None = None
        self._stop = threading.Event()

    # --- status plumbing for the web UI ------------------------------- #
    def _tokens_present(self) -> dict[str, bool]:
        return {
            "spotify": self.config.spotify_token_path.exists(),
            "youtube": self.config.youtube_token_path.exists(),
        }

    def _status(self) -> dict:
        try:
            configured = True
            self.config.require_for_sync()
        except ConfigError:
            configured = False
        next_run = self.db.get_meta("next_sync_at")
        return build_status(
            self.config,
            self.db,
            None,
            configured=configured,
            tokens_present=self._tokens_present(),
            scheduler_running=self.scheduler.is_running,
            next_run=next_run,
        )

    def _controls(self) -> Controls:
        return Controls(
            status_provider=self._status,
            trigger_sync=self.scheduler.trigger,
            clear_unmatched=lambda: self.db.clear_unmatched(),
            add_override=lambda s, v: self.db.put_override(s, v, "added via web UI"),
            delete_override=self.db.delete_override,
        )

    # --- the actual sync entrypoint --------------------------------- #
    def run_once(self, trigger: str) -> SyncOutcome:
        try:
            self.config.require_for_sync()
        except ConfigError as exc:
            return self._record_precondition_failure(
                f"configuration incomplete: {exc}. Set the add-on options and "
                "restart."
            )

        tokens = self._tokens_present()
        if not all(tokens.values()):
            missing = [k for k, v in tokens.items() if not v]
            return self._record_precondition_failure(
                f"missing OAuth token(s): {', '.join(missing)}. Complete "
                "authorization (see docs/SETUP.md) and drop the *_token.json "
                "file(s) into the add-on config folder, then restart."
            )

        quota = QuotaTracker(self.db, self.config.daily_quota_budget)
        try:
            spotify = SpotifyClient(
                self.config.spotify_client_id,
                self.config.spotify_client_secret,
                self.config.spotify_token_path,
            )
            youtube = YouTubeClient(
                self.config.youtube_client_id,
                self.config.youtube_client_secret,
                self.config.youtube_token_path,
                quota,
                region_code=self.config.region_code,
            )
        except AuthError as exc:
            return self._record_precondition_failure(f"authentication failed: {exc}")

        engine = SyncEngine(self.config, self.db, spotify, youtube, quota)
        return engine.run(trigger=trigger)

    def _record_precondition_failure(self, message: str) -> SyncOutcome:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        sync_id = self.db.start_sync(now)
        self.db.finish_sync(
            sync_id,
            status="failed",
            finished_at=now,
            tracks_evaluated=0,
            additions=0,
            removals=0,
            reorders=0,
            unmatched=0,
            quota_spent=0,
            errors=[message],
            message=message,
        )
        self.db.set_meta("last_attempt_at", now)
        self.db.set_meta("last_result", "failed")
        log.error("%s", message)
        return SyncOutcome(
            status="failed", started_at=now, finished_at=now, message=message,
            errors=[message],
        )

    # --- lifecycle ------------------------------------------------- #
    def start(self) -> None:
        log.info(
            "spotify-youtube-sync starting | interval=%dh strict_mirror=%s dry_run=%s",
            self.config.sync_interval_hours,
            self.config.strict_mirror,
            self.config.dry_run,
        )
        self._server = StatusServer(self.config.ingress_port, self._controls())
        self._server.start()
        self.scheduler.start()

    def run_forever(self) -> None:
        self.start()

        def _handle(signum, _frame):
            log.info("received signal %s; shutting down", signum)
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handle)
            except ValueError:  # pragma: no cover - not main thread
                pass

        try:
            while not self._stop.is_set():
                time.sleep(1)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("stopping scheduler and status server")
        self.scheduler.stop()
        if self._server:
            self._server.stop()
        self.db.close()


def main() -> None:
    config = Config.from_env()
    configure(config.log_level)
    Application(config).run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
