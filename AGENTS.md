# AGENTS.md

Background for any AI agent working in this repo, including the Gemini step CI runs when a patch conflicts.

## What this repo is

A patch overlay, not a fork. It builds [kineticman/FastChannels](https://github.com/kineticman/FastChannels) (branch `development`) plus the patches in `patches/`, and publishes the result to `ghcr.io/mackid1993/fastchannels-mackid1993`. Upstream's code never lives here; CI clones it fresh for every build.

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

1. Read upstream `development`'s newest commit and the newest Player APK release. Skip if that exact combination with these patches was already built.
2. `git am --3way` the patches onto a fresh upstream checkout.
3. Static checks: compile, no new ruff error-class findings, templates parse, the DAI code is present.
4. Build the image (bundles the latest Player APK from kineticman's releases) and confirm the APK is inside.
5. Smoke test the DAI code inside the image; boot the container and wait for the web UI.
6. Publish `:latest`, `:upstream-<sha>`, `:build-<key>`.
7. Regenerate `patches/` against that upstream and commit it, so the patch context stays current.

If step 2 conflicts, the `resolve` job asks Gemini to resolve the conflict. If the result passes the static checks, the `open-pr` job merges it and starts a normal build, which must pass every check before anything is published.

## The DAI patch

It adds an opt-in DirecTV source setting, **Use DirecTV ad insertion (DAI)** (`use_dai`). Off is upstream's behavior. When on, playback uses the stream DirecTV's own apps use: the Yospace ad-insertion session (`streamURL`) instead of `fallbackStreamUrl`, with the ad flags the DirecTV desktop web client sends.

Files it touches:

- `app/scrapers/directv.py`: nearly everything.
  - `_DAI_PLATFORM_PARAMS`: the web client's platform constants.
  - `_gpp_targeted_ad_opt_out()`: decodes the US-National GPP section (id 7).
  - `_fetch_dai_account_context()`: the account's DMA and GPP consent string, fetched at login.
  - `_ids_from_bearer_jwt()`: backfills household/profile ids from the bearer token's claims.
  - `_ensure_dai_device_ids()`: mints this device's own ad/device ids once.
  - The `use_dai` `ConfigField`, `uses_dai()` and `_build_dai_query()`.
  - In `_fetch_channel_playback()`: choosing `streamURL`, appending `yospace.pool=livepause`, merging the DAI query with exact-key dedup, and storing `dai` on the cache entry.
  - In both auth paths (curl_cffi and Playwright): persisting the `dai_*` values.
  - In the scrape: capturing each channel's `daiChannelName` into the `dai_channel_names` cache.
  - In `resolve()`: using a cached URL only if its `dai` flag matches the setting.
- `app/templates/admin/sources.html`: the toggle in `renderDirectvConfig`.
- `app/routes/api_sources.py`: clears the `directv_playback` cache when `use_dai` changes.

Invariants a port must keep:

- **Never invent account values.** Only values sourced from the account, or minted for our own device, go in the query. Anything missing is omitted.
- `is_lat` is derived from the GPP string **only when `gpp_sid` is `7`**: `1` if the account opted out of targeted advertising, otherwise `0`. Other sections pass `gpp` through untouched, without `is_lat`.
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
