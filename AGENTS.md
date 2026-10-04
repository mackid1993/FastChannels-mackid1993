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
  smoke_test.py       exercises the DAI code inside the built image (incl. behavioral relay tests)
  boot_test.sh        starts the image and waits for the web UI
  reproduce.sh        re-runs the gauntlet to classify a build failure (drift vs infra) for the AI
  ai_fix_drift.sh     the shared AI repair: reproduce -> ask the AI -> verify scope -> re-check
  refresh-patches.sh  regenerates patches/ and source/ from commits on top of an upstream commit
.github/workflows/build.yml
```

## Build lifecycle

Every 6 hours, on a manual run, or on a push to `patches/`, `scripts/` or the build workflow, the build job runs these steps — building and publishing only when there's a real change:

1. Read upstream `main`'s newest commit and the newest Player APK release. Skip if that exact combination with these patches was already built.
2. `git am --3way` the patches onto a fresh upstream checkout.
3. Static checks: compile, no new ruff error-class findings, templates parse, the DAI module and every hook are present.
4. Build the image (bundles the latest Player APK from kineticman's releases) and confirm the APK is inside.
5. Smoke test the DAI code inside the image; boot the container and wait for the web UI.
6. Publish `:latest`, `:upstream-<sha>`, `:build-<key>`.
7. Regenerate `patches/` against that upstream and commit it, so the patch context stays current.

Step 1's dedup (the build key is just upstream sha + Player APK + the patch's added/removed lines) means a scheduled run builds + publishes only when upstream `main`, the APK, or the patch actually changed; otherwise it's a no-op. A **push** to this repo runs a drift check instead (steps 2-3: apply + validate, no image, no publish), so a CI/patch/doc change reruns the tests without rebuilding `:latest`. Step 6 publishes the exact image steps 4-5 tested, and a successful publish opens-and-closes a status issue that @mentions you — a success ping. A "Build failed" issue @mentions you and is retried on the next scheduled run.

The AI fix (Aider, with the model in the `AI_MODEL` variable — set to `openrouter/z-ai/glm-5.3-flash`) runs for two kinds of failure. It only ever edits files; CI does all git operations and runs the validation. Both paths converge on one shared repair script, **`scripts/ai_fix_drift.sh`**: it runs the whole gauntlet (`reproduce.sh`), hands the AI the failing log plus the files the patch touches (`git diff --name-only <sha> HEAD`), verifies the AI changed only those files and leaked no key, amends the fix into the patch commit, and re-checks everything — up to three attempts, each shown the latest failure. A fix must pass the static checks **and** a full image build, APK check, smoke test and boot test before the `open-pr` job merges it and starts the publishing build. A bad fix never reaches `main`.

- **Conflict** (step 2 fails): the `resolve` job recreates the conflict and asks the AI to resolve it, editing only the conflicted files, then finishes the merge and runs `ai_fix_drift.sh`. Resolving the markers is often enough (the script reproduces clean and does nothing more), but if the *same* upstream change also drifted a file the patch adds or hooks that didn't conflict — e.g. upstream renamed a name one of the patch's own modules imports — the script repairs that too. So the conflict path self-heals instead of publishing a half-fixed patch that builds and then fails the smoke test.
- **Drift** (step 2 applies cleanly but a later check fails — upstream renamed or moved something a hook or an added module depends on): the `resolve-drift` job runs `ai_fix_drift.sh`, but **only on the unattended scheduled runs** (and test-run dispatches), never on a push or the post-merge dispatch. A clean reproduction (a transient hiccup, or a failure only in the publish/refresh steps) or a Docker/APK failure (infrastructure, not patch-fixable) stops without calling the AI and exports nothing. The overlay's own tests (`validate.sh`, `smoke_test.py`) are the spec and are not editable by the AI.

If either path can't produce a passing fix, `ai-failed` comments on the matching status issue.

## Watching upstream development (early fixes) + reconcile

Two more workflows keep the whole thing hands-off by fixing drift *before* it reaches a release:

- **`upstream-watch.yml`** polls kineticman's `development` branch every 6 hours. On a new commit (deduped via the Actions cache) it dispatches `build.yml` in drift-check mode against development (`upstream_branch=development`, `drift_only=true`). Because `upstream_branch` is set that run is a test run: it never builds an image, publishes, refreshes the patch, or merges. If the patch drifts there, the AI adapts it and `open-pr` opens a **held** PR (branch `auto/drift-*`) — the agent's prepared fix, left open (worded as FYI, not "merge me").
- **`reconcile-drift.yml`** polls every 6 hours. For each open `auto/drift-*` PR it re-tests that PR's patch against the *current* upstream `main`; the moment a fix is proven valid for production it merges the PR and dispatches the publishing build (which re-runs the full gauntlet, so a wrong fix can't reach `:latest`). It merges at most one per run and shares the `build` concurrency group so it can't race a publish.

So upstream drift is fixed and queued as a held PR before a release lands, then merged and published automatically once it's proven against `main` — you only ever get FYI notifications.

## The DAI patch

It adds an opt-in DirecTV source setting, **Use DirecTV ad insertion (DAI)** (`use_dai`). Off is upstream's behavior. When on, playback uses the stream DirecTV's own apps use: the Yospace ad-insertion session (`streamURL`) instead of `fallbackStreamUrl`, with the ad flags DirecTV's own **Android TV app** sends (`d=android_tv` and friends; see `CLAUDE.md` for where every value comes from).

Its logic lives in files upstream doesn't have, so they can't conflict. The **core file is `app/scrapers/dtv_android.py`** — the Android TV client the DirecTV source now runs on end to end. It is self-contained (it imports nothing from the other overlay modules) and works without DAI; everything else layers on top of it and depends on it, never the reverse. `directv_dai.py` is the DAI (ad-insertion) feature; `dtv_aac_ads.py` and `dtv_aac_gain.py` are the inserted-ad audio fix (standalone relay helpers, no-op unless DAI is on).

**`dtv_android.py`** — the Android TV client (the core; the login flow, device identity, app User-Agent, channel/v2, DRM self-heal and profile picker all live here):

- `sign_in(source_id, …)`: the `run_directv_auth` entry — refresh a stored Android TV session (reusing its saved device id), else run the one-time device-code grant; accepts a refresh only when a DRM activation token came back or is still stored.
- `capture_android_auth()`, `start_device_code()`, `poll_tokens()`: the one-time device-code grant — start it, surface the code + directv.com/tvsigninv2 link in the admin status, poll the tokens endpoint until the user approves in a browser.
- `refresh_session(refresh_token, device_class_id)`: `POST authn-refreshgo/v3/refresh?clientID=…` with `reqParams=ACTIVATIONTOKEN` — rotates the bearer + refresh token and **re-mints the DRM activation token**; keeps the saved device id (one registered device).
- `refresh_in_place(scraper)`: refresh now and write the new tokens onto the scraper's config so an in-flight tune can retry (resolve()'s expired-token path).
- `token_stale()`, `background_refresh_due()`, `token_expires_at()`: staleness + proactive-refresh cadence — no timer refresh; stale only with no bearer or past a known expiry; proactive refresh on the app's ~55-min cadence.
- `drm_reauth(source, reason)`: self-healing DRM-403 recovery — single-flight refresh (shared redis lock) that re-mints the activation token and drops the cached identity cookie; returns `False` for a non-Android-TV session so upstream's web recovery still runs.
- `license_headers(config)`: the DRM activate/license headers (app User-Agent, no `stream.directv.com` Origin/Referer).
- `android_auth_request(session, params, dai, default_url)`: with DAI on, turns the channel authorization into the Android TV app's request (`channel/v2`; no browser `Origin`/`Referer`; the device's app User-Agent; `startOver=false`; the web-only `timeShiftEnabled`/`dualManifest` dropped) and returns the URL. DAI off: returns `default_url` (upstream's v1) untouched.
- `_client_device()`, `_bridge_address()`, `_read_device()`, `_recheck_device()`, `_DEVICE_PROPS`, `_store_device()`/`_saved_devices()`/`_save_devices()`/`_devices_file()`: match the request IP to a bridge device (`bridge_devices.known_devices()`) and read, in one adb call, its stable identity — Android ID, `limit_ad_tracking`, Android version, model, board, manufacturer — persisted in `directv_dai_devices.json` (next to the SQLite DB on `/data`, keyed by adb address) so a device keeps one identity even while asleep. New devices are read + saved at once; known ones re-checked in a background thread ~hourly, saved values changing only on a real change (factory reset, ad-tracking toggle, firmware), logged; a failed re-check changes nothing; a failed first read is retried after a minute.
- `player_headers()`, `player_user_agent()`, `_APP_USER_AGENT`: the app's player User-Agent from the device's properties (`APP_PROJECT_NAME/5.0.136.2002113867 (Android <release>; <model>; <board>)  PureRN/0.79.5`); `{}`/`None` for non-bridge requests.
- `list_profiles(bearer)`, `select_profile(source, profile_id, …)`, `profile_token_exchange()`, `_partner_profile_id1()`: the viewer-profile picker — list the account's profiles and run the session as one, storing its id as the Yospace `profid` (`dtv_android_profid`).
- Constants: `_CLIENT_ID = 'UNIFIED_Android_TV_02'`; the grant / refresh / profile endpoint URLs.

**`directv_dai.py`** — the DAI (ad-insertion) feature; `from . import dtv_android` (depends on it, never the reverse):

- `CONFIG_FIELD`, `CONFIG_FIELDS`, `enabled()`: the DAI toggle (`use_dai`, default off).
- `_v2_playback()`, `pick_stream_url(pb, dai)`: channel/v2 returns `playbackData.streamUrls` groups (`DAI`, `Data Center`); these map to v1's `streamURL`/`fallbackStreamUrl` (first URL of each). `pick_stream_url` takes either shape; `dai` is the flag dict from `request_flags()` or `None`. With flags it plays the Yospace URL, appends `yospace.pool=livepause`, and merges the flags (exact-key dedup; `:` and `,` left literal).
- `cached_url_usable()`: a cached URL is reused only under the same toggle setting and, with DAI on, only for the same device (its `_fw_did` is in the URL).
- `_CLIENT_PARAMS`: the Android TV app's fixed flags (`d=android_tv`, Nielsen/comScore Android TV values, app constants, Yospace live params).
- `request_flags(config, channel_names, ccid)` / `build_query()`: `_CLIENT_PARAMS` plus the account's values (household/profile ids, DMA, GPP, `bZipCode`), the device flags, and `net`. Returns `None` with DAI off.
- `device_ad_flags(props)`: the device's ad flags, identical logic on every device (Fire TV, Google TV, Shield): `comscore_device=Android_<manufacturer>_<model>` (whitespace removed, as the Android TV app builds it), `_fw_did=android_id:<Android ID>` + `is_lat=0`; limited tracking: `google_advertising_id:optout` + `adid=optout` + `is_lat=1`. No device's platform advertising id is read (a Google TV/Shield only exposes it through Play services, so none use it).
- `_account_zip()`: the billing ZIP for accounts that logged in before `dai_zip` existed.
- `gpp_targeted_ad_opt_out()`: decodes the US-National GPP section (id 7); used for `is_lat` only when the request isn't from a bridge device.
- `login_fields()`, `login_fields_from_cookies()`, `store_login_result()`, `fetch_account_context()`, `ids_from_bearer_jwt()`: login-time capture of DMA, ZIP, consent and household/profile ids.
- `note_channel(scraper, row, ccid)`: stores each lineup row's `daiChannelName` (the `net` flag) in `scraper.cache['dai_channel_names']` via `_update_cache`, only when it changes.
- `clear_cache_if_toggled()`: drops cached stream URLs when the toggle changes.

**`dtv_aac_ads.py`** and **`dtv_aac_gain.py`** — the inserted-ad audio fix: standalone relay helpers (they import only the stdlib and are hooked in `directv_proxy.py`), **no-op unless DAI is on** — the inserted AAC ad tracks only exist under DAI; without it the stream is AC-3 and nothing matches:

- `dtv_aac_ads.swap_muffled_ads(playlist)`: rewrite each inserted AC-3 ad on a DAI audio playlist (every segment **and** its `#EXT-X-MAP`) to its full-range HE-AAC twin (`-a-96-`), matched by the Yospace creative name; a safe no-op on live `dfwlive-*` content and any non-matching playlist.
- `dtv_aac_gain.is_ad_segment(url)`: `True` for an inserted-ad AAC twin (`u-<id>-a-<rate>-…mp4`), matched on the URL path only (so it can't be tricked into proxying an arbitrary host); used both to route the creative through the relay and to gate the cut.
- `dtv_aac_gain.attenuate_ad_segment(data)`: pull each inserted AAC ad down `_DEFAULT_STEPS`×1.5 dB (currently −12 dB) losslessly by editing the HE-AAC `global_gain` of every frame; returns the bytes unchanged on any parse mismatch, so it can never emit a corrupt segment.

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
| curl_cffi login result dict | `**directv_dai.login_fields(session, bearer, token_data),` | DMA, consent, household/profile ids. | Add to the dict that path returns. |
| Playwright login | `captured.update(directv_dai.login_fields_from_cookies(captured))` | Same for that path. | After `captured` has the bearer and cookies. |
| `run_directv_auth()`, before commit | `directv_dai.store_login_result(cfg, result)` | Saves those values to the config (incl. the Android TV device id + any token expiry). | Next to upstream's other `cfg[...] = result[...]` lines. |
| `app/scrapers/directv.py`, imports | `from . import dtv_android` | — | Keep with the other relative imports. |
| `run_directv_auth()`, the capture call | `result = dtv_android.sign_in(source_id, username, password, app=app, on_status=_on_status)` | Sign in / refresh as DirecTV's Android TV app (device-code grant) instead of the web client. | Replace upstream's `capture_directv_auth_fast(...)` call. |
| `license_request_headers()` | `return dtv_android.license_headers(config)` | App User-Agent, no `stream.directv.com` Origin/Referer, so the DRM device is Android TV not a browser. | Replace the returned headers dict. |
| `_token_stale()` | `return dtv_android.token_stale(self.config)` | Stale (→ must authenticate) only when there's no bearer, or past a known expiry. | Replace the time-based staleness check. |
| `pre_run_setup()` | `if dtv_android.background_refresh_due(self.config): self._start_background_reauth()` | Keep the DRM session warm like the app's ~55-min refresh (re-minting the activation token via the refresh) so a tune never meets a dead token; fires on the scrape cadence, lock-guarded, proceeds on the current token. Still raises `ScrapeSkipError` only when `_token_stale()` (no bearer). | After the not-authenticated skip. |
| `resolve()`, the expired-token `except` | `dtv_android.refresh_in_place(self)` then retry `_fetch_channel_playback(...)` once | Refresh inline and retry so a tune recovers instead of failing on an expired token. | Wrap the `DirectvAuthExpiredError` handler; if it returns False, fall through to `_mark_auth_stale_and_reauth` (clears the cache + logs; no stored creds to auto-reauth with). |
| `_directv_trigger_reauth()` (directv_proxy.py), DRM 403 recovery | `if dtv_android.drm_reauth(source, reason): return` at the top | Android TV session self-heal: a single-flight refresh (shares the background-reauth lock) that re-mints the DRM activation token (`reqParams=ACTIVATIONTOKEN`), then drops the cached identity cookie so the next tune re-activates with the fresh token. If the refresh can't mint a token, it clears the dead one and logs "Log out → Authenticate". Returns False for non-ATV so that path still runs. | First line of the function, before the web-client token-wipe / background-reauth. |
| `app/source_config.py`, `is_source_config_complete()` | `if source_name == 'directv': return bool(saved.get('bearer_token') or saved.get('refresh_token'))` | "Configured" = a captured session (there are no username/password fields; the device-code grant is approved in a browser). | Add a `directv` branch, like Philo/ESPN/FOX One. |
| `app/routes/api_sources.py`, `directv_logout()` + `directv_auto_login()` | `POST /sources/<id>/directv-logout` clears the session keys + the `directv_playback` cache; `directv_auto_login` passes no credentials | The Log out button and the credential-less Authenticate. | Keep the route + the `_DIRECTV_SESSION_KEYS` list; don't re-add a creds requirement to auto-login. |
| `app/routes/api_sources.py`, `directv_profile()` | `GET/POST /sources/<id>/directv-profile` → `dtv_android.list_profiles` (list the account's viewer profiles) / `dtv_android.select_profile` (run the session as one; sets the Yospace `profid`) | The viewer-profile picker. | Keep the route; it reads the account bearer and calls the two `dtv_android` functions. |
| `app/routes/api_sources.py`, `save_source_config()` | `directv_dai.clear_cache_if_toggled(source, old, current)` | Flushes cached URLs on toggle change. | After the config is committed. |
| `app/routes/directv_proxy.py`, imports | `from ..scrapers import dtv_aac_ads, dtv_aac_gain, dtv_android, registry` | — | directv_dai is no longer imported here (the relay uses `dtv_android` for the UA + DRM re-auth, `dtv_aac_ads` for the ad swap, and `dtv_aac_gain` for the ad loudness cut). |
| `_DIRECTV_BROWSER_CDN_SUFFIXES` | `'yospace.com',` | Yospace playlists go through the same relay. | Keep in the relay allowlist. |
| `_directv_browser_proxyable_url()` + `directv_browser_asset()` CDN check | `... or dtv_aac_gain.is_ad_segment(url)` added to both the playlist-rewrite routing decision and the `browser-asset` host allowlist | Route a Yospace inserted-ad creative through the relay (so its loudness cut always runs) no matter which CDN host it lands on, not only when the host is on `_DIRECTV_BROWSER_CDN_SUFFIXES`. `is_ad_segment` matches on the URL *path* only, anchored to the `u-<id>-a-<rate>-…mp4` filename, so it can't be tricked into proxying an arbitrary host. | Add the `or dtv_aac_gain.is_ad_segment(...)` clause back to both the routing decision and the `browser-asset` host check. |
| `directv_browser_manifest()`, master fetch | `headers=dtv_android.player_headers()` | The app's User-Agent on the request that opens the Yospace session (`player_headers` moved to the portable `dtv_android`). | On upstream's `requests.get` of the resolved URL. |
| `directv_browser_asset()`, relay headers | `{'User-Agent': _BROWSER_UA, **dtv_android.player_headers()}` | The app's User-Agent on every playlist and segment. | Merge into whatever headers dict the relay sends. |
| `directv_browser_asset()`, the `.m3u8` branch | `_rewrite_directv_browser_playlist(dtv_aac_ads.swap_muffled_ads(r.text), r.url)` | Swap each inserted AC-3 ad for its full-range AAC twin (HE-AAC 96k — the muffle fix). | Wrap the playlist text before `_rewrite_directv_browser_playlist(...)`. |
| `directv_browser_asset()`, inserted-ad AAC segment | `if dtv_aac_gain.is_ad_segment(raw_url) and not range_header: body = dtv_aac_gain.attenuate_ad_segment(r.content); return Response(body, ...)` (plus `dtv_aac_gain` on the `from ..scrapers import ...` line) | Pull each inserted AAC ad's loudness down ~12 dB losslessly (8 × 1.5 dB `global_gain` steps, `_DEFAULT_STEPS`; safe no-op on anything else) so a break matches the programming. The swap's byte-level sibling. | Right after the `.m3u8` branch, before the segment streaming return. Guard to full (non-range) fetches so a partial request is never half-attenuated. |
| `app/templates/admin/sources.html`, `renderDirectvConfig` | `toggleHtml('use_dai', ...)` | The DAI setting in the UI. | Next to the other DirecTV toggles. |
| `app/templates/admin/sources.html`, `_directvCheckStatus` | `_directvLinkify(detail)` on the `running` status | Makes the tvsigninv2 sign-in URL in the auth status a clickable link. | Wrap the status `detail` before it's put in `innerHTML`. |
| `app/templates/admin/sources.html`, `renderDirectvConfig` + `directvLogout()` | no username/password fields; an Authenticate/Re-authenticate button and, when a session exists, a **Log out** button → `directvLogout()` → `/directv-logout` | Credential-less sign-in UI. | Keep the Log out button gated on `hasSession` and the `directvLogout` handler. |
| `app/templates/admin/sources.html`, the profile picker | a **Run as DirecTV profile** `<select>` + button calling `GET`/`POST /directv-profile` | Pick which DirecTV viewer profile FastChannels runs as. | Keep the select + handler, gated on a captured session. |

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

## Resolving a conflict (and repairing drift)

The patch is two things: **our own modules** (`app/scrapers/directv_dai.py`, `dtv_android.py`, `dtv_aac_ads.py`, `dtv_aac_gain.py`), which we own outright, and one-line **hooks** injected into upstream's files (`api_sources.py`, `directv_proxy.py`, `directv.py`, `source_config.py`, `templates/admin/sources.html`). A hook is a single line that wires our code in — `from . import dtv_android`, a `dai=directv_dai.request_flags(...)` argument, a `dtv_android.sign_in(...)` call, the `dai: dict | None = None` parameter we add to an upstream function. Every other line in those upstream files — including every function name — is upstream's. The job is to keep the hooks wired as upstream moves.

- The result must be upstream's current code plus exactly the patch's behavior.
- **The AI runs unattended.** It is given `upstream_index.txt` (upstream's current files and every def/class); it uses that to find where code moved, and it never asks a question or waits for input — when a capability is genuinely gone it *states* that in one sentence (a statement, not a question) rather than guessing. A fix can be a large backport onto a diverged upstream: re-wire every hook to the current structure.
- Keep every upstream change. **A hook follows upstream:** if upstream renamed or moved a name a hook calls or sits in, adapt the hook's reference (anywhere in the files being edited, not only inside a conflict block) to the new name or location; don't restore the old code.
- **Keep a test-pinned name reachable.** The overlay's own tests (`validate.sh`, `smoke_test.py`) are the spec and can't be edited; some reference an upstream name a hook depends on. If upstream renamed or MOVED it, point the old name at the REAL relocated code: a module-level alias next to a renamed function in a file you edit (`old_name = new_name`, the real object under both names, so `inspect.signature`/`inspect.getsource` resolve through it); or, for a moved module the patch doesn't own, `sys.modules['app.<oldname>'] = <real new module>` registered from one of our own modules (imported before the test uses it). A bare alias only — no wrapper, no new logic — keeping one name reachable, not restoring old code.
- **Backport a removed capability from its real source; never fabricate data.** A capability a hook or test needs may be gone as a named function/module yet still be rebuildable from real upstream data that's still present (e.g. the `known_devices` registry is gone, but the configured device address it listed is still there). Then **backport it**: re-implement the capability in one of *our own* modules reading that REAL upstream source, and expose it under the pinned name (a module-level alias, or a `sys.modules['app.<old>']` runtime module carrying your real function). That is a genuine port to upstream's current shape, not fabrication, and it's the correct fix — the ported code must read real data so the feature actually works. Forbidden is only inventing/hardcoding data (made-up `address`/`host`) or a hollow stub that returns nothing. Make no edit (and say what's gone in one sentence) only when no real data source exists anywhere to rebuild from. Don't add other non-hook code to an upstream file: no stubs, no `try/except` swallowing an import, no deleted or no-op'd hooks. Our own modules are free to change as the fix needs.
- Touch only what the failure requires. No refactoring, reformatting or unrelated fixes. Keep the Python valid.
- Verify with `python3 -m compileall -q app` in the checkout. CI then runs `scripts/validate.sh`, and after the merge, `scripts/smoke_test.py` inside the built image.
- In CI, only edit the patch's files (the four modules and the hooked upstream files listed above); editing any other file fails the run. The workflow does the git operations.

## Changing the patch by hand

Clone upstream, run `scripts/apply-patches.sh <checkout>`, commit your change (or amend) in the checkout, then run `scripts/refresh-patches.sh <checkout> <upstream-sha>` and commit `patches/` here.
