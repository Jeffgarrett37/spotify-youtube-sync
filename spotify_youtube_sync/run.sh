#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
# ---------------------------------------------------------------------------
# Spotify -> YouTube Playlist Sync : add-on entrypoint
#
# Responsibilities:
#   1. Read add-on options via bashio and export them as SYNC_* env vars.
#   2. Bootstrap OAuth token files from the Samba-visible /config folder into
#      the persistent /data folder on first run (never overwrites /data).
#   3. Hand off to the long-lived Python process (scheduler + status server).
# Secrets are never echoed.
# ---------------------------------------------------------------------------
set -euo pipefail

DATA_DIR="/data"
CONFIG_DIR="/config"          # == /addon_configs/<slug> over Samba
mkdir -p "${DATA_DIR}"

bashio::log.info "Starting Spotify -> YouTube Playlist Sync"

# --- token bootstrap -------------------------------------------------------
# Users complete OAuth on a normal computer with scripts/authorize.py and then
# drop the resulting *_token.json files into the add-on config share. We copy
# them into /data exactly once (if /data does not already have them).
for name in spotify_token.json youtube_token.json; do
  if [ ! -f "${DATA_DIR}/${name}" ] && [ -f "${CONFIG_DIR}/${name}" ]; then
    cp "${CONFIG_DIR}/${name}" "${DATA_DIR}/${name}"
    chmod 600 "${DATA_DIR}/${name}"
    bashio::log.info "Imported ${name} from add-on config share into /data"
  fi
done

# --- options -> environment ---------------------------------------------------
export SYNC_DATA_DIR="${DATA_DIR}"
export SYNC_SPOTIFY_CLIENT_ID="$(bashio::config 'spotify_client_id')"
export SYNC_SPOTIFY_CLIENT_SECRET="$(bashio::config 'spotify_client_secret')"
export SYNC_SPOTIFY_PLAYLIST_ID="$(bashio::config 'spotify_playlist_id')"
export SYNC_YOUTUBE_CLIENT_ID="$(bashio::config 'youtube_client_id')"
export SYNC_YOUTUBE_CLIENT_SECRET="$(bashio::config 'youtube_client_secret')"
export SYNC_YOUTUBE_PLAYLIST_ID="$(bashio::config 'youtube_playlist_id')"
export SYNC_INTERVAL_HOURS="$(bashio::config 'sync_interval_hours')"
export SYNC_STRICT_MIRROR="$(bashio::config 'strict_mirror')"
export SYNC_MATCH_THRESHOLD="$(bashio::config 'match_threshold')"
export SYNC_DRY_RUN="$(bashio::config 'dry_run')"
export SYNC_LOG_LEVEL="$(bashio::config 'log_level')"
export SYNC_MANAGE_ORDER="$(bashio::config 'manage_order')"
export SYNC_MAX_REORDERS_PER_SYNC="$(bashio::config 'max_reorders_per_sync')"
export SYNC_MAX_REMOVALS_PER_SYNC="$(bashio::config 'max_removals_per_sync')"
export SYNC_REMOVAL_SAFETY_RATIO="$(bashio::config 'removal_safety_ratio')"
export SYNC_CONFIRM_LARGE_REMOVAL="$(bashio::config 'confirm_large_removal')"
export SYNC_DAILY_QUOTA_BUDGET="$(bashio::config 'daily_quota_budget')"
export SYNC_UNMATCHED_RETRY_HOURS="$(bashio::config 'unmatched_retry_hours')"
export SYNC_REGION_CODE="$(bashio::config 'region_code')"
export SYNC_INGRESS_PORT="8099"

bashio::log.info "dry_run=${SYNC_DRY_RUN} strict_mirror=${SYNC_STRICT_MIRROR} interval=${SYNC_INTERVAL_HOURS}h log_level=${SYNC_LOG_LEVEL}"

cd /opt/app
exec python3 -m app
