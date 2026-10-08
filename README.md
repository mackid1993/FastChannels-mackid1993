# FastChannels-mackid1993

[kineticman/FastChannels](https://github.com/kineticman/FastChannels) (`main`, the branch his releases come from) with a DirecTV addition, kept up to date automatically:

- **DirecTV ad insertion (DAI):** an opt-in toggle in the DirecTV source settings. When on, playback uses the Yospace ad-insertion stream DirecTV's own apps use, with the account's own targeting values (DMA, billing ZIP, consent, household and profile IDs) and each bridge device's real advertising ID, ad-tracking setting and comScore device name — the same values DirecTV's Android TV app sends. That's how it gets local and political ads for the account's market. Requests look like the app's: its `channel/v2` authorization request and its player User-Agent, built from each device's own model, board and Android version. It needs the AH4C bridge (an Android device) — it does nothing on Prismcast. Each device's real advertising ID is captured once and reused: when you turn DAI on (and via a "Capture advertising IDs" button for devices added later), a Fire TV is read silently and a Google TV/Android TV device briefly shows its Ads settings screen — a device that's currently playing is skipped so a stream is never interrupted. The device's Android ID is the fallback when no advertising ID can be read, and a deleted or limited ID sends the opt-out form.

- **Full-range inserted-ad audio:** many of DirecTV's inserted ads are muffled ("AM radio") in their Dolby (AC-3) encode while the same ad's AAC encode is full range. With DAI on, the relay swaps each inserted ad to its AAC version so breaks sound clear. Swapped ads play in stereo; live programming is untouched. (The muffle is in DirecTV's own files — upstream and the Osprey have it too — so this is the only lever.)

- **Inserted ads matched to programming loudness:** DirecTV's inserted ads come in several dB louder than the show. With DAI on, the relay pulls each inserted ad down to the programming level — lowering its AAC gain in place, losslessly (no re-encoding) — so breaks don't blast. It only touches inserted ads, never live programming, and leaves an ad untouched if it can't read it cleanly.

- **Sign-in is upstream's.** DirecTV's Android TV code sign-in (approve a code once at directv.com/tvsigninv2; no password stored; refreshes on its own) started in this overlay and is now part of upstream as "Sign in with a code". The overlay no longer carries its own copy. A session signed in by the old overlay is still recognized and refreshes through upstream's code, so there's nothing to redo. The old overlay's full Android TV client, including the viewer-profile picker, is archived in `archive/android-tv-client-before-upstream-merge/`.

```
docker pull ghcr.io/mackid1993/fastchannels-mackid1993:latest
```

Use it anywhere you'd use upstream's image; the data volume and settings are the same.

## How it stays current

This repo doesn't hold a copy of FastChannels. It holds the patch in `patches/` and workflows that poll upstream every ~6 hours (GitHub can't be notified of pushes to a repo you don't own) and keep a tested image published on top of his latest release — hands-off, with the AI doing any fixing. When his `main` (releases) has a new commit or a new Player APK release, a build runs; you can also run it by hand from the Actions tab:

1. **Check for changes.** It reads upstream `main`'s newest commit and the newest FastChannels Player release. If neither they nor the patch changed since the last build, it stops without rebuilding.
2. **Apply the patch** to a fresh upstream checkout with `git am --3way`. A 3-way merge means upstream can move, add or edit code around our changes and the patch still lands. It fails only if upstream rewrites the same lines the patch changes.
3. **Static checks:** every Python file compiles; ruff's error rules (syntax errors, undefined names) find nothing new compared with pristine upstream; the one hook is present; and every upstream name the DAI code wires into still exists (a rename fails here, naming exactly what moved).
4. **Build** the image, which downloads the latest Player APK from upstream's latest release, and confirm the APK is bundled.
5. **Smoke test:** inside the built image, build the app, confirm every DAI wire is attached exactly once, and drive upstream's own tune, sign-in and relay code through it with stubbed network: the DAI request, the ad swap, the loudness cut and the device User-Agent all have to really happen.
6. **Boot test:** start the container and wait for the web UI to answer.
7. **Publish** to GHCR as `:latest`, `:upstream-<commit>` and `:build-<key>`.
8. **Refresh the patch.** The patch is regenerated against the upstream it just built on and committed back here. It always carries upstream's newest surrounding code, so small upstream edits never pile up into a conflict.

A `:latest` rebuild happens **only** on a real change (a new upstream commit, a new Player APK, or a patch change); a docs or CI tweak just re-runs the tests, never a rebuild.

## Which upstream branch

Builds always come from his **`main`** (his releases). The one exception was a single manual build from his `development` branch on 2026-10-08, published as `:latest` (upstream `b71954d`). He had just merged the Android TV code sign-in there, and this patch now builds on it.

Until his `main` carries that code sign-in (`app/scrapers/directv_device_auth.py`), the scheduled `main` builds **wait**. Each run notes "waiting for his release" and skips, with no failure and no AI repair, and `:latest` stays on the 2026-10-08 development build. The first `main` build after his release builds and publishes normally. Once that's happened, the wait check in `build.yml` (in the "Skip if this exact build already exists" step) can be deleted.

**It watches his `development` branch too**, so the AI fixes drift *before* it reaches a release. On a new development commit it applies the patch and, if upstream drifted, the AI (GLM 5.3 Flash via Aider) adapts the patch and opens a *held* pull request — its prepared fix. A reconcile workflow then re-tests each held fix against `main` and **auto-merges and publishes** it the moment it's proven correct for production (the publish build re-runs the full gauntlet, so a wrong fix can never reach `:latest`).

To do another one-off build from development, run **Build patched image** from the Actions tab with `upstream_branch=development`. That's a test run: it builds and tests the image but publishes nothing.

## How little of upstream it touches

The overlay's code lives in its own files, which upstream doesn't have and so can't conflict with:

- `app/scrapers/directv_dai.py`: ad insertion, the account targeting and the advertising-ID capture.
- `app/scrapers/directv_dai_device.py`: the bridge device's identity and its app User-Agent.
- `app/scrapers/directv_dai_install.py`: the wiring.
- `app/scrapers/dtv_aac_ads.py`: the inserted-ad audio swap.
- `app/scrapers/dtv_aac_gain.py`: the inserted-ad loudness cut.

Upstream's code gets **exactly one line**, at the end of `create_app` in `app/__init__.py`. That line runs the wiring, which attaches DAI to upstream's functions at startup instead of editing their bodies. Upstream can rewrite any of those functions freely and the patch still applies.

What *can* drift is a name: if upstream renames a function the wiring hooks onto, the static checks fail with that exact name before any image is built. The fix is one line in `directv_dai_install.py`, never in upstream's files. At runtime, every piece fails safe: if something it hooks onto is missing or errors, that piece logs an error and the stream plays as if DAI were off.

If any step fails, nothing is published, `:latest` stays on the last good build, and a "Build failed" issue pings you. On the scheduled runs an AI then tries to fix it automatically (the same AI and gauntlet as a patch conflict, below): it reproduces the failing check, adapts the patch to upstream's current code, and the fix must build, smoke-test and boot-test cleanly before it's merged and published. A one-off failure (a registry hiccup, say) clears on the next run.

**You never have to act.** The AI does the fixing and merging; you only get FYI notifications (GitHub emails you) — when a fix is prepared, when a new image is built and published, and, the one worth a glance, if the AI ever can't fix something (it keeps retrying). It costs about nothing to run: GitHub Actions is free on a public repo, and the AI (Flash) runs only on real drift, at pennies.

## When the patch conflicts

If upstream rewrites the lines around the one hook, step 2 fails and the workflow opens an issue here with the upstream commit and instructions.

It then asks an AI to resolve the conflict: [Aider](https://aider.chat) with an OpenRouter model (the `OPENROUTER_API_KEY` secret; the model is set by the `AI_MODEL` repo variable and the workflow default, currently GLM 5.3 Flash, `openrouter/z-ai/glm-5.3-flash`), given the patch and `AGENTS.md` as background. It runs with a read-only token and can edit only the conflicted files. Its result must pass the static checks **and** a full image build, Player APK check, smoke test and boot test before anything is merged. Then CI opens a pull request with it, merges it, and runs the publishing build; the issue closes when that succeeds. If the AI can't produce a passing fix, nothing is merged and the issue gets a comment saying so.

The AI's PRs and every status issue @mention you, so GitHub emails you whenever something happens.

To fix a conflict by hand instead:

```
git clone https://github.com/kineticman/FastChannels.git up && cd up
git checkout <upstream commit from the issue>
../FastChannels-mackid1993/scripts/apply-patches.sh .   # stops at the conflict
# resolve the conflict, then:
git add -u && git am --continue
../FastChannels-mackid1993/scripts/refresh-patches.sh . <upstream commit>
```

Commit the updated `patches/` here. The push triggers a build, and the issue closes itself once it succeeds.

## Changing the patch

Same routine: apply the patches to an upstream checkout, make your change as a commit (or amend), run `scripts/refresh-patches.sh`, and commit `patches/`. Several patches are fine; they apply in filename order.
