# AGENTS.md

Background for any AI agent working in this repo, including the AI step CI runs when a patch conflicts (Aider with Claude Haiku 5.5 on Anthropic's API).

## What this repo is

A patch overlay, not a fork. It builds [kineticman/FastChannels](https://github.com/kineticman/FastChannels) (branch `main`, where his releases come from; manual `development` builds (`upstream_branch=development` + `publish`) were published as `:latest` on 2026-10-08, and `main` builds skip quietly until his `main` has `app/scrapers/directv_device_auth.py`) plus the patches in `patches/`, and publishes the result to `ghcr.io/mackid1993/fastchannels-mackid1993`. Upstream's code never lives here; CI clones it fresh for every build.

```
patches/            the changes applied on top of upstream, as `git format-patch` files
scripts/
  apply-patches.sh    git am --3way each patch onto an upstream checkout, then insert_hook.py
  insert_hook.py      inserts the one install() line into upstream's create_app (never a patch hunk)
  validate.sh         static checks on the patched tree
  ruff_diff.py        fail only on ruff findings upstream doesn't already have
  smoke_test.py       exercises the DAI code inside the built image (incl. behavioral relay tests)
  boot_test.sh        starts the image and waits for the web UI
  reproduce.sh        re-runs the gauntlet to classify a build failure (drift vs infra) for the AI
  ai_fix_drift.sh     the shared AI repair: reproduce -> ask the AI -> verify scope -> re-check
  refresh-patches.sh  regenerates patches/ and source/ from commits on top of an upstream commit
source/             readable copies of the overlay's own modules (generated; patches/ is authoritative)
archive/            frozen old versions, never applied (the pre-2026-10-08 Android TV client patch)
.github/workflows/build.yml             the release build (upstream main) + AI repair
.github/workflows/test-development.yml  test only: the full gauntlet on upstream development
```

## Build lifecycle

Every 6 hours, on a manual run, or on a push to `patches/`, `scripts/` or the build workflow, the build job runs these steps — building and publishing only when there's a real change:

1. Read upstream `main`'s newest commit and the newest Player APK release. Skip if that exact combination with these patches was already built.
2. `git am --3way` the patches onto a fresh upstream checkout (they only add our files), then `insert_hook.py` adds the one line to `create_app`.
3. Static checks: compile, no new ruff error-class findings, upstream's files differ by the one hook line only, and every upstream name, string and route the overlay relies on is present.
4. Build the image (bundles the latest Player APK from kineticman's releases) and confirm the APK is inside.
5. Smoke test the DAI code inside the image; boot the container and wait for the web UI.
6. Publish `:latest`, `:upstream-<sha>`, `:build-<key>`.
7. Regenerate `patches/` against that upstream and commit it, so the patch context stays current.

Step 1's dedup (the build key is just upstream sha + Player APK + the patch's added/removed lines) means a scheduled run builds + publishes only when upstream `main`, the APK, or the patch actually changed; otherwise it's a no-op. A **push** to this repo runs a drift check instead (steps 2-3: apply + validate, no image, no publish), so a CI/patch/doc change reruns the tests without rebuilding `:latest`. Step 6 publishes the exact image steps 4-5 tested, and a successful publish opens-and-closes a status issue that @mentions you — a success ping. A "Build failed" issue @mentions you and is retried on the next scheduled run.

The AI fix (Aider, with the model in the `AI_MODEL` variable — set to `anthropic/claude-haiku-5-5`, using the `ANTHROPIC_API_KEY` secret; an `openrouter/<id>` model uses `OPENROUTER_API_KEY` instead) runs for two kinds of failure. It only ever edits files; CI does all git operations and runs the validation. Both paths converge on one shared repair script, **`scripts/ai_fix_drift.sh`**: it runs the whole gauntlet (`reproduce.sh`), hands the AI the failing log plus the files the patch touches (`git diff --name-only <sha> HEAD`), verifies the AI changed only those files and leaked no key, amends the fix into the patch commit, and re-checks everything — up to three attempts, each shown the latest failure. A fix must pass the static checks **and** a full image build, APK check, smoke test and boot test before the `open-pr` job merges it and starts the publishing build. A bad fix never reaches `main`.

- **Conflict** (step 2 fails; practically unreachable now that the patch only adds our own files and the hook line is inserted by `insert_hook.py`): the `resolve` job recreates the conflict and asks the AI to resolve it, editing only the conflicted files, then finishes the merge and runs `ai_fix_drift.sh`. Resolving the markers is often enough (the script reproduces clean and does nothing more), but if the *same* upstream change also drifted a file the patch adds or hooks that didn't conflict — e.g. upstream renamed a name one of the patch's own modules imports — the script repairs that too. So the conflict path self-heals instead of publishing a half-fixed patch that builds and then fails the smoke test.
- **Drift** (step 2 applies cleanly but a later check fails — upstream renamed or moved something a hook or an added module depends on): the `resolve-drift` job runs `ai_fix_drift.sh`, but **only on the unattended scheduled runs** (and test-run dispatches), never on a push or the post-merge dispatch. A clean reproduction (a transient hiccup, or a failure only in the publish/refresh steps) or a Docker/APK failure (infrastructure, not patch-fixable) stops without calling the AI and exports nothing. The overlay's own tests (`validate.sh`, `smoke_test.py`) are the spec and are not editable by the AI.

If either path can't produce a passing fix, `ai-failed` comments on the matching status issue.

## Watching upstream development (early fixes) + reconcile

Two more workflows keep the whole thing hands-off by fixing drift *before* it reaches a release:

- **`upstream-watch.yml`** polls kineticman's `development` branch every 6 hours. On a new commit (deduped via the Actions cache) it dispatches `build.yml` in drift-check mode against development (`upstream_branch=development`, `drift_only=true`). Because `upstream_branch` is set, that run is a test run: it never builds an image, publishes, refreshes the patch, or merges. If the patch drifts there, the AI adapts it and `open-pr` opens a **held** PR (branch `auto/drift-*`). That's the agent's prepared fix, left open and worded as FYI, not "merge me".
- **`test-development.yml`** runs the full gauntlet (static checks, image, APK, smoke, boot) against `development` on every push here, on each new development commit (dispatched by `upstream-watch.yml`), and by hand (any upstream ref). Test only: no publish, no commits, no AI. It's how a change here is proven on his newest code while `main` builds wait.
- **`reconcile-drift.yml`** polls every 6 hours. For each open `auto/drift-*` PR it re-tests that PR's patch against the *current* upstream `main`. The moment a fix is proven valid for production, it merges the PR and dispatches the publishing build, which re-runs the full gauntlet, so a wrong fix can't reach `:latest`. It merges at most one per run and shares the `build` concurrency group so it can't race a publish.

**Wait gate (2026-10-08):** `build.yml`'s skip step skips any upstream commit that lacks `app/scrapers/directv_device_auth.py` (his code sign-in, development-only for now), with a notice, so `main` builds wait for his release instead of failing into AI repair. Delete the gate once `main` has the file.

## The DAI patch

It adds an opt-in DirecTV source setting, **Use DirecTV ad insertion (DAI)** (`use_dai`). Off is upstream's behavior. When on, playback uses the stream DirecTV's own apps use: the Yospace ad-insertion session (`streamURL`) instead of `fallbackStreamUrl`, with the ad flags DirecTV's own **Android TV app** sends (`d=android_tv` and friends; see `CLAUDE.md` for where every value comes from), each bridge device's real advertising id, and the inserted-ad audio fix.

Sign-in, refresh and DRM recovery are **upstream's** (`app/scrapers/directv_device_auth.py`, kineticman's port of the old overlay client; merged 2026-10-08). The overlay carries none of it. The old client is in `archive/`.

### Where the hooks live

The design goal: upstream changes break the overlay as rarely as possible, and loudly when they do. So the overlay depends on as few of upstream's internals as it can, and on the most stable ones.

**Upstream's code gets exactly one line**, before `create_app` returns in `app/__init__.py`:

```python
    from .scrapers import directv_dai_install; directv_dai_install.install(app)  # mackid1993 overlay: DirecTV DAI
    return app
```

It is inserted by `scripts/insert_hook.py` (run by `apply-patches.sh`), which finds `create_app` with Python's parser. It is **not** a patch hunk, so `git am` can't conflict on it; `patches/` only adds our five modules. `validate.sh` fails if upstream's files differ by anything but that line.

**Everything else is in our own `app/scrapers/directv_dai_install.py`**, in three layers, most stable first:
1. **HTTP layer, keyed on the relay's URL prefix (`RELAY_PREFIX` = `/play/directv/`), not on function names.** An `after_request` swaps inserted AC-3 ads to their AAC twins in every playlist response there; a `before_request` serves inserted-ad AAC segments there with the loudness cut (hosts on upstream's DirecTV CDN allowlist only); the app User-Agent goes on at `requests.Session.request`.
2. **Our own UI and routes.** The DAI panel (toggle, profile picker, ad-id button) is our script and our routes; it mounts inside upstream's settings box for the DirecTV source (`id="source-config-<id>"`) and is also its own page at `/directv-dai`. `use_dai` lives in the source config, outside upstream's schema.
3. **Wrapped by name, only where unavoidable** (`TARGETS`).

Rules every hook follows:
- DAI off: upstream's behavior, untouched.
- Any error in our code: log it, run upstream's behavior. Drift degrades to "DAI off", never to broken playback.
- Never silent: a missing name (`missing()`), a target whose shape changed (`status()['failed']`, e.g. a renamed fetch argument), and at runtime a tune that should have been DAI but wasn't (`status()['runtime']`) are all reported, by CI and in red in the DAI panel.
- Module-level wraps happen once per process (`create_app` runs several times), per-app hooks once per app. A double wrap would cut ad loudness twice.

What the overlay relies on in upstream, all listed at the top of `directv_dai_install.py` and checked by `validate.sh`, `smoke_test.py` and `status()`:

| Upstream name | Kind | What it's for |
|---|---|---|
| `directv.DirectvScraper.resolve` | wrap | Offers the requesting box its own saved DAI session for the channel (`dai_playback_by_device`), else drops a cached URL from the other DAI setting or another box (that channel only, in memory); runs upstream's resolve with the scraper as the tune context (a `ContextVar`); records whether the tune really got DAI and saves a new session under the box. |
| `directv._fetch_channel_playback` | wrap | With DAI flags for this tune, makes the Android TV app's `channel/v2` request (`_fetch_dai_playback`) and returns upstream's dict shape plus `'dai': True`. Raises upstream's `DirectvAuthExpiredError` on `0015`; any other failure falls back to upstream's v1 request. Arguments bound by name (`bearer_token`, `cookies`, `client_context`, `ccid`); a missing one is reported. |
| `directv.DirectvScraper.prepare_license_request` (classmethod) | wrap | The same tune context for the license path's fallback fetch, and the requesting box's own DAI session (play token) when it has one. |
| `directv.DirectvScraper._fetch_allchannels_rows` | wrap | Records each row's `daiChannelName` (the `net` flag). |
| `directv.apply_auth_result` | wrap | After upstream writes the session, `store_login_result` adds the DAI account values (DMA/ZIP/GPP fetched with the bearer, DAI on only; hhid/u). |
| `directv_device_auth._result` | wrap | Carries `valuePairs.partnerProfileId`/`profileId` into the result (upstream keeps only the activation token). |
| `directv_proxy._DIRECTV_BROWSER_CDN_SUFFIXES` | extended | Adds `yospace.com`, so Yospace playlists take the relay. |
| `USES`: `_license_content_id_from_stream_url`, `DirectvAuthExpiredError`, `_PLAYBACK_CACHE_TTL` (the per-box session lifetime), `DirectvScraper._token_stale`/`can_reauth`/`_start_background_reauth` (re-queue a refresh the profile switch deferred), `directv_device_auth.AUTH_METHOD`/`app_headers`/`APP_USER_AGENT`/`_CLIENT_ID`/`is_device_session`, `_directv_browser_cdn_allowed`, `BaseScraper.cache`/`_update_cache`, `bridge_devices.known_devices`, `config_store.persist_source_cache_updates`/`persist_source_config_updates`/`load_source_cache_by_name`, `models.Source`, `extensions.db` | called/read | Named so a rename fails with its name. |
| `RELAY_PREFIX` `/play/directv/` | URL prefix | The HTTP-layer hooks (ad swap, loudness cut, User-Agent). |
| `SOURCE_MARKERS`: `id="source-config-{{ source.id }}"` in `sources.html`; the refresh-lock key `directv:auth:refreshing:` in `directv.py` | strings | Where the panel mounts (it is also at `/directv-dai`); the profile swap waits on upstream's refresh lock. |

Our routes (our blueprint): `/directv-dai` (the panel page), `/directv-dai/admin.js`, `/directv-dai/status`, `/directv-dai/sources`, `/api/sources/<id>/directv-dai` (read/save the toggle), `/api/sources/<id>/directv-profile`, `/api/sources/<id>/directv-capture-adids`. A session the old overlay signed in (`auth_method: 'dtv_android'`) is re-tagged once to upstream's `AUTH_METHOD` (`migrate_old_sessions`).

`TARGETS` and `USES` are dicts keyed by a fixed **role** (`'channel_fetch'`, `'source_model'`, ...) whose value is upstream's `(module, 'name' or 'Class.name')`. The overlay's modules **and `smoke_test.py`** reach upstream only through the role (`install.up(role)`, `up_name`, `up_owner`, `up_set`), never by name, so if upstream renames or moves something, **the fix is that role's value, one line**, and the tests follow it (rehearsed: a rename of `_fetch_channel_playback` plus that one-line change passes `validate.sh` and `smoke_test.py`). **Never edit an upstream file, never rename or drop a role to make a check pass** (the smoke test requires every role).

**A role must point at the thing that does the job, not just at a name that exists.** `smoke_test.py` checks the roles the overlay only reads by what they do in upstream, so picking a look-alike fails with the role named:
- `playback_cache_ttl` must be the age limit upstream's own `resolve()` applies to its cached `directv_playback` (younger is served from cache, older is fetched again).
- `code_signin_client_id` must be exactly what upstream's code sign-in sends DirecTV as `clientId`/`clientID`, and a `UNIFIED_` id.
- `token_stale`, `can_reauth` and `start_reauth` must be the stale check, re-auth gate and refresh queueing that upstream's own `pre_run_setup` calls, in that order.
- `load_cache` must read one cache key by source name (`keys=[...]`); six concurrent per-box session saves must all survive.

When one of these fails, find the upstream object that does that job (read the code that uses it, not just the names in `upstream_index.txt`) and point the role there.

**`CONTRACT` (also in `directv_dai_install.py`) is the tests' view of upstream:** the routes, source name and stream-URL format `smoke_test.py` drives (each route is checked against the running app), plus the allowed differences between our DAI request and upstream's own channel request. If upstream moves a route or changes a format, the fix is that `CONTRACT` value, like a role. The parity test (`smoke_test.py` (v)) fails when upstream's channel request sends a parameter or header ours doesn't, or treats a different response as an expired token. **Fairness rule for a parity failure:** a new upstream parameter or header goes on `web_only_params` / `web_only_headers` (not sent by us) unless the Android TV app is known to send it. If in doubt, leave it out. Never copy a web value into the Android TV request to make the test pass. A new expired-token signal is mirrored in `_fetch_dai_playback`.

### Our modules

- **`directv_dai.py`**: the DAI feature.
  - `CONFIG_FIELD(S)`, `enabled()`.
  - `pick_stream_url()` takes the v1 or v2 shape and does the Yospace pool and exact-key flag merge. `cached_url_usable()`.
  - `request_flags()`/`build_query()` build `_CLIENT_PARAMS` plus the account's values and the device flags.
  - `device_ad_flags(props, ad_id)`.
  - Advertising-id capture: `capture_registered_devices`, `capture_in_background`, `uncaptured_addresses`, `_capture_and_store`, `_reachable`, `_read_fire_advertising_id`, `_read_gms_advertising_id`, `_is_playing`, and the `directv_dai_adids.json` store with its thread lock plus `flock`.
  - Viewer profiles, which set the Yospace `profid` and are required for ad targeting: `list_profiles`, `select_profile`, `profile_token_exchange`, `_partner_profile_id1`, `profiles_supported`. The exchange is single-flight behind upstream's `directv:auth:refreshing:<name>` lock.
  - `gpp_targeted_ad_opt_out()`, `store_login_result()`, `fetch_account_context()`, `ids_from_bearer_jwt()`, `note_channels()`, `clear_cache_if_toggled()`.
- **`directv_dai_device.py`**: the bridge device behind the request.
  - `_client_device()` matches the request IP to `bridge_devices.known_devices()` and reads Android ID, `limit_ad_tracking`, release, model, board and manufacturer over adb. They're persisted in `directv_dai_devices.json`, re-checked about hourly, and only a real change updates them.
  - `player_user_agent()`/`player_headers()` give the app UA, `APP_PROJECT_NAME/5.0.136.2002113867 (Android <release>; <model>; <board>)  PureRN/0.79.5`.
- **`directv_dai_install.py`**: the wiring above, the runtime health notes (`_note`, `health()`, `runtime_warnings()`), and the DAI panel (`_ADMIN_JS`, `_PAGE_HTML`).
- **`dtv_aac_ads.py`** and **`dtv_aac_gain.py`**: the inserted-ad audio fix, stdlib only. `swap_muffled_ads`, `is_ad_segment`, `attenuate_ad_segment` (returns the bytes unchanged on any parse mismatch).

**Why it exists:** to be ethical, so the right people get their fair share. The patch sends DirecTV and its advertisers the accurate information their own apps send, so local ads are delivered to the right market and counted, and the subscriber's privacy choice is honored. It is not a way to skip ads, fake measurement or impersonate other clients. See `CLAUDE.md` ("Why this exists").

Invariants a port must keep:

- **Never invent values.** Only the account's own values and the playback device's own ids go in the query. Never mint random ids or make up an advertising id; anything missing is omitted.
- **`d=android_tv` must be sent.** Without a device name the ad server recognizes, Yospace inserts no ads at all.
- **Never send the desktop identity** (`d=desktop`, `plt,DSK`, `devgrp,DSK`, comScore `PC`/`b`): it pulls web ad inventory with wrong-market, band-limited ads.
- **`is_lat`, `_fw_did` and `adid` come from the playback device** (bridge device matched by request IP); `comscore_device` names the device upstream's fixed app User-Agent presents (`Android_Google_Chromecast`, via `directv_dai_device.presented_device()`), so the session shows one client; built as in `device_ad_flags()`: the device's **real advertising id**, captured once per device (`google_advertising_id:<id>` + `adid=<id>`, `is_lat=0`); a deleted/limited id or `limit_ad_tracking=1` gives the opted-out form (`is_lat=1`, `_fw_did=google_advertising_id:optout`, `adid=optout`); `android_id:<Android ID>` + `is_lat=0` **only** as the last resort when no advertising id could be read. `is_lat=0` is what gets local ads. Only when the requester isn't a bridge device is `is_lat` derived from GPP, and **only when `gpp_sid` is `7`**.
- Yospace URLs need `yospace.pool=livepause`; without it Yospace answers 503.
- DAI params are merged with exact-key dedup: a key already in the URL is never duplicated or overridden.
- Changing the toggle must clear the cached playback URLs.
- Never log token values or account values. Field names only.
- Nothing in a tune or relay fetch waits on the network beyond the one channel authorization: the bridge-device list is cached (single-flight, stale-while-refresh). The one exception: a tune with no stored `dai_zip` looks the billing ZIP up before it goes out (bounded, once per sign-in), because every DAI session must carry `bZipCode`.
- Config writes go through `persist_config` (merged under upstream's per-source lock), never a commit of a config copied earlier.

## Resolving a conflict (and repairing drift)

The patch only adds our own modules (`directv_dai.py`, `directv_dai_device.py`, `directv_dai_install.py`, `dtv_aac_ads.py`, `dtv_aac_gain.py`), and the one line in `create_app` is inserted by `scripts/insert_hook.py`, not carried as a hunk. So:

- **A conflict** (`git am` fails) can only mean upstream added a file with one of our modules' names. The `resolve` job re-inserts the hook line after the merge, as `apply-patches.sh` does. If `insert_hook.py` fails (`apply-patches.sh` exits 3, not reported as a conflict), upstream's app factory changed shape: update `insert_hook.py` (a human change), never `app/__init__.py`; `ai_fix_drift.sh` stops early on it.
- **Drift** (the patch applies but `validate.sh`/`smoke_test.py` fails) means upstream renamed, moved or reshaped something in `TARGETS`, `USES`, `SOURCE_MARKERS` or `RELAY_PREFIX`. The failure names it. Find the new name in `upstream_index.txt`, change that role's value in `TARGETS`/`USES`, and keep the behavior. If a wrapped target's shape changed (new arguments, a different return value), adapt the wrapper to it.
- **Never touch an upstream file.** CI inserts the hook line and `validate.sh` rejects any other change to upstream's files.
- **Never drop an entry or weaken a check** to get green; the smoke test counts the entries.
- **The AI runs unattended.** It never asks a question. When what the overlay needs is truly gone from upstream, it says so in one sentence and makes no edit.
- **Never fabricate**: no invented values, no hollow stubs, no copies of removed upstream code.
- Touch only what the failure requires. Keep the Python valid. Verify with `python3 -m compileall -q app`, then `scripts/validate.sh`, then `scripts/smoke_test.py` (CI runs it inside the built image).
- In CI, only our five modules are editable; editing any other file fails the run.

## Changing the patch by hand

Clone upstream, run `scripts/apply-patches.sh <checkout>`, commit your change (or amend) in the checkout, then run `scripts/refresh-patches.sh <checkout> <upstream-sha>` and commit `patches/` and `source/` here (it leaves `app/__init__.py` out; the hook line is always inserted by `insert_hook.py`). Commits are authored as `mackid1993 <david@brustein.net>`, with no Claude/AI attribution trailers (see `CLAUDE.md`).
