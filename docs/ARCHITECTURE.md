# Architecture

## Process model

`run.sh` (bashio) reads the add-on options into `SYNC_*` env vars, imports any
token files staged in `/config` into `/data`, then execs `python -m app`.

`app.main.Application` starts two things and then sleeps until SIGTERM:

- **`Scheduler`** (`app/scheduler.py`) – a daemon thread. Runs a sync
  immediately (`trigger=startup`), then waits `sync_interval_hours` or until
  woken by a manual trigger, forever. Any exception in a sync is logged and the
  loop continues.
- **`StatusServer`** (`app/httpserver.py`) – a `ThreadingHTTPServer` on
  `:8099`, reachable only through Home Assistant ingress. Serves the dashboard,
  `/health` JSON, `/healthz` (watchdog), and POST endpoints for *Sync now*,
  clearing the unmatched store, and adding/removing overrides.

A sync is serialised by a lock, so the scheduled run and a *Sync now* click can
never overlap.

## One sync: the two-phase engine (`app/sync_engine.py`)

```
run()
 ├─ Phase 1  _load_source()
 │    ├─ GET /playlists/{id}?fields=snapshot_id,tracks.total        (1 Spotify call)
 │    ├─ if snapshot == stored snapshot and we have a stored,
 │    │    validated desired-state and no unmatched retry is due:
 │    │        reuse the stored desired-state, no pagination        ← cheap path
 │    └─ else: page /playlists/{id}/items fully, then:
 │         ├─ re-read snapshot; if it changed mid-read → retry (≤3)
 │         ├─ assert len(items) == playlist.total  → else IncompleteSpotifyFetchError
 │         └─ persist snapshot_id + desired-state JSON to /data
 │    (any SourceStateError / AuthError / ApiUnavailable here → status=failed,
 │     ZERO YouTube writes)
 │
 ├─ Phase 2  _build_plan(tracks, source_validated=True)
 │    ├─ playlistItems.list  → current YouTube playlist                (1 unit)
 │    ├─ videos.list (batched) → availability of every referenced video (≈1 unit/50)
 │    ├─ for each desired (track, occurrence):
 │    │     managed record exists?
 │    │       yes + video ok + item present + not superseded → UNCHANGED
 │    │       yes but video dead / item vanished / override changed → REMOVE + re-add
 │    │       no  → resolver.resolve(track):
 │    │              override  →  use it (no search)
 │    │              cache hit →  use it (no search)
 │    │              sticky unmatched (metadata unchanged, within retry window)
 │    │                        →  skip (no search)
 │    │              else      →  search.list (+ videos.list) → matcher → HIGH? add : unmatched
 │    ├─ orphans (managed items no desired occurrence claimed) → REMOVE
 │    │     strict: always;  non-strict: only the clean single recorded copy
 │    └─ safety brakes:
 │         • source not validated              → drop ALL removals
 │         • removals / managed_before > ratio and ≥5 and not confirmed → drop ALL removals
 │         • removals > max_removals_per_sync   → truncate
 │
 ├─ log the plan (always, before any write)
 ├─ dry_run → stop here, status=dry_run
 │
 └─ Phase 3  _apply(plan)
      ├─ additions   (stop cleanly when quota can't afford the next insert)
      ├─ removals    (unless blocked; delete only recorded playlist_item_ids)
      ├─ reorders    (only if manage_order; ≤ max_reorders_per_sync)
      └─ persist last_success / last_result; status = success | partial
```

### Why "validate then act"

The single most important safety property: **a failed or incomplete Spotify
read must never look like "the playlist is empty".** So:

1. The Spotify client only returns tracks if pagination retrieved exactly
   `playlist.tracks.total` items with a stable `snapshot_id`. Otherwise it
   raises an `IncompleteSpotifyFetchError` / `SourceStateError`.
2. The engine catches every such error *before* Phase 2 and finishes with
   `status=failed` having touched nothing.
3. Even inside Phase 2, `source_validated` is threaded through and re-checked in
   `_apply_safety_brakes`; removals are dropped if it's ever false.
4. A genuinely large shrink (playlist really did lose a third of its tracks) is
   still gated behind `removal_safety_ratio` + `confirm_large_removal` so a
   surprising change pauses for a human.

## Matching (`app/normalize.py`, `app/matcher.py`, `app/resolver.py`)

`resolver.resolve()` is the quota gate. It only calls `search.list` when there
is no override, no cached mapping, and no still-valid "unmatched" record – and
only if the remaining daily budget can also afford to hydrate + insert the
result.

`matcher.score_candidate()` produces a 0–1 score:

```
base = 0.42·title_sim + 0.34·artist_sim + 0.24·duration_score
```

- `title_sim` – best of difflib ratio + order-independent token-set ratio, over
  the normalized full title and the parenthetical-stripped "core"; containment
  of the Spotify title inside the YouTube title short-circuits high.
- `artist_sim` – best match of any Spotify artist against the channel name and
  the `Artist -` prefix of the video title; `- Topic` and `VEVO` suffixes are
  stripped first.
- `duration_score` – 1.0 within ±12 s, linear decay to 0 at ±75 s (which also
  disqualifies), small allowance for a longer music-video runtime.

Then **bonuses** (official audio, official video, `- Topic` channel, artist /
VEVO channel, album name present, ISRC in description) and **penalties**
(cover, karaoke/instrumental, live when the track isn't live, remix when the
track isn't a remix, slowed/reverb/sped-up/nightcore, alt edits, lyric video,
weak artist). **Hard disqualifiers** (score capped ≤0.30): wrong artist, wrong
title, duration off by ≥75 s, or a killer phrase (`karaoke`, `reaction`,
`tutorial`, `cover by`, `made famous by`, `full album`, `1 hour`, …).

Tiers: **HIGH** (≥`match_threshold`, plus artist/title/duration floors) → added;
**MEDIUM** (≥`match_threshold`−0.20, floor 0.55) → stored for review, never
added; **LOW** → rejected, stored so it isn't re-searched every run.

## Persistence (`app/database.py`)

Single SQLite file `/data/state.sqlite`, WAL mode, forward-only numbered
migrations (`_MIGRATIONS`, `schema_version` table). Tables:

| table | purpose |
|---|---|
| `track_mapping` | the Spotify→YouTube match cache (id, artist, title, album, duration, ISRC, video id/title/channel, score, tier, source, first-matched, last-verified) |
| `managed_items` | every YouTube item the add-on inserted: spotify track id + occurrence, video id, `playlist_item_id`, position, `active`, reason, added/removed timestamps |
| `overrides` | manual `spotify_track_id → youtube_video_id` |
| `unmatched_tracks` | reason, best candidate id/title/score, tier, metadata fingerprint, first-seen, last-attempt, retry count |
| `sync_history` | start/finish, status, counts, quota spent, errors, message |
| `quota_usage` | units spent per `America/Los_Angeles` day |
| `kv` | `spotify_snapshot_id`, `spotify_desired_state`, `last_success_at`, `next_sync_at`, `last_result`, … |

Duplicates: identity is `(spotify_track_id, occurrence)` where `occurrence` is
the 0-based index among copies of that track, assigned in playlist order. Two
copies in Spotify ⇒ two `managed_items` rows ⇒ two YouTube inserts of the same
video id.

## Quota (`app/quota.py`)

`QuotaTracker` keeps a running total per Pacific day in `quota_usage`.
`require()` raises `QuotaExceededError` before an unaffordable call; `charge()`
is applied only after a call succeeds. The engine and resolver check
affordability up front so a sync always ends on a clean boundary and the next
run continues from the persisted `managed_items` / `track_mapping` state.

## Failure handling summary

| condition | behaviour |
|---|---|
| Spotify/YouTube 5xx, network blip | tenacity exponential backoff (≤5 tries) |
| HTTP 429 | sleep `Retry-After` (capped) then retry |
| Spotify `invalid_grant` / Google `RefreshError` | `AuthError`, sync fails, clear log message, no writes |
| Spotify playlist private/deleted | `PlaylistUnavailableError`, no writes |
| Incomplete pagination / snapshot unstable | `IncompleteSpotifyFetchError`, no writes |
| YouTube quota exceeded (local budget or server) | stop, persist progress, `status=partial`, resume next run |
| Cached YouTube video removed | mapping dropped, track re-searched (quota permitting), managed item replaced |
| Malformed API JSON | `MalformedResponseError`, sync fails, no partial writes |
| SQLite error | `DatabaseError` surfaces; scheduler loop survives and retries next cycle |
