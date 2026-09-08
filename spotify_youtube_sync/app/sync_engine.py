"""The two-phase, safety-first synchronization engine.

    Phase 1  FETCH & VALIDATE   - obtain a complete, trustworthy Spotify state
                                  or abort (never delete on a partial read).
    Phase 2  PLAN               - compute adds / removes / reorders, apply
                                  safety brakes, log the plan.
    Phase 3  APPLY              - only now touch the YouTube playlist, stopping
                                  cleanly if the quota budget runs out.

Removal rules (both modes only ever delete items THIS add-on inserted and has a
record for):
    strict_mirror = True   orphaned managed item  -> delete
    strict_mirror = False  orphaned managed item  -> delete only if it is the
                           single, still-recorded copy; otherwise just forget it
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime

from .config import Config
from .database import Database
from .errors import (
    ApiUnavailableError,
    AuthError,
    ConfigError,
    QuotaExceededError,
    SourceStateError,
    SyncError,
)
from .logging_setup import get_logger
from .models import (
    MatchTier,
    PlannedAdd,
    PlannedRemove,
    SpotifyArtist,
    SpotifyTrack,
    SyncOutcome,
    SyncPlan,
)
from .quota import QuotaTracker
from .resolver import ResolveOutcome, TrackResolver
from .spotify_client import SpotifyClient
from .youtube_client import YouTubeClient

log = get_logger("sync")

_SNAPSHOT_KEY = "spotify_snapshot_id"
_DESIRED_KEY = "spotify_desired_state"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _track_to_row(t: SpotifyTrack) -> dict:
    d = asdict(t)
    d["artists"] = [{"id": a["id"], "name": a["name"]} for a in d["artists"]]
    return d


def _row_to_track(d: dict) -> SpotifyTrack:
    return SpotifyTrack(
        track_id=d["track_id"],
        name=d["name"],
        artists=tuple(SpotifyArtist(id=a["id"], name=a["name"]) for a in d["artists"]),
        album=d.get("album", ""),
        duration_ms=int(d.get("duration_ms", 0)),
        isrc=d.get("isrc"),
        explicit=bool(d.get("explicit", False)),
        is_remix=bool(d.get("is_remix", False)),
        is_live=bool(d.get("is_live", False)),
        playlist_position=int(d.get("playlist_position", 0)),
        occurrence=int(d.get("occurrence", 0)),
        added_at=d.get("added_at"),
    )


class SyncEngine:
    def __init__(
        self,
        config: Config,
        db: Database,
        spotify: SpotifyClient,
        youtube: YouTubeClient,
        quota: QuotaTracker,
    ) -> None:
        self.config = config
        self.db = db
        self.spotify = spotify
        self.youtube = youtube
        self.quota = quota
        self.resolver = TrackResolver(
            db, youtube, quota, config, persist=not config.dry_run
        )

    # ================================================================== #
    def run(self, *, trigger: str = "scheduled") -> SyncOutcome:
        started = _now()
        sync_id = self.db.start_sync(started)
        self.db.set_meta("last_attempt_at", started)
        errors: list[str] = []
        log.info("Spotify -> YouTube sync starting (trigger=%s, dry_run=%s)",
                 trigger, self.config.dry_run)

        try:
            snapshot_id, tracks, from_cache = self._load_source()
        except (SourceStateError, AuthError, ApiUnavailableError, ConfigError) as exc:
            msg = f"source fetch failed: {exc}"
            log.error("%s -- no YouTube changes will be made", msg)
            return self._finish(sync_id, started, "failed", errors=[msg], message=msg)
        except Exception as exc:  # pragma: no cover - unexpected
            msg = f"unexpected source error: {exc!r}"
            log.exception(msg)
            return self._finish(sync_id, started, "failed", errors=[msg], message=msg)

        log.info("Spotify playlist: %d tracks%s", len(tracks),
                 " (from validated cache, snapshot unchanged)" if from_cache else "")

        try:
            plan = self._build_plan(tracks, source_validated=True)
        except QuotaExceededError as exc:
            msg = f"quota exhausted while planning: {exc}"
            log.warning(msg)
            return self._finish(sync_id, started, "partial", errors=[msg], message=msg)
        except (AuthError, ApiUnavailableError, SourceStateError) as exc:
            msg = f"planning failed: {exc}"
            log.error("%s -- no YouTube changes will be made", msg)
            return self._finish(sync_id, started, "failed", errors=[msg], message=msg)

        for line in plan.describe().splitlines():
            log.info(line)

        if self.config.dry_run:
            log.info("dry_run=true: no changes applied")
            return self._finish(
                sync_id, started, "dry_run", plan=plan,
                message="dry run - no changes applied",
            )

        status = self._apply(plan, errors)
        if status == "success" and (plan.quota_limited or plan.large_removal_blocked):
            status = "partial"
        return self._finish(sync_id, started, status, plan=plan, errors=errors,
                            message=self._summary_message(plan, errors, status))

    # ================================================================== #
    # Phase 1
    # ================================================================== #
    def _load_source(self) -> tuple[str, list[SpotifyTrack], bool]:
        meta = self.spotify.get_playlist_meta(self.config.spotify_playlist_id)
        stored_snapshot = self.db.get_meta(_SNAPSHOT_KEY)
        stored_desired = self.db.get_meta_json(_DESIRED_KEY)

        if (
            stored_snapshot
            and stored_snapshot == meta["snapshot_id"]
            and isinstance(stored_desired, list)
            and stored_desired
            and not self._unmatched_retry_due()
            and self.db.last_successful_sync() is not None
        ):
            tracks = [_row_to_track(d) for d in stored_desired]
            return meta["snapshot_id"], tracks, True

        snapshot_id, tracks = self.spotify.get_playlist_tracks(
            self.config.spotify_playlist_id
        )
        # Persist the validated state immediately so a later quota stop can
        # resume from here without another full fetch.
        self.db.set_meta(_SNAPSHOT_KEY, snapshot_id)
        self.db.set_meta_json(_DESIRED_KEY, [_track_to_row(t) for t in tracks])
        return snapshot_id, tracks, False

    def _unmatched_retry_due(self) -> bool:
        rows = self.db.all_unmatched()
        if not rows:
            return False
        cutoff_h = self.config.unmatched_retry_hours
        for row in rows:
            try:
                last = datetime.fromisoformat(row["last_attempt_at"])
            except (ValueError, TypeError):
                return True
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (datetime.now(UTC) - last).total_seconds() / 3600 >= cutoff_h:
                return True
        return False

    # ================================================================== #
    # Phase 2
    # ================================================================== #
    def _build_plan(
        self, tracks: list[SpotifyTrack], *, source_validated: bool
    ) -> SyncPlan:
        plan = SyncPlan(source_validated=source_validated)
        plan.spotify_tracks = len(tracks)
        plan.managed_before = self.db.count_active_managed()

        yt_items = self.youtube.list_playlist_items(self.config.youtube_playlist_id)
        yt_by_item_id = {it["playlist_item_id"]: it for it in yt_items}
        yt_video_counts = Counter(it["video_id"] for it in yt_items if it["video_id"])
        yt_present_item_ids = set(yt_by_item_id)

        # Cheap availability check for every video id we care about.
        alive = self._verify_videos(tracks, yt_items)

        active_managed = self.db.active_managed_items()
        managed_by_key: dict[tuple[str, int], list] = defaultdict(list)
        for row in active_managed:
            managed_by_key[(row["spotify_track_id"], row["occurrence"])].append(row)

        wanted_managed_ids: set[int] = set()
        overrides = self.db.all_overrides()

        target_index = 0
        for track in tracks:
            key = track.key
            managed_rows = managed_by_key.get(key, [])
            row = managed_rows[0] if managed_rows else None

            if row is not None:
                wanted_managed_ids.add(row["id"])
                video_id = row["youtube_video_id"]
                override_id = overrides.get(track.track_id)
                item_in_yt = (
                    row["playlist_item_id"] in yt_present_item_ids
                    if row["playlist_item_id"]
                    else any(
                        it["video_id"] == video_id for it in yt_items
                    )
                )
                video_ok = alive.get(video_id, True)

                if override_id and override_id != video_id:
                    plan.removes.append(self._removal(row, "superseded by manual override"))
                    self._plan_add_for(track, target_index, plan)
                elif not video_ok:
                    log.info(
                        "managed video %s for '%s - %s' is unavailable; will replace",
                        video_id, track.primary_artist, track.name,
                    )
                    plan.removes.append(self._removal(row, "youtube video unavailable"))
                    if not self.config.dry_run:
                        self.db.delete_mapping(track.track_id)
                    self._plan_add_for(track, target_index, plan)
                elif not item_in_yt:
                    log.info(
                        "managed item for '%s - %s' vanished from the YouTube "
                        "playlist; will re-add", track.primary_artist, track.name,
                    )
                    if not self.config.dry_run:
                        self.db.deactivate_managed_item(
                            row["id"], "item vanished from playlist"
                        )
                    self._plan_add_for(track, target_index, plan)
                else:
                    plan.unchanged += 1
                    if not self.config.dry_run:
                        self.db.touch_mapping_verified(track.track_id)
            else:
                self._plan_add_for(track, target_index, plan)

            target_index += 1

        # Orphans: active managed items no desired occurrence claimed.
        for row in active_managed:
            if row["id"] in wanted_managed_ids:
                continue
            if not source_validated:  # defensive; should never happen
                plan.notes.append("source not validated - skipping all removals")
                break
            removal = self._orphan_removal(row, yt_by_item_id, yt_video_counts)
            if removal is not None:
                plan.removes.append(removal)
            else:
                if not self.config.dry_run:
                    self.db.deactivate_managed_item(
                        row["id"], "orphan not safely removable (non-strict); forgotten"
                    )
                plan.notes.append(
                    f"kept YouTube item for gone Spotify track {row['spotify_track_id']}"
                    " (non-strict, could not confirm single managed copy)"
                )

        self._apply_safety_brakes(plan)
        if self.config.manage_order and not self.config.dry_run:
            plan.notes.append("order management runs after add/remove in the apply phase")
        return plan

    def _plan_add_for(self, track: SpotifyTrack, target_index: int, plan: SyncPlan) -> None:
        outcome: ResolveOutcome = self.resolver.resolve(track)
        if outcome.video_id and outcome.tier in (
            MatchTier.HIGH, MatchTier.OVERRIDE, MatchTier.CACHED
        ):
            plan.adds.append(
                PlannedAdd(
                    track=track,
                    video=outcome.video,  # type: ignore[arg-type]
                    score=outcome.score,
                    tier=outcome.tier,
                    target_position=target_index,
                )
            )
        else:
            if outcome.quota_blocked:
                plan.quota_limited = True
                plan.notes.append(
                    f"'{track.primary_artist} - {track.name}': search deferred "
                    "(quota); will retry next sync"
                )
                return  # not a real "unmatched" - just deferred
            plan.unmatched.append(
                outcome.match_result
                or _synthetic_unmatched(track, outcome.note)
            )

    def _removal(self, row, reason: str) -> PlannedRemove:
        return PlannedRemove(
            spotify_track_id=row["spotify_track_id"],
            occurrence=row["occurrence"],
            youtube_video_id=row["youtube_video_id"],
            playlist_item_id=row["playlist_item_id"] or "",
            reason=reason,
        )

    def _orphan_removal(self, row, yt_by_item_id, yt_video_counts) -> PlannedRemove | None:
        reason = "removed from Spotify"
        if self.config.strict_mirror:
            return self._removal(row, reason)
        # non-strict: require a single, still-recorded copy
        item_id = row["playlist_item_id"]
        vid = row["youtube_video_id"]
        if item_id and item_id in yt_by_item_id and yt_video_counts.get(vid, 0) == 1:
            return self._removal(row, reason + " (non-strict, sole managed copy)")
        return None

    def _verify_videos(self, tracks, yt_items) -> dict[str, bool]:
        ids: set[str] = set()
        for t in tracks:
            m = self.db.get_mapping(t.track_id)
            if m and m["youtube_video_id"]:
                ids.add(m["youtube_video_id"])
        for it in yt_items:
            if it["video_id"]:
                ids.add(it["video_id"])
        if not ids:
            return {}
        alive: dict[str, bool] = {}
        try:
            found = self.youtube.get_videos(sorted(ids))
        except QuotaExceededError:
            log.warning("skipping video-availability check (quota)")
            return {i: True for i in ids}
        for i in ids:
            v = found.get(i)
            alive[i] = bool(v and v.is_available)
        dead = [i for i, ok in alive.items() if not ok]
        if dead:
            log.info("%d referenced YouTube video(s) are unavailable: %s",
                     len(dead), ", ".join(dead[:10]))
        return alive

    def _apply_safety_brakes(self, plan: SyncPlan) -> None:
        n_rm = len(plan.removes)
        if n_rm == 0:
            return
        if not plan.source_validated:
            plan.notes.append("BLOCKED all removals: Spotify source not validated")
            plan.removes.clear()
            return
        base = plan.managed_before or 1
        ratio = n_rm / base
        if (
            ratio > self.config.removal_safety_ratio
            and n_rm >= 5
            and not self.config.confirm_large_removal
        ):
            plan.large_removal_blocked = True
            plan.notes.append(
                f"BLOCKED {n_rm} removals ({ratio:.0%} of {base} managed items) > "
                f"safety ratio {self.config.removal_safety_ratio:.0%}. Set "
                "'confirm_large_removal: true' to allow, or investigate Spotify first."
            )
            plan.removes.clear()
            return
        if n_rm > self.config.max_removals_per_sync:
            plan.notes.append(
                f"capping removals at max_removals_per_sync={self.config.max_removals_per_sync}"
                f" (had {n_rm}); the rest go next sync"
            )
            del plan.removes[self.config.max_removals_per_sync :]

    # ================================================================== #
    # Phase 3
    # ================================================================== #
    def _apply(self, plan: SyncPlan, errors: list[str]) -> str:
        added = removed = reordered = 0
        quota_stop = False

        for add in plan.adds:
            if not self.quota.can_afford("playlistItems.insert"):
                quota_stop = True
                log.warning("quota budget reached; %d adds deferred to next sync",
                            len(plan.adds) - added)
                break
            try:
                res = self.youtube.insert_playlist_item(
                    self.config.youtube_playlist_id,
                    add.video.video_id,
                    add.target_position if self.config.manage_order else None,
                )
            except QuotaExceededError:
                quota_stop = True
                log.warning("YouTube reported quota exceeded; stopping adds")
                break
            except SyncError as exc:
                errors.append(f"add {add.video.video_id} failed: {exc}")
                log.error("failed to add %s: %s", add.video.video_id, exc)
                continue
            self.db.record_managed_add(
                add.track.track_id, add.track.occurrence, add.video.video_id,
                res["playlist_item_id"], res.get("position"),
                reason=f"{add.tier.value} match {add.score:.2f}",
            )
            self.db.upsert_mapping(
                add.track, add.video.video_id, add.video.title,
                add.video.channel_title, add.score,
                add.tier.value if add.tier != MatchTier.CACHED else "auto",
                "override" if add.tier == MatchTier.OVERRIDE else "auto",
            )
            added += 1
            log.info("MATCH %.2f\n  Spotify: %s - %s\n  YouTube: %s [%s]",
                     add.score, add.track.primary_artist, add.track.name,
                     add.video.channel_title or "?", add.video.title)

        if plan.large_removal_blocked:
            log.warning("removals blocked by safety ratio; see plan notes")
        for rm in plan.removes:
            if not self.quota.can_afford("playlistItems.delete"):
                quota_stop = True
                log.warning("quota budget reached; %d removals deferred",
                            len(plan.removes) - removed)
                break
            try:
                if rm.playlist_item_id:
                    self.youtube.delete_playlist_item(rm.playlist_item_id)
            except QuotaExceededError:
                quota_stop = True
                break
            except SyncError as exc:
                errors.append(f"remove {rm.youtube_video_id} failed: {exc}")
                log.error("failed to remove %s: %s", rm.youtube_video_id, exc)
                continue
            self._deactivate_by_removal(rm)
            removed += 1
            log.info("REMOVED %s (Spotify %s#%d): %s", rm.youtube_video_id,
                     rm.spotify_track_id, rm.occurrence, rm.reason)

        if self.config.manage_order and not quota_stop:
            reordered = self._reconcile_order(errors)

        self._record_unmatched_summary(plan)

        clean = not quota_stop and not errors and not plan.quota_limited
        if clean:
            self.db.set_meta("last_success_at", _now())
        self.db.set_meta("last_result", "success" if clean else "partial")
        plan_counts = (added, removed, reordered)
        plan.notes.append(f"applied: +{added} -{removed} ~{reordered}")
        # stash for _finish
        self._last_apply_counts = plan_counts
        return "partial" if (quota_stop or errors) else "success"

    def _deactivate_by_removal(self, rm: PlannedRemove) -> None:
        for row in self.db.active_managed_items():
            if (
                row["spotify_track_id"] == rm.spotify_track_id
                and row["occurrence"] == rm.occurrence
                and row["youtube_video_id"] == rm.youtube_video_id
            ):
                self.db.deactivate_managed_item(row["id"], rm.reason)
                return

    def _reconcile_order(self, errors: list[str]) -> int:
        """Best-effort: move at most N managed items toward Spotify order."""
        desired = self.db.get_meta_json(_DESIRED_KEY) or []
        desired_seq = [(d["track_id"], d["occurrence"]) for d in desired]
        managed = {
            (r["spotify_track_id"], r["occurrence"]): r
            for r in self.db.active_managed_items()
        }
        current = self.youtube.list_playlist_items(self.config.youtube_playlist_id)
        current_item_ids = [it["playlist_item_id"] for it in current]
        target_item_ids = [
            managed[k]["playlist_item_id"]
            for k in desired_seq
            if k in managed and managed[k]["playlist_item_id"]
        ]
        moves = 0
        budget = self.config.max_reorders_per_sync
        for target_pos, item_id in enumerate(target_item_ids):
            if moves >= budget:
                break
            try:
                cur_pos = current_item_ids.index(item_id)
            except ValueError:
                continue
            if cur_pos == target_pos:
                continue
            if not self.quota.can_afford("playlistItems.update"):
                break
            row = next(
                (r for r in managed.values() if r["playlist_item_id"] == item_id), None
            )
            if not row:
                continue
            try:
                self.youtube.move_playlist_item(
                    item_id, self.config.youtube_playlist_id,
                    row["youtube_video_id"], target_pos,
                )
            except QuotaExceededError:
                break
            except SyncError as exc:  # pragma: no cover - network edge
                errors.append(f"reorder failed: {exc}")
                break
            current_item_ids.remove(item_id)
            current_item_ids.insert(target_pos, item_id)
            self.db.update_managed_item(row["id"], position=target_pos)
            moves += 1
        if moves:
            log.info("reordered %d item(s) toward Spotify order", moves)
        return moves

    def _record_unmatched_summary(self, plan: SyncPlan) -> None:
        if not plan.unmatched:
            return
        log.info("%d track(s) unmatched / review required:", len(plan.unmatched))
        for mr in plan.unmatched[:25]:
            log.info("  UNMATCHED  %s - %s : %s",
                     mr.track.primary_artist, mr.track.name, mr.note)

    # ================================================================== #
    def _summary_message(self, plan: SyncPlan, errors: list[str], status: str) -> str:
        a, r, ro = getattr(self, "_last_apply_counts", (0, 0, 0))
        parts = [
            f"Unchanged: {plan.unchanged}",
            f"Added: {a}",
            f"Removed: {r}",
            f"Reordered: {ro}",
            f"Unmatched: {len(plan.unmatched)}",
        ]
        if errors:
            parts.append(f"Errors: {len(errors)}")
        return " | ".join(parts)

    def _finish(
        self,
        sync_id: int,
        started: str,
        status: str,
        *,
        plan: SyncPlan | None = None,
        errors: list[str] | None = None,
        message: str = "",
    ) -> SyncOutcome:
        finished = _now()
        errors = errors or []
        a, r, ro = getattr(self, "_last_apply_counts", (0, 0, 0))
        evaluated = plan.spotify_tracks if plan else 0
        unmatched = len(plan.unmatched) if plan else 0
        self.db.finish_sync(
            sync_id,
            status=status,
            finished_at=finished,
            tracks_evaluated=evaluated,
            additions=a,
            removals=r,
            reorders=ro,
            unmatched=unmatched,
            quota_spent=self.quota.session_spent,
            errors=errors,
            message=message,
        )
        self.db.set_meta("last_result", status)
        self.db.set_meta("last_finished_at", finished)

        if status in ("success", "dry_run"):
            log.info("Sync completed successfully" if status == "success"
                     else "Dry run completed")
        elif status == "partial":
            log.warning("Sync completed partially: %s", message)
        else:
            log.error("Sync failed: %s", message)

        self._last_apply_counts = (0, 0, 0)  # reset for next run
        return SyncOutcome(
            status=status,
            started_at=started,
            finished_at=finished,
            spotify_tracks=evaluated,
            managed_before=plan.managed_before if plan else 0,
            unchanged=plan.unchanged if plan else 0,
            added=a,
            removed=r,
            reordered=ro,
            unmatched=unmatched,
            quota_spent=self.quota.session_spent,
            errors=errors,
            message=message,
        )


def _synthetic_unmatched(track: SpotifyTrack, note: str):
    from .models import MatchResult

    return MatchResult(
        track=track, accepted=None, best_rejected=None,
        tier=MatchTier.LOW, searched=False, note=note or "unmatched",
    )
