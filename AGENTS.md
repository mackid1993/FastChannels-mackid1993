# AGENTS.md

Background for any AI agent working in this repo, including the AI step CI runs when a patch conflicts (Aider with an OpenRouter model).

## What this repo is

A patch overlay, not a fork. It builds [kineticman/FastChannels](https://github.com/kineticman/FastChannels) (branch `main`, where his releases come from) plus the patches in `patches/`, and publishes the result to `ghcr.io/mackid1993/fastchannels-mackid1993`. Upstream's code never lives here; CI clones it fresh for every build.

```
patches/            the changes applied on top of upstream, as `git format-patch` files
scripts/
  apply-patches.sh    git am --3way each patch onto an upstream checkout
  validate.sh         static checks on the patched tree
  ruff_diff.py        fail only on ruff findings upstream doesn't already have
  smoke_test.py       exercises the DAI code inside the built image
  boot_test.sh        starts the image and waits for the web UI
  refresh-patches.sh  regenerates patches/ from commits on top of an upstream commit
.github/workflows/build.yml
```

## Build lifecycle

Weekly (Mondays, 9 AM Eastern), on a manual run, or when `patches/`, `scripts/` or the workflow change:

1. Read upstream `main`'s newest commit and the newest Player APK release. Skip if that exact combination with these patches was already built.
2. `git am --3way` the patches onto a fresh upstream checkout.
3. Static checks: compile, no new ruff error-class findings, templates parse, the DAI module and every hook are present.
4. Build the image (bundles the latest Player APK from kineticman's releases) and confirm the APK is inside.
5. Smoke test the DAI code inside the image; boot the container and wait for the web UI.
6. Publish `:latest`, `:upstream-<sha>`, `:build-<key>`.
7. Regenerate `patches/` against that upstream and commit it, so the patch context stays current.

The other six days at 9 AM Eastern, a drift check runs steps 1-3 and 7 without building an image, so a conflict is caught and fixed the day upstream introduces it. While a "Build failed" issue is open, the drift check does a full build instead, so failures are retried daily. Step 6 pushes the exact image steps 4-5 tested.

If step 2 conflicts, the `resolve` job asks an AI (Aider, with the model in the `AI_MODEL` variable) to resolve the conflict. The result must pass the static checks and a full image build, APK check, smoke test and boot test; only then does the `open-pr` job merge it and start the publishing build. If it fails, `ai-failed` comments on the conflict issue.

## The DAI patch

It adds an opt-in DirecTV source setting, **Use DirecTV ad insertion (DAI)** (`use_dai`). Off is upstream's behavior. When on, playback uses the stream DirecTV's own apps use: the Yospace ad-insertion session (`streamURL`) instead of `fallbackStreamUrl`, with the ad flags DirecTV's own **Android TV app** sends (`d=android_tv` and friends; see `CLAUDE.md` for where every value comes from).

Nearly all of it lives in **`app/scrapers/directv_dai.py`**, a file upstream doesn't have, so it can't conflict:

- `CONFIG_FIELD`, `CONFIG_FIELDS`, `enabled()`: the DAI toggle (`use_dai`, default off).
- `android_auth_request(session, params, dai, default_url)`: with DAI on, turns the channel authorization into the Android TV app's request and returns the URL to call: `channel/v2`; no browser `Origin`/`Referer`; the device's app User-Agent; the app's query (`startOver=false`; the web-only `timeShiftEnabled` and `dualManifest` dropped). DAI off: returns `default_url` (upstream's v1) and touches nothing.
- `_v2_playback()`, `pick_stream_url(pb, dai)`: channel/v2 returns `playbackData.streamUrls` groups (`DAI`, `Data Center`); these map to v1's `streamURL`/`fallbackStreamUrl` (first URL of each). `pick_stream_url` takes either shape; `dai` is the flag dict from `request_flags()` or `None`. With flags it plays the Yospace URL, appends `yospace.pool=livepause`, and merges the flags (exact-key dedup; `:` and `,` left literal).
- `cached_url_usable()`: a cached URL is reused only under the same toggle setting and, with DAI on, only for the same device (its `_fw_did` is in the URL).
- `_CLIENT_PARAMS`: the Android TV app's fixed flags (`d=android_tv`, Nielsen/comScore Android TV values, app constants, Yospace live params).
- `request_flags(config, channel_names, ccid)` / `build_query()`: `_CLIENT_PARAMS` plus the account's values (household/profile ids, DMA, GPP, `bZipCode`), the device flags, and `net`. Returns `None` with DAI off.
- `_client_device()`, `_bridge_address()`, `_read_device()`, `_DEVICE_PROPS`: match the request IP to a bridge device (`bridge_devices.known_devices()`) and read, in one adb call: Android ID, `limit_ad_tracking`, Android version, model, board, manufacturer. Every session uses the device's saved values from `directv_dai_devices.json` (next to the SQLite database on the `/data` volume, keyed by adb address), so a device keeps one stable identity even while asleep. A new device is read right away and saved. Known devices are re-checked in a background thread about once an hour (`_recheck_device`); the saved values change only if the device really changed (factory reset = new Android ID, ad tracking toggled, firmware), and the change is logged. A failed re-check changes nothing. A new device whose first read fails is retried after a minute (warning logged). `_store_device()`, `_devices_file()`, `_saved_devices()`, `_save_devices()` handle the file (atomic replace).
- `device_ad_flags(props)`: the device's ad flags, identical logic on every device (Fire TV, Google TV, Shield): `comscore_device=Android_<manufacturer>_<model>` (whitespace removed, as the Android TV app builds it), `_fw_did=android_id:<Android ID>` + `is_lat=0`; limited tracking: `google_advertising_id:optout` + `adid=optout` + `is_lat=1`. No device's platform advertising id is read (a Google TV/Shield only exposes it through Play services, so none use it).
- `player_headers()`, `player_user_agent()`, `_APP_USER_AGENT`: the app's player User-Agent from the device's properties: `APP_PROJECT_NAME/5.0.136.2002113867 (Android <release>; <model>; <board>)  PureRN/0.79.5`. `{}`/`None` for non-bridge requests.
- `_account_zip()`: the billing ZIP for accounts that logged in before `dai_zip` existed.
- `gpp_targeted_ad_opt_out()`: decodes the US-National GPP section (id 7); used for `is_lat` only when the request isn't from a bridge device.
- `login_fields()`, `login_fields_from_cookies()`, `store_login_result()`, `fetch_account_context()`, `ids_from_bearer_jwt()`: login-time capture of DMA, ZIP, consent and household/profile ids.
- `note_channel(scraper, row, ccid)`: stores each lineup row's `daiChannelName` (the `net` flag) in `scraper.cache['dai_channel_names']` via `_update_cache`, only when it changes.
- `clear_cache_if_toggled()`: drops cached stream URLs when the toggle changes.

Upstream's files get one-line hooks only. `scripts/validate.sh` checks each is present, and `scripts/smoke_test.py` checks the upstream APIs they call. Every hook, what it's for, and how to re-apply it if upstream moves the code:

| File / place | Hook | Purpose | If upstream changes it |
|---|---|---|---|
| `app/scrapers/directv.py`, imports | `from . import directv_dai` | — | Keep with the other relative imports. |
| `_fetch_channel_playback()` signature | `dai: dict \| None = None` | Receives the DAI flags (`None` = off). | Add the keyword to whatever function now calls the channel authorization API. |
| `_fetch_channel_playback()`, the `session.get(...)` | `dtv_android.android_auth_request(session, params, dai, _CHANNEL_AUTH_URL)` as the URL | Android TV app request on channel/v2 when DAI is on (part of the portable Android client in `dtv_android`). | Wrap upstream's authorization URL in this call after its `params`/headers are built. |
| `_fetch_channel_playback()`, the stream URL | `fallback_url = directv_dai.pick_stream_url(pb, dai)` | Picks the Yospace URL and adds the flags. | Replace upstream's `fallbackStreamUrl`/`streamURL` pick with this. |
| `_fetch_channel_playback()`, the returned dict | `'dai': bool(dai),` | Lets `cached_url_usable()` tell DAI from non-DAI cache entries. | Add to the cached playback dict. |
| `resolve()`, the cache check | `directv_dai.cached_url_usable(cached, directv_dai.enabled(self.config))` | Don't reuse a URL from the other toggle state or another device. | Replace the bare `cached and` check. |
| `resolve()`, the `_fetch_channel_playback(...)` call | `dai=directv_dai.request_flags(self.config, self.cache.get('dai_channel_names'), ccid)` | Passes the flags. | Add the keyword. |
| License fallback, its `_fetch_channel_playback(...)` call | `dai=directv_dai.request_flags(config, None, channel_id)` | Same session type for the play token. | Add the keyword. |
| Lineup scrape, per row | `directv_dai.note_channel(self, row, ccid)` | Records `net`. | Call once per channel row after `ccid` is known. |
| `config_schema` | `*directv_dai.CONFIG_FIELDS,` | The DAI toggle. | Keep inside the DirecTV schema list. |
| `app/scrapers/directv.py`, imports + `config_schema` | `from . import dtv_stereo_downmix` + `*dtv_stereo_downmix.CONFIG_FIELDS,` | The opt-in stereo-downmix toggle (`stereo_downmix`, default off). | Keep the import with the others and the fields in the schema list. |
| curl_cffi login result dict | `**directv_dai.login_fields(session, bearer, token_data),` | DMA, consent, household/profile ids. | Add to the dict that path returns. |
| Playwright login | `captured.update(directv_dai.login_fields_from_cookies(captured))` | Same for that path. | After `captured` has the bearer and cookies. |
| `run_directv_auth()`, before commit | `directv_dai.store_login_result(cfg, result)` | Saves those values to the config (incl. the Android TV device id + any token expiry). | Next to upstream's other `cfg[...] = result[...]` lines. |
| `app/scrapers/directv.py`, imports | `from . import dtv_android` | — | Keep with the other relative imports. |
| `run_directv_auth()`, the capture call | `result = dtv_android.sign_in(source_id, username, password, app=app, on_status=_on_status)` | Sign in / refresh as DirecTV's Android TV app (device-code grant) instead of the web client. | Replace upstream's `capture_directv_auth_fast(...)` call. |
| `license_request_headers()` | `return dtv_android.license_headers(config)` | App User-Agent, no `stream.directv.com` Origin/Referer, so the DRM device is Android TV not a browser. | Replace the returned headers dict. |
| `_token_stale()` | `return dtv_android.token_stale(self.config)` | No fixed-timer refresh — stale only when there's no bearer (the grant token is long-lived). | Replace the time-based staleness check. |
| `resolve()`, the expired-token `except` | `dtv_android.refresh_in_place(self)` then retry `_fetch_channel_playback(...)` once | Refresh inline and retry so a tune recovers instead of failing on an expired token. | Wrap the `DirectvAuthExpiredError` handler; if it returns False, fall through to `_mark_auth_stale_and_reauth` (clears the cache + logs; no stored creds to auto-reauth with). |
| `app/source_config.py`, `is_source_config_complete()` | `if source_name == 'directv': return bool(saved.get('bearer_token') or saved.get('refresh_token'))` | "Configured" = a captured session (there are no username/password fields; the device-code grant is approved in a browser). | Add a `directv` branch, like Philo/ESPN/FOX One. |
| `app/routes/api_sources.py`, `directv_logout()` + `directv_auto_login()` | `POST /sources/<id>/directv-logout` clears the session keys + the `directv_playback` cache; `directv_auto_login` passes no credentials | The Log out button and the credential-less Authenticate. | Keep the route + the `_DIRECTV_SESSION_KEYS` list; don't re-add a creds requirement to auto-login. |
| `app/routes/api_sources.py`, `save_source_config()` | `directv_dai.clear_cache_if_toggled(source, old, current)` | Flushes cached URLs on toggle change. | After the config is committed. |
| `app/routes/directv_proxy.py`, imports | `from ..scrapers import dtv_aac_ads, dtv_android, dtv_stereo_downmix, registry` | — | directv_dai is no longer imported here (the relay uses `dtv_android` for the UA, `dtv_aac_ads` for the ad swap, `dtv_stereo_downmix` for the master). |
| `_DIRECTV_BROWSER_CDN_SUFFIXES` | `'yospace.com',` | Yospace playlists go through the same relay. | Keep in the relay allowlist. |
| `directv_browser_manifest()`, master fetch | `headers=dtv_android.player_headers()` | The app's User-Agent on the request that opens the Yospace session (`player_headers` moved to the portable `dtv_android`). | On upstream's `requests.get` of the resolved URL. |
| `directv_browser_manifest()`, master rewrite | `_rewrite_directv_browser_playlist(dtv_stereo_downmix.stereo_downmix_master(r.text, channel.source.config), r.url)` | Declare the AC-3 rendition stereo (CHANNELS="2") when the opt-in toggle is on — fuller/louder downmix. | Wrap the master text before `_rewrite_directv_browser_playlist(...)`. |
| `directv_browser_asset()`, relay headers | `{'User-Agent': _BROWSER_UA, **dtv_android.player_headers()}` | The app's User-Agent on every playlist and segment. | Merge into whatever headers dict the relay sends. |
| `directv_browser_asset()`, the `.m3u8` branch | `_rewrite_directv_browser_playlist(dtv_aac_ads.swap_muffled_ads(r.text), r.url)` | Swap each inserted AC-3 ad for its full-range HE-AAC twin (the muffle fix). | Wrap the playlist text before `_rewrite_directv_browser_playlist(...)`. |
| `app/templates/admin/sources.html`, `renderDirectvConfig` | `toggleHtml('use_dai', ...)` and `toggleHtml('stereo_downmix', ...)` | The DAI and stereo-downmix settings in the UI. | Next to the other DirecTV toggles. |
| `app/templates/admin/sources.html`, `_directvCheckStatus` | `_directvLinkify(detail)` on the `running` status | Makes the tvsigninv2 sign-in URL in the auth status a clickable link. | Wrap the status `detail` before it's put in `innerHTML`. |
| `app/templates/admin/sources.html`, `renderDirectvConfig` + `directvLogout()` | no username/password fields; an Authenticate/Re-authenticate button and, when a session exists, a **Log out** button → `directvLogout()` → `/directv-logout` | Credential-less sign-in UI. | Keep the Log out button gated on `hasSession` and the `directvLogout` handler. |

When resolving a conflict in upstream's files, the fix is almost always to put the hook back where upstream's new code needs it.

**Why it exists:** to be ethical, so the right people get their fair share. The patch sends DirecTV and its advertisers the accurate information their own apps send, so local ads are delivered to the right market and counted, and the subscriber's privacy choice is honored. It is not a way to skip ads, fake measurement or impersonate other clients. See `CLAUDE.md` ("Why this exists").

Invariants a port must keep:

- **Never invent values.** Only the account's own values and the playback device's own ids go in the query. Never mint random ids or make up an advertising id; anything missing is omitted.
- **`d=android_tv` must be sent.** Without a device name the ad server recognizes, Yospace inserts no ads at all.
- **Never send the desktop identity** (`d=desktop`, `plt,DSK`, `devgrp,DSK`, comScore `PC`/`b`): it pulls web ad inventory with wrong-market, band-limited ads.
- **`is_lat`, `_fw_did`, `adid` and `comscore_device` come from the playback device** (bridge device matched by request IP), built as in `device_ad_flags()`, the same on every device: `android_id:<Android ID>` + `is_lat=0`; limited tracking: the opted-out form (`is_lat=1`, `_fw_did=google_advertising_id:optout`, `adid=optout`) when Fire OS reports `limit_ad_tracking=1`. `is_lat=0` is what gets local ads. Only when the requester isn't a bridge device is `is_lat` derived from GPP, and **only when `gpp_sid` is `7`**.
- Yospace URLs need `yospace.pool=livepause`; without it Yospace answers 503.
- DAI params are merged with exact-key dedup: a key already in the URL is never duplicated or overridden.
- Changing the toggle must clear the cached playback URLs.
- Never log token values or account values. Field names only.

## Resolving a conflict

- The result must be upstream's current code plus exactly the patch's behavior.
- Keep every upstream change. If upstream renamed or restructured something the patch uses, adapt the patch's code to the new structure; don't restore the old code.
- Touch only what the conflict requires. No refactoring, reformatting or unrelated fixes.
- Verify with `python3 -m compileall -q app` in the checkout. CI then runs `scripts/validate.sh`, and after the merge, `scripts/smoke_test.py` inside the built image.
- In CI, only edit files. The workflow does the git operations.

## Changing the patch by hand

Clone upstream, run `scripts/apply-patches.sh <checkout>`, commit your change (or amend) in the checkout, then run `scripts/refresh-patches.sh <checkout> <upstream-sha>` and commit `patches/` here.
