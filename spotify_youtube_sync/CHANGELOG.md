# Changelog

## 1.0.0

- Initial release.
- One-way authoritative mirror: Spotify playlist -> YouTube playlist.
- Runs on startup and every `sync_interval_hours` (default 6).
- Two-phase sync: validate complete Spotify source state before any YouTube writes.
- Persistent SQLite state in `/data` with versioned schema migrations.
- Cached Spotify-track -> YouTube-video mappings; searches only on cache miss.
- Conservative matcher with configurable confidence thresholds.
- Managed-item tracking so only add-on-created YouTube items are ever removed.
- `dry_run`, `strict_mirror`, manual mapping overrides, unmatched-track store.
- Quota-aware: per-Pacific-day YouTube budget with safe resume for large initial syncs.
- Ingress status page with a "Sync now" button; `/healthz` watchdog endpoint.
