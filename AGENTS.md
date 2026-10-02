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

- `CONFIG_FIELD`, `enabled()`: the DAI toggle. `SURROUND_FIELD`, `CONFIG_FIELDS`, `strip_surround()`: the Surround sound toggle; off removes the AC-3/E-AC-3 renditions from the bridge's master playlist so the stick plays DirecTV's stereo track.
- `pick_stream_url()`: chooses `streamURL` when DAI is on, appends `yospace.pool=livepause`, and merges the DAI query with exact-key dedup.
- `cached_url_usable()`: a cached URL is reused only under the same toggle setting and, with DAI on, only for the same playback device (its `_fw_did` is in the URL).
- `_CLIENT_PARAMS`: the Android TV app's fixed flags (`d=android_tv`, Nielsen/comScore Android TV values, app constants, Yospace flags).
- `request_flags()` / `build_query()`: those flags plus the account's own values (DMA, GPP, `bZipCode`) and the requesting device's ad id and `is_lat`.
- `_client_device()` / `_read_device()`: match the request IP to a bridge device and read its Android ID and Fire OS `limit_ad_tracking` over adb, cached an hour.
- `_account_zip()`: the billing ZIP for accounts that logged in before `dai_zip` existed.
- `gpp_targeted_ad_opt_out()`: decodes the US-National GPP section (id 7).
- `login_fields()`, `login_fields_from_cookies()`, `store_login_result()`, `fetch_account_context()`, `ids_from_bearer_jwt()`: login-time capture of DMA, ZIP, consent and household/profile ids.
- `note_channel()`: each channel's `daiChannelName`.
- `clear_cache_if_toggled()`: drops cached stream URLs when the toggle changes.

Upstream's files only get one-line hooks, and `scripts/validate.sh` checks every one is present:

- `app/scrapers/directv.py`:
  - `from . import directv_dai`.
  - `_fetch_channel_playback()`: the `dai`/`dai_extra` parameters, `pick_stream_url()`, and `'dai': dai` on the cache entry.
  - Both login paths call `login_fields*()`; `run_directv_auth()` calls `store_login_result()`.
  - `CONFIG_FIELD` in `config_schema`.
  - The scrape collects `dai_channel_names` and caches it.
  - `resolve()` uses `cached_url_usable()` and passes `request_flags()`; the license path passes `dai=`.
- `app/routes/api_sources.py`: calls `clear_cache_if_toggled()` after saving a source's config.
- `app/routes/directv_proxy.py`: imports `directv_dai`, runs the bridge master through `directv_dai.strip_surround()` (the Surround sound toggle), and has `'yospace.com'` in `_DIRECTV_BROWSER_CDN_SUFFIXES`, so DAI playlists and segments go through the same `browser-asset` relay as non-DAI streams.
- `app/templates/admin/sources.html`: the toggle in `renderDirectvConfig`.

When resolving a conflict in upstream's files, the fix is almost always to put the hook back where upstream's new code needs it.

**Why it exists:** to be ethical, so the right people get their fair share. The patch sends DirecTV and its advertisers the accurate information their own apps send, so local ads are delivered to the right market and counted, and the subscriber's privacy choice is honored. It is not a way to skip ads, fake measurement or impersonate other clients. See `CLAUDE.md` ("Why this exists").

Invariants a port must keep:

- **Never invent values.** Only the account's own values and the playback device's own ids go in the query. Never mint random ids or make up an advertising id; anything missing is omitted.
- **`d=android_tv` must be sent.** Without a device name the ad server recognizes, Yospace inserts no ads at all.
- **Never send the desktop identity** (`d=desktop`, `plt,DSK`, `devgrp,DSK`, comScore `PC`/`b`): it pulls web ad inventory with wrong-market, band-limited ads.
- **`is_lat` and `_fw_did` come from the playback device** (bridge device matched by request IP): `is_lat=0` with `_fw_did=android_id:<Android ID>`, or the opted-out form (`is_lat=1`, `_fw_did=google_advertising_id:optout`, `adid=optout`) when Fire OS reports `limit_ad_tracking=1`. `is_lat=0` is what gets local ads. Only when the requester isn't a bridge device is `is_lat` derived from GPP, and **only when `gpp_sid` is `7`**.
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
