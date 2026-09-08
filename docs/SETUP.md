# First-run walkthrough

Follow these in order. Steps 1–2 are one-time external setup; 3 onward is the
add-on.

## 0. Prerequisites

- Home Assistant OS (Supervisor) running 24/7. A Dell OptiPlex / any `amd64`
  box is ideal; `aarch64` and `armv7` also build.
- The **Samba share** add-on *or* **Studio Code Server** *or* the **Advanced
  SSH & Web Terminal** add-on installed, so you can drop two files onto the
  system.
- A computer with a web browser and Python 3.11+ for the one-time OAuth.
- A Spotify account with **Premium**.
- A Google account that **owns** (or will create) the destination YouTube
  playlist.

## 1. Spotify  → [`SPOTIFY.md`](SPOTIFY.md)

Create the developer app, add redirect URI `http://127.0.0.1:8723/`, add
yourself under *User Management*, and note:

- `spotify_client_id`
- `spotify_client_secret`
- `spotify_playlist_id`

## 2. Google / YouTube  → [`GOOGLE.md`](GOOGLE.md)

Create a Cloud project, enable **YouTube Data API v3**, configure the OAuth
consent screen and **set publishing status to *In production***, create a
**Desktop app** OAuth client, and note:

- `youtube_client_id`
- `youtube_client_secret`
- `youtube_playlist_id`
- the downloaded `client_secret_XXXX.json`

## 3. Produce the token files (on your computer)

```bash
git clone https://github.com/OWNER/spotify-youtube-sync
cd spotify-youtube-sync
python -m pip install -r scripts/requirements-auth.txt

python scripts/authorize.py both \
    --spotify-client-id     "<spotify_client_id>" \
    --spotify-client-secret "<spotify_client_secret>" \
    --google-client-secrets "/path/to/client_secret_XXXX.json" \
    --out ./tokens
```

Two browser prompts (Spotify, then Google). Result:

```
tokens/spotify_token.json
tokens/youtube_token.json
```

> If the machine running this can't open a browser, add `--no-browser` and open
> the printed URLs manually (they must resolve to the same machine – or use an
> SSH tunnel, see *Alternative* below).

## 4. Add the repository to Home Assistant

**Settings → Add-ons → Add-on store → ⋮ (top right) → Repositories** →
paste `https://github.com/OWNER/spotify-youtube-sync` → **Add** → close.

## 5. Install and pre-configure

1. In the store, open **“Spotify → YouTube Playlist Sync”** → **Install**
   (builds locally; a few minutes).
2. **Configuration** tab: fill the six credential/playlist fields. Leave:
   - `dry_run: true`
   - `strict_mirror: true`
   - `match_threshold: 0.85`
   **Save.**
3. **Start** the add-on once (it will fail the first sync with "missing OAuth
   token" – that's expected; it creates the config folder).
4. **Stop** the add-on.

## 6. Install the token files

Using Samba (`\\homeassistant\`), VS Code Server, or SSH, copy the two files
into the add-on's config folder:

```
/addon_configs/XXXXXXXX_spotify_youtube_sync/spotify_token.json
/addon_configs/XXXXXXXX_spotify_youtube_sync/youtube_token.json
```

The `XXXXXXXX_` prefix is assigned by Home Assistant – look for the folder that
ends in `spotify_youtube_sync` under `addon_configs`. Over SSH:

```bash
ls /addon_configs | grep spotify_youtube_sync
cp spotify_token.json youtube_token.json /addon_configs/<that folder>/
```

## 7. Dry run

1. **Start** the add-on.
2. Open the **Log** tab. You should see:
   ```
   Spotify -> YouTube sync starting (trigger=startup, dry_run=True)
   Refreshed Spotify access token
   Refreshed YouTube access token
   Spotify playlist '<name>': 247 items (247 playable tracks), snapshot 1a2b3c...
   Spotify playlist: 247 tracks
   ... Sync plan:
   ...   To add: 247
   ...   Unmatched / review: 3
   dry_run=true: no changes applied
   Dry run completed
   ```
3. Open the **Playlist Sync** panel in the sidebar. Review:
   - the plan / recent sync row,
   - the **Unmatched / review required** table.
4. For important tracks that didn't match, add a **manual override** (panel box:
   Spotify track ID → YouTube video ID).

The token copies in `/addon_configs/...` can be deleted now – the add-on has
imported them into `/data`.

## 8. Go live

1. **Configuration** → set `dry_run: false` → **Save**.
2. On the panel, click **Sync now** (or restart the add-on).
3. Watch the **Log**:
   ```
   MATCH 0.96
     Spotify: Chris Stapleton - Tennessee Whiskey
     YouTube: Chris Stapleton - Tennessee Whiskey (Official Audio)
   ...
   Added: 60
   Removed: 0
   Unmatched: 3
   Sync completed partially: ...   (if the initial batch hit the quota budget)
   Next sync in 6 hours
   ```
4. For a big playlist the first sync will report **partial** and stop at the
   daily quota. That's fine – it resumes automatically every 6 h and after each
   Pacific-midnight quota reset until everything is added.

## 9. Steady state

Nothing more to do. The add-on syncs on every start and every
`sync_interval_hours`. A normal "nothing changed" sync logs a few lines and
spends almost no quota.

## 10. Backups

Home Assistant snapshots include the add-on's `/data`
(`state.sqlite`, `spotify_token.json`, `youtube_token.json`). Restoring a
snapshot restores the full mapping state and you won't re-search anything.

---

## Alternative: authorize directly on the Home Assistant box (SSH tunnel)

If you can't or won't run `scripts/authorize.py` elsewhere:

```bash
# from your laptop, tunnel the loopback callback port to the HA host
ssh -L 8723:127.0.0.1:8723 root@homeassistant.local

# in another terminal, open a shell in the add-on container
#   (Add-on -> ... -> "Terminal", requires Protection mode off)
python -m app authorize both --no-browser
```

Open the printed URLs in your laptop's browser (the tunnel routes the
`127.0.0.1:8723` redirect back to the add-on). Tokens land straight in `/data`.
