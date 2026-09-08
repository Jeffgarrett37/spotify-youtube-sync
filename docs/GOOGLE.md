# Google / YouTube setup

You need: a Google account that **owns the destination YouTube playlist**, a
Google Cloud project with the YouTube Data API enabled, an OAuth **Desktop app**
client, and a one-time authorization.

## 1. Create / pick a Cloud project

1. <https://console.cloud.google.com/> → project picker → **New project**
   (name it e.g. "ha-playlist-sync"). Select it.

## 2. Enable the YouTube Data API v3

1. **APIs & Services → Library**.
2. Search **YouTube Data API v3** → **Enable**.

## 3. Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen** (in the current console this is
   under **Google Auth Platform → Branding / Audience**).
2. **User type / Audience**: **External**.
3. Fill the required fields (app name, your email for support + developer
   contact). You can skip logo/domains.
4. **Scopes**: you don't have to add any here; the add-on requests
   `https://www.googleapis.com/auth/youtube.force-ssl` at authorization time.
5. **Test users**: add your own Google address.
6. **IMPORTANT – publish the app.** On the **Audience** (or *OAuth consent
   screen*) page, set **Publishing status → In production** ("Publish app").
   You do **not** need to complete Google verification for personal use – you'll
   just see an *"unverified app"* warning during authorization that you can
   click through (**Advanced → Go to … (unsafe)**).

   If you leave the app in **Testing**, Google **expires the refresh token after
   7 days** and the add-on will stop working until you re-authorize. "In
   production" makes the refresh token effectively permanent (it still dies if
   unused for 6 months or if you revoke access – the add-on uses it every 6 h).

## 4. Create the OAuth client

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. **Application type: Desktop app**. Name it anything.
3. **Create**. Download the JSON (**client_secret_XXXX.json**) – you'll pass it
   to `scripts/authorize.py`. The **Client ID** and **Client secret** shown here
   also go into the add-on options `youtube_client_id` / `youtube_client_secret`.

A Desktop-app client allows the loopback redirect
`http://127.0.0.1:<port>/` that the authorization helper uses; you do not need
to register a redirect URI manually.

## 5. Find the destination playlist ID

1. On YouTube, open (or create) the playlist → its URL contains `list=PL...`:
   `https://www.youtube.com/playlist?list=PLabc123...` → the ID is `PLabc123...`.
2. It must be **owned by the Google account you authorize**. A brand-new empty
   playlist is fine – the add-on will populate it.
3. Put it in `youtube_playlist_id`.

## 6. Authorize (one time)

```bash
pip install -r scripts/requirements-auth.txt
python scripts/authorize.py google \
    --google-client-secrets ~/Downloads/client_secret_XXXX.json \
    --out ./tokens
```

Browser opens → pick the account that owns the playlist → click through the
unverified-app warning → **Allow**. The helper writes
`tokens/youtube_token.json` (contains the refresh token + client id/secret).
Copy it into the add-on config folder (see `docs/SETUP.md`).

## Quota – what to expect

Default quota is **10 000 units/day per project**, resetting at midnight US
Pacific. Costs the add-on incurs:

| Operation | Units | When |
|---|---|---|
| `search.list` | **100** | once (occasionally twice) per *new* track only |
| `videos.list` | 1 | hydrating candidates / verifying cached videos (batched 50) |
| `playlistItems.list` | 1 | once or twice per sync |
| `playlistItems.insert` | 50 | per track added |
| `playlistItems.delete` | 50 | per track removed |
| `playlistItems.update` | 50 | per item moved (only if `manage_order`) |

So a *new* match costs ~151 units and the add-on can process ~**60 new tracks
per day** on the default quota. Steady-state syncs (nothing changed) cost only a
few units. `daily_quota_budget` (default 9000) leaves headroom; raise it to
~9900 if this add-on is the only quota consumer, or request more quota from
Google via **IAM & Admin → Quotas** / the YouTube API compliance audit form.

## Troubleshooting

- **`accessNotConfigured` / API not enabled** – step 2 didn't take; wait a few
  minutes after enabling.
- **`Token has been expired or revoked`** – app still in *Testing* (7-day
  expiry), or you revoked access at
  <https://myaccount.google.com/permissions>. Publish the app and re-run the
  helper.
- **`quotaExceeded`** – you hit the daily cap. The add-on stops cleanly and
  resumes after the Pacific-midnight reset. Nothing is corrupted.
- **`playlistItemsNotAccessible` / 404 on the playlist** – the authorized
  account doesn't own `youtube_playlist_id`.
- **No refresh token returned** – you'd previously authorized; remove the app at
  <https://myaccount.google.com/permissions> and run the helper again (it forces
  `prompt=consent`).
