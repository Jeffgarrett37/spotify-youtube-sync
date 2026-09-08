# Spotify setup

You need: a **Spotify account with an active Premium subscription** (Spotify made
this mandatory for development-mode apps in February 2026), a developer app, and
a one-time authorization that produces a refresh token.

## 1. Create the developer application

1. Go to <https://developer.spotify.com/dashboard> and log in.
2. **Create app**.
   - **App name / description**: anything (e.g. "HA playlist mirror").
   - **Redirect URIs**: add exactly
     ```
     http://127.0.0.1:8723/
     ```
     Spotify no longer accepts `http://localhost` – it must be the loopback
     literal `127.0.0.1` (or `[::1]`). The trailing slash matters; use the same
     value everywhere. If you run `scripts/authorize.py` with
     `--redirect-port`, register that port instead.
   - **Which API/SDKs**: tick **Web API**.
3. **Save**. Open the app → **Settings** and copy the **Client ID** and
   **Client secret** (click *View client secret*). These go into the add-on
   options `spotify_client_id` / `spotify_client_secret`.

## 2. Add yourself as a user

Development-mode apps only work for explicitly listed users.

1. In the app, open **User Management**.
2. Add your own name + the email address on your Spotify account.
3. Save.

(You do **not** need to apply for "extended quota mode" – that path is for
commercial apps with 250k+ users and is not available to individuals.)

## 3. Find the playlist ID

1. In the Spotify app, open the playlist → **Share → Copy link to playlist**.
2. The link looks like
   `https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=...`.
3. The ID is the part between `/playlist/` and `?` –
   `37i9dQZF1DXcBWIGoYBM5M`. Put it in `spotify_playlist_id`.

The playlist must be **owned by** or **collaborative for** the account you
authorize. The add-on only needs to *read* it.

## 4. Authorize (one time)

Run this on any computer with a browser (see also `docs/SETUP.md`):

```bash
pip install -r scripts/requirements-auth.txt
python scripts/authorize.py spotify \
    --spotify-client-id  <CLIENT_ID> \
    --spotify-client-secret <CLIENT_SECRET> \
    --out ./tokens
```

A browser window opens → **Agree**. The helper writes
`tokens/spotify_token.json` containing the refresh token. Copy that file into
the add-on's config folder (see `docs/SETUP.md`).

### Scopes requested

`playlist-read-private`, `playlist-read-collaborative` – read only. The add-on
has no write scopes and no code that modifies a Spotify playlist.

## Notes / troubleshooting

- **`INVALID_CLIENT: Invalid redirect URI`** – the redirect URI in the dashboard
  must be character-for-character identical to the one the helper uses
  (`http://127.0.0.1:8723/`).
- **Token rotation** – Spotify sometimes returns a new refresh token when the
  access token is refreshed. The add-on detects this and rewrites
  `/data/spotify_token.json` automatically.
- **`invalid_grant` on refresh** – the refresh token was revoked (password
  change, app removed from your account, or Premium lapsed). Re-run the helper
  and replace the token file.
- **Playlist changed to private / deleted** – the add-on logs
  `PlaylistUnavailableError` and makes **no** changes to YouTube until it can
  read the source again.
