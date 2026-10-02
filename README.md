# FastChannels-mackid1993

[kineticman/FastChannels](https://github.com/kineticman/FastChannels) (`main`, the branch his releases come from) with a DirecTV addition, kept up to date automatically:

- **DirecTV ad insertion (DAI):** an opt-in toggle in the DirecTV source settings. When on, playback uses the Yospace ad-insertion stream DirecTV's own apps use, with the account's own targeting values (DMA, billing ZIP, household and profile IDs) and each bridge device's Android ID and ad-tracking setting. That's how it gets the same local ads an Osprey gets.

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

The DAI code lives in its own file, `app/scrapers/directv_dai.py`, which upstream doesn't have and so can't conflict with. Upstream's files only get about two dozen one-line hooks, which keeps conflicts rare and trivial to fix.

If any step fails, nothing is published, `:latest` stays on the last good build, and a "Build failed" issue pings you. Until it's resolved, the daily run does a full build instead of a drift check, so a one-off failure (a registry hiccup, say) fixes itself the next day.

## When the patch conflicts

If upstream rewrites the lines the patch changes, step 2 fails and the workflow opens an issue here with the upstream commit and instructions.

It then asks an AI to resolve the conflict: [Aider](https://aider.chat) with an OpenRouter model (the `OPENROUTER_API_KEY` secret; the model is pinned to DeepSeek V4.1 Flash, `openrouter/deepseek/deepseek-v4.1-flash`, via the `AI_MODEL` repo variable and the workflow default), given the patch and `AGENTS.md` as background. It runs with a read-only token and can edit only the conflicted files. Its result must pass the static checks **and** a full image build, Player APK check, smoke test and boot test before anything is merged. Then CI opens a pull request with it, merges it, and runs the publishing build; the issue closes when that succeeds. If the AI can't produce a passing fix, nothing is merged and the issue gets a comment saying so.

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
