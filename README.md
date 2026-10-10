# FastChannels-mackid1993

[kineticman/FastChannels](https://github.com/kineticman/FastChannels) with a DirecTV addition, kept up to date automatically:

- **DirecTV ad insertion (DAI):** an opt-in toggle in the DirecTV source settings. When on, playback uses the Yospace ad-insertion stream DirecTV's own apps use, carrying the account's own targeting values (DMA, billing ZIP, consent, household and profile IDs) and each bridge device's real advertising ID and ad-tracking setting, the same values DirecTV's Android TV app sends. That's what gets local and political ads for the account's market. Every request in the session presents one consistent DirecTV Android TV app client: the app's `channel/v2` authorization, and upstream's own app User-Agent on sign-in, DRM and playback alike.

  It needs the AH4C bridge (an Android device); it does nothing on Prismcast. Each device's real advertising ID is captured once and reused: when you turn DAI on (and with a "Capture advertising IDs" button for devices added later), a Fire TV is read silently, and a Google TV/Android TV device briefly shows its Ads settings screen. A device that's playing is skipped, so a stream is never interrupted. The device's Android ID is the fallback when no advertising ID can be read, and a deleted or limited ID sends the opt-out form.

- **Pick which DirecTV profile you watch as:** a *Run as DirecTV profile* picker in the DirecTV settings. Ads are requested as the profile you choose (its Yospace `profid`, as the app does). Switching clears the cached stream so the next tune uses it.

- **Full-range inserted-ad audio:** many of DirecTV's inserted ads are muffled ("AM radio") in their Dolby (AC-3) encode, while the same ad's AAC encode is full range. With DAI on, the relay swaps each inserted ad to its AAC version so breaks sound clear. Swapped ads play in stereo; live programming is untouched.

- **Inserted ads matched to programming loudness:** DirecTV's inserted ads come in several dB louder than the show. With DAI on, the relay lowers each inserted ad to the programming level by editing its AAC gain in place, losslessly (no re-encoding). It only touches inserted ads, and leaves an ad untouched if it can't read it cleanly.

Sign-in is upstream's own "Sign in with a code" (DirecTV's Android TV device-code sign-in: approve a code once at directv.com/tvsigninv2; no password stored; it refreshes on its own).

```
docker pull ghcr.io/mackid1993/fastchannels-mackid1993:latest
```

Use it anywhere you'd use upstream's image; the data volume and settings are the same.

## How it stays current

This repo doesn't hold a copy of FastChannels. It holds the patch in `patches/` and workflows that poll upstream every ~6 hours (GitHub can't be notified of pushes to a repo you don't own) and keep a tested image published on top of his `main`, the branch his releases come from. When his `main` has a new commit or there's a new Player APK release, a build runs; you can also run it by hand from the Actions tab:

1. **Check for changes.** It reads upstream `main`'s newest commit and the newest FastChannels Player release. If neither they nor the patch changed since the last build, it stops without rebuilding.
2. **Apply the patch** to a fresh upstream checkout. The patch only adds the overlay's own files, and `scripts/insert_hook.py` places the one hook line, so there's nothing in upstream's code for it to conflict with.
3. **Static checks:** every Python file compiles; ruff's error rules find nothing new compared with pristine upstream; upstream's files differ by exactly the one hook line; and every upstream name and string the DAI code relies on still exists (a rename fails here, naming exactly what moved).
4. **Build** the image, which downloads the latest Player APK from upstream's latest release, and confirm the APK is bundled.
5. **Smoke test:** inside the built image, build the app, confirm every DAI wire is attached exactly once, and walk the real Fire TV path through the server with the network stubbed: the DAI request and its flags, the ad swap, the loudness cut and the app User-Agent all have to really happen.
6. **Boot test:** start the container and wait for the web UI to answer.
7. **Publish** to GHCR as `:latest`, `:upstream-<commit>` and `:build-<key>`.
8. **Refresh the patch** against the upstream it just built on, and commit it back here.

A `:latest` rebuild happens **only** on a real change (a new upstream commit, a new Player APK, or a patch change); a docs or CI tweak just re-runs the tests.

If upstream `main` doesn't have something the patch needs yet (for example, a feature that's only on his `development` branch so far), the scheduled build waits: it notes why and skips, with no failure and no AI repair, and `:latest` stays on the last good build.

**It watches his `development` branch too.** On each new development commit, `upstream-watch.yml` runs the full test there (`test-development.yml`: build, smoke and boot test; nothing is published). If upstream drifted, the AI (Claude Haiku 5.5 via Aider) prepares a fix as a *held* pull request, and a reconcile workflow merges and publishes it once it's proven correct against `main`. So a breaking change is usually fixed before his release lands.

To build from development on purpose, run **Build patched image** with `upstream_branch=development`. On its own that's a test run (nothing is published); tick `publish` too to publish it as `:latest`. Scheduled builds always use `main`.

## How little of upstream it touches

The overlay's code lives in its own files, which upstream doesn't have and so can't conflict with:

- `app/scrapers/directv_dai.py`: ad insertion, the account targeting, viewer profiles and the advertising-ID capture.
- `app/scrapers/directv_dai_device.py`: the bridge device's identity and the app User-Agent.
- `app/scrapers/directv_dai_install.py`: the wiring.
- `app/scrapers/dtv_aac_ads.py`: the inserted-ad audio swap.
- `app/scrapers/dtv_aac_gain.py`: the inserted-ad loudness cut.

Upstream's code gets **exactly one line**, before `create_app` returns in `app/__init__.py`. That line runs the wiring, which attaches DAI at startup without editing upstream's code. Where it can, it hooks the relay's URLs, the overlay's own settings panel and the `requests` library rather than upstream's internal function names, so upstream can restructure its code freely.

What *can* drift is one of the few upstream names or strings the wiring relies on, all listed at the top of `directv_dai_install.py`. You'll know:
- **The build fails** before any image is built, naming exactly what changed. A "Build failed" issue emails you, and the AI repair takes it. The fix goes in `directv_dai_install.py`, never in upstream's files.
- **The smoke test fails** if the wiring is in place but no longer works.
- **The DAI panel shows it in red** if a running server ever has a mismatch, including when DAI is on but tunes stopped getting a DAI stream. The panel is in the DirecTV source's settings and also at `/directv-dai`.

At runtime every piece fails safe: if something it hooks onto is missing or errors, that piece logs an error and the stream plays as if DAI were off.

If any step fails, nothing is published, `:latest` stays on the last good build, and a "Build failed" issue pings you. On scheduled runs the AI then tries to fix it: it reproduces the failing check, adapts the overlay to upstream's current code, and the fix must build, smoke-test and boot-test cleanly before it's merged and published. A one-off failure (a registry hiccup, say) clears on the next run. A problem the AI can't fix, such as upstream reshaping `create_app` so the hook line can't be placed, opens an issue for a person instead.

**You never have to act.** You only get FYI notifications (GitHub emails you): when a fix is prepared, when a new image is published, and, the one worth a glance, if something needs a person. It costs about nothing to run: GitHub Actions is free on a public repo, and the AI runs only on real drift, at pennies.

## When the patch conflicts

Because the patch only adds the overlay's own files, a conflict should only happen if upstream adds a file with one of the overlay's names. If one does, step 2 fails and the workflow opens an issue with the upstream commit and instructions.

It then asks the AI to resolve it: [Aider](https://aider.chat) with Claude Haiku 5.5 on Anthropic's API (the `ANTHROPIC_API_KEY` secret, paid from the Claude Max plan's monthly API credit; the model is the `AI_MODEL` repo variable, `anthropic/claude-haiku-5-5`). An OpenRouter model still works: set `AI_MODEL` to `openrouter/<model id>` and add an `OPENROUTER_API_KEY` secret. After changing the model, run the **Model preflight** workflow, with the patch and `AGENTS.md` as background. It can edit only the overlay's files, and its result must pass the static checks **and** a full image build, Player APK check, smoke test and boot test before anything is merged. If it can't produce a passing fix, nothing is merged and the issue gets a comment saying so.

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

Apply the patches to an upstream checkout (`scripts/apply-patches.sh`), make your change as a commit (or amend), run `scripts/refresh-patches.sh`, and commit `patches/` and `source/`. Several patches are fine; they apply in filename order.
