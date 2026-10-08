# AGENTS.md

Background for any AI agent working in this repo, including the AI step CI runs when a patch conflicts (Aider with an OpenRouter model).

## What this repo is

A patch overlay, not a fork. It builds [kineticman/FastChannels](https://github.com/kineticman/FastChannels) (branch `main`, where his releases come from; manual `development` builds (`upstream_branch=development` + `publish`) were published as `:latest` on 2026-10-08, and `main` builds skip quietly until his `main` has `app/scrapers/directv_device_auth.py`) plus the patches in `patches/`, and publishes the result to `ghcr.io/mackid1993/fastchannels-mackid1993`. Upstream's code never lives here; CI clones it fresh for every build.

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
source/             readable copies of the overlay's own modules (generated; patches/ is authoritative)
archive/            frozen old versions, never applied (the pre-2026-10-08 Android TV client patch)
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

- **`upstream-watch.yml`** polls kineticman's `development` branch every 6 hours. On a new commit (deduped via the Actions cache) it dispatches `build.yml` in drift-check mode against development (`upstream_branch=development`, `drift_only=true`). Because `upstream_branch` is set, that run is a test run: it never builds an image, publishes, refreshes the patch, or merges. If the patch drifts there, the AI adapts it and `open-pr` opens a **held** PR (branch `auto/drift-*`). That's the agent's prepared fix, left open and worded as FYI, not "merge me".
- **`reconcile-drift.yml`** polls every 6 hours. For each open `auto/drift-*` PR it re-tests that PR's patch against the *current* upstream `main`. The moment a fix is proven valid for production, it merges the PR and dispatches the publishing build, which re-runs the full gauntlet, so a wrong fix can't reach `:latest`. It merges at most one per run and shares the `build` concurrency group so it can't race a publish.

**Wait gate (2026-10-08):** `build.yml`'s skip step skips any upstream commit that lacks `app/scrapers/directv_device_auth.py` (his code sign-in, development-only for now), with a notice, so `main` builds wait for his release instead of failing into AI repair. Delete the gate once `main` has the file.

## The DAI patch

It adds an opt-in DirecTV source setting, **Use DirecTV ad insertion (DAI)** (`use_dai`). Off is upstream's behavior. When on, playback uses the stream DirecTV's own apps use: the Yospace ad-insertion session (`streamURL`) instead of `fallbackStreamUrl`, with the ad flags DirecTV's own **Android TV app** sends (`d=android_tv` and friends; see `CLAUDE.md` for where every value comes from), each bridge device's real advertising id, and the inserted-ad audio fix.

Sign-in, refresh and DRM recovery are **upstream's** (`app/scrapers/directv_device_auth.py`, kineticman's port of the old overlay client; merged 2026-10-08). The overlay carries none of it. The old client is in `archive/`.

### Where the hooks live

**Upstream's code gets exactly one line**, at the end of `create_app` in `app/__init__.py`:

```python
    from .scrapers import directv_dai_install; directv_dai_install.install(app)  # mackid1993 overlay: DirecTV DAI
    return app
```

**Every other hook is in our own `app/scrapers/directv_dai_install.py`.** It wires the feature in at runtime by wrapping upstream functions *by name* after they're defined, never by editing their bodies, so upstream can rewrite those functions freely without a patch conflict. Rules every wrapper follows:
- DAI off: upstream's function runs untouched.
- Any error in our code: log it, run upstream's function. Drift degrades to "DAI off", never to broken playback.
- Module-level wraps happen once per process (`create_app` runs several times), and per-app wraps (views, blueprint, `after_request`) once per app. A double wrap would cut ad loudness twice.

`TARGETS` (top of `directv_dai_install.py`) lists every upstream name it touches. `validate.sh` checks each one statically before any image is built, and fails with the exact name. `smoke_test.py` checks each is wrapped exactly once, then drives it.

| Upstream name (TARGETS) | What the wrap does |
|---|---|
| `directv.DirectvScraper.config_schema` | Appends the `use_dai` toggle (`directv_dai.CONFIG_FIELDS`), once. |
| `directv.DirectvScraper.resolve` | Drops a cached URL from the other DAI setting (or, with DAI on, another device; `directv_dai.cached_url_usable`), then runs upstream's resolve with the scraper as the tune context (a `ContextVar`). |
| `directv.DirectvScraper.prepare_license_request` (classmethod) | Same tune context for the license path's fallback fetch. |
| `directv._fetch_channel_playback` | With DAI flags for this tune (`directv_dai.request_flags`), makes the Android TV app's `channel/v2` request (`_fetch_dai_playback`: app query, the device's app UA, no Origin/Referer) and returns upstream's dict shape plus `'dai': True`. Raises upstream's `DirectvAuthExpiredError` on `0015` so upstream's re-auth runs. Any other failure falls back to upstream's v1 request. Arguments are bound by name with `inspect.signature`. |
| `directv._license_content_id_from_stream_url`, `directv.DirectvAuthExpiredError` | Used by the DAI fetch. |
| `directv.DirectvScraper._fetch_allchannels_rows` | Records each row's `daiChannelName` (the `net` flag) via `directv_dai.note_channels`. |
| `directv.apply_auth_result` | After upstream writes the session, `directv_dai.store_login_result` adds the DAI account values (DMA/ZIP/GPP fetched with the bearer for a `device_code` session; hhid/u). |
| `directv_device_auth._result` | Carries `valuePairs.partnerProfileId`/`profileId` into the result (upstream keeps only the activation token). |
| `directv_device_auth.is_device_session` | Also accepts the old overlay's `auth_method: 'dtv_android'` session, so it refreshes through upstream with no new sign-in. |
| `directv_proxy._DIRECTV_BROWSER_CDN_SUFFIXES` | Adds `yospace.com`, so Yospace playlists take the relay. |
| `directv_proxy._requests` | Swapped for `_RelayRequests`, which is identical except that a GET to a DirecTV relay host from a bridge device carries that device's DirecTV app User-Agent (Yospace master fetch + every playlist/segment). Everything else delegates to `requests`. |
| `directv_proxy._rewrite_directv_browser_playlist` | Runs `dtv_aac_ads.swap_muffled_ads` on the text first (inserted AC-3 ad → its HE-AAC twin). |
| `directv_proxy._directv_browser_proxyable_url` (+ `_directv_browser_asset_proxy_url`) | An inserted-ad creative (`dtv_aac_gain.is_ad_segment`, path-anchored) takes the relay on any CDN host. |
| `directv_proxy.directv_browser_asset` (the view, found in `app.view_functions`) | A full (non-Range) inserted-ad AAC segment is fetched here and cut with `dtv_aac_gain.attenuate_ad_segment` (−12 dB, lossless). Every other asset goes to upstream's view. |
| `SAVE_CONFIG_RULE` = `POST /api/sources/<int:source_id>/config` (found by URL rule) | After upstream saves the DirecTV settings: `directv_dai.clear_cache_if_toggled`, which drops cached URLs on a toggle flip and captures uncaptured devices' ad ids. |
| `TEMPLATE_MARKERS` in `sources.html` (`function renderDirectvConfig`, `class="config-actions"`) | Our blueprint serves `/directv-dai/admin.js` plus the `directv-profile` and `directv-capture-adids` routes. An `after_request` adds the script tag to the page containing `function renderDirectvConfig`. The script wraps that global function to add the **Run as DirecTV profile** picker (when `cfg.directv.code_signed_in`), the DAI toggle and the "Capture advertising IDs" button, all before `config-actions`. |

If upstream renames a target, change the reference in `directv_dai_install.py` (and `TARGETS`). **Never edit an upstream file to bring the old name back.**

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
- **`directv_dai_install.py`**: the wiring above.
- **`dtv_aac_ads.py`** and **`dtv_aac_gain.py`**: the inserted-ad audio fix, stdlib only. `swap_muffled_ads`, `is_ad_segment`, `attenuate_ad_segment` (returns the bytes unchanged on any parse mismatch).

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

The patch is our own modules (`directv_dai.py`, `directv_dai_device.py`, `directv_dai_install.py`, `dtv_aac_ads.py`, `dtv_aac_gain.py`), which we own outright, plus **one line** in upstream's `app/__init__.py`. So:

- **A conflict** (`git am` fails) can only be that one line. Keep upstream's current `create_app` and put the line back just before it returns `app`.
- **Drift** (the patch applies but `validate.sh`/`smoke_test.py` fails) means upstream renamed or moved a name in `TARGETS`, the save-config URL rule, or a template marker. The failure names it. Find the new name in `upstream_index.txt`, update the reference in `directv_dai_install.py` (and `TARGETS`), and keep the wrapper's behavior. If the target's shape changed (new arguments, a different return value), adapt the wrapper to it. The wrappers bind arguments by name, so a new parameter alone doesn't break them.
- **Never add code to an upstream file**, and never restore an old upstream name there. Upstream's files are upstream's, apart from the one line.
- **The AI runs unattended.** It never asks a question. When a capability is genuinely gone it says so in one sentence and makes no edit.
- **Backport, don't fabricate.** If a capability a wrapper needs is gone but rebuildable from real upstream data, rebuild it in one of our modules from that real data. Never invent data, and never write a hollow stub.
- Touch only what the failure requires. Keep the Python valid. Verify with `python3 -m compileall -q app`, then `scripts/validate.sh`, then `scripts/smoke_test.py` (CI runs it inside the built image).
- In CI, only the patch's files are editable; editing any other file fails the run.

## Changing the patch by hand

Clone upstream, run `scripts/apply-patches.sh <checkout>`, commit your change (or amend) in the checkout, then run `scripts/refresh-patches.sh <checkout> <upstream-sha>` and commit `patches/` here.
