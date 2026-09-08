# Spotify → YouTube Playlist Sync (Home Assistant add-on)

A Home Assistant OS add-on that keeps **one YouTube playlist mirroring one
Spotify playlist**. Spotify is the source of truth. It runs on the machine you
already have on 24/7, syncs on startup and then every 6 hours, and is
deliberately conservative: it would rather skip a song than add the wrong one,
and it will **never** delete a YouTube item it didn't add itself.

> Replace `OWNER` in `repository.yaml` and `spotify_youtube_sync/config.yaml`
> with your GitHub username/org before publishing the repo.

---

## What it does

| Spotify change | YouTube result |
|---|---|
| Track added | Best-matching official video/audio is found and added |
| Track removed | The item this add-on added for it is removed |
| Nothing changed | Nothing is written (no wasted API calls) |
| Playlist fetch failed / incomplete | **No deletions at all** – sync aborts safely |

Key properties:

- **Two-phase sync** – the complete Spotify playlist is fetched and validated
  (every page retrieved, count matches the playlist's own total, snapshot stable)
  *before* any YouTube write. A partial Spotify read can never be interpreted as
  "the playlist is now empty".
- **Managed-item tracking** – every YouTube item the add-on inserts is recorded
  in SQLite with the Spotify track that caused it. Removals only ever touch
  those records. Videos you added by hand are left alone.
- **Match cache** – once a Spotify track is matched to a YouTube video the
  mapping is cached forever; it is not re-searched on later syncs or on restart.
- **Quota-aware** – YouTube Data API costs are metered per US-Pacific day. When
  the budget runs out the sync stops cleanly, writes its progress and resumes
  next time. A 500-song initial migration spreads over several days by design.
- **Dry-run mode**, **manual mapping overrides**, an **unmatched/review** store,
  and a small status page in the Home Assistant sidebar with a **Sync now**
  button.

Full design notes: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Repository layout

```
spotify-youtube-sync/
├── repository.yaml            # Home Assistant add-on repository metadata
├── README.md
├── docs/
│   ├── SETUP.md               # the end-to-end first-run walkthrough
│   ├── SPOTIFY.md             # Spotify developer app + authorization
│   ├── GOOGLE.md              # Google Cloud project + YouTube OAuth
│   └── ARCHITECTURE.md        # how the sync engine and matcher work
├── scripts/
│   ├── authorize.py           # one-time OAuth helper (run on your desktop)
│   └── requirements-auth.txt
└── spotify_youtube_sync/      # the add-on itself
    ├── config.yaml            # add-on manifest + user options schema
    ├── build.yaml             # base images per architecture
    ├── Dockerfile
    ├── run.sh                 # entrypoint (bashio: options -> env, token import)
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── pyproject.toml         # pytest + ruff config
    ├── translations/en.yaml
    ├── app/
    │   ├── __main__.py        # `python -m app [run|sync|status|authorize]`
    │   ├── main.py            # wiring: db + scheduler + status server
    │   ├── config.py          # options -> Config, match thresholds
    │   ├── errors.py          # explicit error hierarchy
    │   ├── models.py          # dataclasses (SpotifyTrack, YouTubeVideo, SyncPlan…)
    │   ├── normalize.py       # title/artist normalization + similarity
    │   ├── matcher.py         # candidate scoring + confidence tiers
    │   ├── resolver.py        # override > cache > unmatched > search
    │   ├── spotify_client.py  # minimal Spotify Web API client (read-only)
    │   ├── youtube_client.py  # YouTube Data API v3 client (quota-metered)
    │   ├── quota.py           # per-Pacific-day quota ledger
    │   ├── database.py        # SQLite schema + migrations + DAO
    │   ├── sync_engine.py     # the two-phase engine
    │   ├── scheduler.py       # startup + every-N-hours loop
    │   ├── status.py          # health snapshot
    │   ├── httpserver.py      # ingress status/control page
    │   ├── oauth_flows.py     # reusable loopback OAuth flows
    │   └── authorize.py       # `python -m app authorize`
    └── tests/                 # pytest, external APIs mocked
```

---

## Configuration options

Set these in the add-on's **Configuration** tab.

| Option | Default | Notes |
|---|---|---|
| `spotify_client_id` | `""` | from your Spotify developer app |
| `spotify_client_secret` | `""` | secret field |
| `spotify_playlist_id` | `""` | 22-char ID from the playlist's share link |
| `youtube_client_id` | `""` | Google OAuth **Desktop app** client |
| `youtube_client_secret` | `""` | secret field |
| `youtube_playlist_id` | `""` | starts with `PL…`, must be owned by the authorized Google account |
| `sync_interval_hours` | `6` | 1–168; a sync also always runs on startup |
| `strict_mirror` | `true` | see [strict vs non-strict](#strict-vs-non-strict) |
| `match_threshold` | `0.85` | minimum confidence to auto-add (0–1) |
| `dry_run` | `false` | compute everything, change nothing |
| `log_level` | `info` | `trace` \| `debug` \| `info` \| `warning` \| `error` |
| `manage_order` | `false` | keep YouTube ordered like Spotify (costs extra quota) |
| `max_reorders_per_sync` | `20` | cap on move operations per sync |
| `max_removals_per_sync` | `50` | hard cap on deletions per sync |
| `removal_safety_ratio` | `0.34` | block a sync that would remove more than this fraction of managed items |
| `confirm_large_removal` | `false` | set `true` to allow a sync past the safety ratio |
| `daily_quota_budget` | `9000` | YouTube Data API units/day the add-on may spend (project default is 10000) |
| `unmatched_retry_hours` | `168` | how long before re-searching a track that failed to match |
| `region_code` | `US` | two-letter region for YouTube search relevance |

### Match confidence thresholds

The matcher scores each candidate 0–1.

| Band (defaults) | Behaviour |
|---|---|
| `score ≥ 0.85` (+ artist ≥ 0.60, title ≥ 0.70, duration sane) | **HIGH** – added automatically |
| `0.65 ≤ score < 0.85` | **MEDIUM** – *not* added; stored as "review required" with the near-miss candidate |
| `score < 0.65` or disqualified (karaoke, cover, reaction, wrong artist, duration off by ≥75 s, …) | **LOW** – rejected, stored so it isn't re-searched constantly |

Thresholds live in `spotify_youtube_sync/app/config.py`
(`DEFAULT_AUTO_ADD_THRESHOLD`, `MEDIUM_THRESHOLD_DELTA`, `MEDIUM_THRESHOLD_FLOOR`,
`DURATION_GRACE_SECONDS`, `DURATION_HARD_LIMIT_SECONDS`). Scoring weights are in
`app/matcher.py` (`W_TITLE=0.42`, `W_ARTIST=0.34`, `W_DURATION=0.24`).

### strict vs non-strict

Both modes **only ever delete YouTube items recorded as managed by this add-on,
with a known Spotify track behind them.** They differ for an "orphan" (a managed
item whose Spotify track was removed) that no longer looks clean:

- `strict_mirror: true` – delete the managed item. If you manually added a
  *second* copy of the same video, the manual copy is left in place.
- `strict_mirror: false` – only delete the orphan if it is still the single,
  intact recorded copy (right `playlist_item_id`, appears once). Otherwise the
  add-on just stops managing it and leaves YouTube untouched.

---

## Setup

The detailed walkthrough with current (2026) UI steps is in
[`docs/SETUP.md`](docs/SETUP.md). Short version:

1. **Spotify** – create a developer app ([`docs/SPOTIFY.md`](docs/SPOTIFY.md)).
   Add redirect URI `http://127.0.0.1:8723/`. Note the client ID/secret and the
   playlist ID. Your Spotify account must have **Premium** (Spotify's Feb-2026
   rule for development-mode apps) and you must add yourself under the app's
   **User Management**.
2. **Google/YouTube** – create a Cloud project, enable **YouTube Data API v3**,
   configure the OAuth consent screen, set publishing status to **In production**
   (so the refresh token doesn't expire after 7 days), and create a **Desktop
   app** OAuth client ([`docs/GOOGLE.md`](docs/GOOGLE.md)). Note the client
   ID/secret and the destination playlist ID.
3. **Authorize once** on a computer with a browser:
   ```bash
   pip install -r scripts/requirements-auth.txt
   python scripts/authorize.py both \
       --spotify-client-id  <ID> --spotify-client-secret <SECRET> \
       --google-client-secrets ~/Downloads/client_secret_xxx.json \
       --out ./tokens
   ```
   This writes `tokens/spotify_token.json` and `tokens/youtube_token.json`.
4. **Install the add-on** (below), put both token files into its config folder,
   configure the options, start it.

### Install on Home Assistant OS

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**, paste
   `https://github.com/OWNER/spotify-youtube-sync`, **Add**.
2. Find **“Spotify → YouTube Playlist Sync”** in the store and click **Install**
   (it builds the image locally for `amd64` – takes a few minutes on an
   OptiPlex; `aarch64` and `armv7` are also supported).
3. Open the add-on → **Configuration**, fill in the six credential/playlist
   fields, leave `dry_run: true` for now, **Save**.
4. Put the OAuth tokens where the add-on can import them. Using the **Samba
   share** or **Studio Code Server** / **SSH** add-on, copy both files into:
   ```
   /addon_configs/<slug>_spotify_youtube_sync/spotify_token.json
   /addon_configs/<slug>_spotify_youtube_sync/youtube_token.json
   ```
   (the folder is created after the first start; the `<slug>` prefix is assigned
   by Home Assistant). On the next start the add-on copies them into its
   persistent `/data` and you can delete the copies from the config folder.
5. **Start** the add-on. Open the **Log** tab – you should see it authenticate,
   fetch both playlists and print a **dry-run plan**.
6. Review the plan and the **unmatched** list on the add-on's sidebar panel
   (**Playlist Sync**). Add manual overrides for anything important that didn't
   match.
7. Set `dry_run: false`, **Save**, then click **Sync now** on the panel (or
   restart). Live syncing now happens on that click and every
   `sync_interval_hours`.
8. **Back up** – Home Assistant's normal snapshots include the add-on's `/data`
   (SQLite state + tokens). That's all you need to restore.

---

## Local development & validation

```bash
cd spotify_youtube_sync
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

pytest                # 68 tests, no network / credentials needed
ruff check .          # lint

# exercise the CLI without Home Assistant (writes to ./_data):
SYNC_DATA_DIR=./_data python -m app status
SYNC_DATA_DIR=./_data python -m app sync          # fails cleanly with no config

# build the add-on image like the Supervisor does:
docker run --rm -it -v "$PWD":/data ghcr.io/home-assistant/amd64-builder \
    --target /data --amd64 --test
```

Run the whole thing locally against real playlists:

```bash
cd spotify_youtube_sync
export SYNC_DATA_DIR=./_data
export SYNC_SPOTIFY_CLIENT_ID=... SYNC_SPOTIFY_CLIENT_SECRET=... SYNC_SPOTIFY_PLAYLIST_ID=...
export SYNC_YOUTUBE_CLIENT_ID=... SYNC_YOUTUBE_CLIENT_SECRET=... SYNC_YOUTUBE_PLAYLIST_ID=...
export SYNC_DRY_RUN=true
mkdir -p ./_data
cp ../tokens/*.json ./_data/
python -m app sync        # one dry-run sync, prints the plan as JSON
```

---

## Manual mapping overrides

Tell the add-on exactly which YouTube video a Spotify track should use. Overrides
beat automatic matching and are stored in `/data/state.sqlite`.

- **From the sidebar panel** – the *Manual mapping override* box: paste the
  Spotify track ID and the YouTube video ID, click *add / update*. Remove with
  the *remove* button.
- **From a shell** in the add-on's container (Advanced → *Protection mode* off,
  then the add-on's *Terminal*, or `docker exec`):
  ```bash
  python - <<'PY'
  from app.config import Config
  from app.database import Database
  db = Database(Config.from_env().db_path)
  db.put_override("SPOTIFY_TRACK_ID", "YOUTUBE_VIDEO_ID", "why")
  db.close()
  PY
  ```

The next sync removes any previously-managed item for that track and adds the
override target.

## Unmatched / review

Tracks that scored MEDIUM or LOW are listed on the panel with the reason and the
best candidate found. They are retried automatically after
`unmatched_retry_hours` or immediately if the Spotify track's metadata changes.
Use **clear & retry all** on the panel to force a fresh attempt on the next sync.

---

## Limitations

- **Ordering** is best-effort and off by default. YouTube charges 50 quota units
  per item move, so `manage_order` only nudges up to `max_reorders_per_sync`
  items toward Spotify order per run and never rewrites the whole playlist.
- **Duplicates** – a Spotify playlist containing the same track twice results in
  two YouTube items (same video ID twice). Each occurrence is tracked
  separately by playlist position.
- **Initial migration** of a few hundred songs takes several days on the default
  10 000-unit YouTube quota (~60 new matches/day). This is expected; progress is
  persisted and resumes each run. Request a quota increase from Google to go
  faster.
- **Spotify development mode** caps the app at 5 users and requires the owner to
  keep an active **Premium** subscription (Spotify, Feb 2026). This add-on only
  needs the one account, so that's fine – just don't let Premium lapse.
- **Google OAuth** must be set to *In production* or the refresh token dies
  after 7 days and you'll have to re-authorize.
- The add-on does **not** create a full Home Assistant integration (no entities
  or services). Health is exposed via the sidebar page and `/data` state; use
  the **Sync now** button or restart for a manual sync. This is intentional to
  keep v1 maintainable.
- No MQTT. Not needed for a single service like this.

## Security notes

- Client secrets and OAuth tokens live only in `/data` (and briefly in the
  add-on config folder during import) – never in the image or this repo.
  `.gitignore` blocks `*_token.json`, `client_secret*.json`, `options.json`.
- Secrets/tokens/authorization codes are never written to the log.
- The add-on requests **read-only** Spotify scopes and has no code path that
  writes to Spotify.
- The status page is served only through Home Assistant ingress (authenticated
  by Home Assistant); no port is exposed.

## License

MIT – see [`LICENSE`](LICENSE).
