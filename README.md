# FastChannels-mackid1993

[kineticman/FastChannels](https://github.com/kineticman/FastChannels) (`main`, the branch his releases come from) with a DirecTV addition, kept up to date automatically:

- **DirecTV ad insertion (DAI):** an opt-in toggle in the DirecTV source settings. When on, playback uses the Yospace ad-insertion stream DirecTV's own apps use, with the account's own targeting values (DMA, billing ZIP, consent, household and profile IDs) and each bridge device's Android ID, ad-tracking setting and comScore device name. That's how it gets local and political ads for the account's market. Requests look like DirecTV's Android TV app's: its `channel/v2` authorization request and its player User-Agent, built from each device's own model, board and Android version. Fire TV, Google TV and Shield are all handled the same way, and no device sends an advertising ID.

- **Full-range inserted-ad audio:** many of DirecTV's inserted ads are muffled ("AM radio") in their Dolby (AC-3) encode while the same ad's AAC encode is full range. With DAI on, the relay swaps each inserted ad to its AAC version so breaks sound clear. Swapped ads play in stereo; live programming is untouched. (The muffle is in DirecTV's own files — upstream and the Osprey have it too — so this is the only lever.)

- **Downmix DirecTV audio to stereo (optional):** an off-by-default toggle in the DirecTV source settings. On, it tells the player DirecTV's audio is stereo so a bridge stick with a stereo-only (PCM) output plays a fuller, louder full-range downmix instead of the muffled, quieter in-decoder 5.1→2.0 one — which also lifts the quiet programming closer to the louder inserted ads so you can raise the whole feed uniformly on the encoder/receiver. Leave it off if your output passes AC-3 (5.1) through to an AV receiver.

- **Signs in as DirecTV's Android TV app:** instead of the web-browser client upstream uses, the DirecTV source authenticates end to end as DirecTV's own Android TV app (`UNIFIED_Android_TV_02`) via its device-code grant. There's no username or password to enter: you click Authenticate and approve a code once in a browser at directv.com/tvsigninv2 (the admin page shows a clickable link) — exactly like signing in a real Android TV box — and after that it refreshes its token on its own and never asks again. A Log out button clears the session. The bearer, DRM activation/license and the Yospace ad session are all Android TV, not a web browser.

```
docker pull ghcr.io/mackid1993/fastchannels-mackid1993:latest
```

Use it anywhere you'd use upstream's image; the data volume and settings are the same.

## How it stays current

This repo doesn't hold a copy of FastChannels. It holds the patch in `patches/` and a workflow that rebuilds on top of upstream once a week (Mondays, 9 AM Eastern), or whenever you run it by hand from the Actions tab:

1. **Check for changes.** It reads upstream `main`'s newest commit and the newest FastChannels Player release. If neither they nor the patch changed since the last build, it stops without rebuilding.
2. **Apply the patch** to a fresh upstream checkout with `git am --3way`. A 3-way merge means upstream can move, add or edit code around our changes and the patch still lands. It fails only if upstream rewrites the same lines the patch changes.
3. **Static checks:** every Python file compiles; ruff's error rules (syntax errors, undefined names) find nothing new compared with pristine upstream; every template parses; the DAI code is present.
4. **Build** the image, which downloads the latest Player APK from upstream's latest release, and confirm the APK is bundled.
5. **Smoke test:** inside the built image, import the patched modules and run the DAI helpers against synthetic inputs.
6. **Boot test:** start the container and wait for the web UI to answer.
7. **Publish** to GHCR as `:latest`, `:upstream-<commit>` and `:build-<key>`.
8. **Refresh the patch.** The patch is regenerated against the upstream it just built on and committed back here. It always carries upstream's newest surrounding code, so small upstream edits never pile up into a conflict.

The other six days, a drift check applies the patch to upstream's latest code, runs the static checks and refreshes the patch, without building an image. A conflict is caught the day it appears.

The overlay's code lives in its own files — `app/scrapers/directv_dai.py` (ad insertion), `app/scrapers/dtv_android.py` (the Android TV sign-in, self-contained enough to use without DAI), and `app/scrapers/dtv_aac_ads.py` (the inserted-ad audio swap) — which upstream doesn't have and so can't conflict with. Upstream's files only get a handful of one-line hooks (listed with re-apply instructions in `AGENTS.md`), which keeps conflicts rare and trivial to fix.

If any step fails, nothing is published, `:latest` stays on the last good build, and a "Build failed" issue pings you. Until it's resolved, the daily run does a full build instead of a drift check, so a one-off failure (a registry hiccup, say) fixes itself the next day.

## When the patch conflicts

If upstream rewrites the lines the patch changes, step 2 fails and the workflow opens an issue here with the upstream commit and instructions.

It then asks an AI to resolve the conflict: [Aider](https://aider.chat) with an OpenRouter model (the `OPENROUTER_API_KEY` secret; the model is set by the `AI_MODEL` repo variable and the workflow default, currently GLM 5.3, `openrouter/z-ai/glm-5.3`), given the patch and `AGENTS.md` as background. It runs with a read-only token and can edit only the conflicted files. Its result must pass the static checks **and** a full image build, Player APK check, smoke test and boot test before anything is merged. Then CI opens a pull request with it, merges it, and runs the publishing build; the issue closes when that succeeds. If the AI can't produce a passing fix, nothing is merged and the issue gets a comment saying so.

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
