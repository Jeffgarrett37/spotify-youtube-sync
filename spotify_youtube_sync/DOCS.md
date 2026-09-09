# Spotify → YouTube Playlist Sync

One-way authoritative mirror: a **Spotify playlist** (source of truth) → a
**YouTube playlist**. Syncs on startup and every `sync_interval_hours` (default
6). It only ever deletes YouTube items it added itself, and never acts on an
incomplete Spotify read.

## Before you start

Complete the external setup and produce the two OAuth token files:

- Spotify developer app + authorization → repo `docs/SPOTIFY.md`
- Google Cloud project + YouTube OAuth → repo `docs/GOOGLE.md`
- Full walkthrough → repo `docs/SETUP.md`

## Configuration

| Option | Default | Notes |
|---|---|---|
| `spotify_client_id` / `spotify_client_secret` | – | Spotify developer app |
| `spotify_playlist_id` | – | 22-char ID from the playlist share link |
| `youtube_client_id` / `youtube_client_secret` | – | Google **Desktop app** OAuth client |
| `youtube_playlist_id` | – | `PL…`, owned by the authorized Google account |
| `sync_interval_hours` | `6` | 1–168 |
| `strict_mirror` | `true` | `false` = preserve manually-added items more aggressively |
| `match_threshold` | `0.85` | min confidence to auto-add |
| `dry_run` | `false` | compute the plan, change nothing |
| `log_level` | `info` | `trace`/`debug`/`info`/`warning`/`error` |
| `manage_order` | `false` | keep YouTube ordered like Spotify (extra quota) |
| `max_reorders_per_sync` | `20` | |
| `max_removals_per_sync` | `50` | hard cap on deletions per sync |
| `removal_safety_ratio` | `0.34` | block syncs removing more than this share of managed items |
| `confirm_large_removal` | `false` | allow a sync past the safety ratio |
| `daily_quota_budget` | `9000` | YouTube API units/day (project default is 10000) |
| `unmatched_retry_hours` | `168` | delay before re-searching a failed match |
| `region_code` | `US` | YouTube search region |

## Installing the OAuth tokens

After the first start, this add-on has a config folder visible over Samba at
`\\homeassistant\addon_configs\<slug>_spotify_youtube_sync\`. Copy
`spotify_token.json` and `youtube_token.json` into it and restart – they are
imported into persistent `/data` automatically (and can then be deleted from the
config folder).

## Recommended first run

1. Install, set the 6 credential/playlist options, keep `dry_run: true`, start.
2. Add the token files (above), restart.
3. Read the **Log** and the **Playlist Sync** sidebar panel: review the plan and
   the unmatched list; add manual overrides for anything important.
4. Set `dry_run: false`, save, click **Sync now** on the panel.
5. Leave it running. Large initial playlists fill in over several days as the
   YouTube quota resets.

## Panel / manual control

The **Playlist Sync** sidebar panel shows health, last/next sync, quota use,
recent syncs, the unmatched list and the overrides list. It has buttons for
**Sync now**, **clear & retry all** unmatched, and add/remove **override**.

## Logs

`INFO` shows the summary and each new match. `DEBUG`/`TRACE` add per-query and
per-candidate detail. Secrets and tokens are never logged.

## Support

Issues: https://github.com/Jeffgarrett37/spotify-youtube-sync/issues
