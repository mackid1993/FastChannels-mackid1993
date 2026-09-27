# FastChannels-mackid1993

[kineticman/FastChannels](https://github.com/kineticman/FastChannels) (`development`) with one addition, kept up to date automatically:

- **DirecTV ad insertion (DAI):** an opt-in toggle in the DirecTV source settings. When on, playback uses the Yospace ad-insertion stream DirecTV's own apps use, with the web client's ad flags filled from each account's own data (DMA, privacy consent, household/profile ids).

```
docker pull ghcr.io/mackid1993/fastchannels-mackid1993:latest
```

Use it anywhere you'd use upstream's image; the data volume and settings are the same.

## How it stays current

This repo doesn't hold a copy of FastChannels. It holds the patch in `patches/` and a workflow that rebuilds on top of upstream once a week (Mondays, 9 AM Eastern), or whenever you run it by hand from the Actions tab:

1. **Check for changes.** It reads upstream `development`'s newest commit and the newest FastChannels Player release. If neither they nor the patch changed since the last build, it stops without rebuilding.
2. **Apply the patch** to a fresh upstream checkout with `git am --3way`. A 3-way merge means upstream can move, add or edit code around our changes and the patch still lands. It fails only if upstream rewrites the same lines the patch changes.
3. **Static checks:** every Python file compiles; ruff's error rules (syntax errors, undefined names) find nothing new compared with pristine upstream; every template parses; the DAI code is present.
4. **Build** the image, which downloads the latest Player APK from upstream's latest release, and confirm the APK is bundled.
5. **Smoke test:** inside the built image, import the patched modules and run the DAI helpers against synthetic inputs.
6. **Boot test:** start the container and wait for the web UI to answer.
7. **Publish** to GHCR as `:latest`, `:upstream-<commit>` and `:build-<key>`.
8. **Refresh the patch.** The patch is regenerated against the upstream it just built on and committed back here. It always carries upstream's newest surrounding code, so small upstream edits never pile up into a conflict.

If any step fails, nothing is published and `:latest` stays on the last good build.

## When the patch conflicts

If upstream rewrites the lines the patch changes, step 2 fails and the workflow opens an issue here with the upstream commit and instructions.

It then asks Gemini (free API key in the `GEMINI_API_KEY` secret) to resolve the conflict, with `AGENTS.md` as background. Gemini runs with a read-only token and no other secrets, and may only edit the conflicted files. If its result passes the static checks, CI opens a pull request with it, merges it, and starts a normal build. That build must pass every check (image, Player APK, smoke test, boot test) before `:latest` changes, and the issue closes when it succeeds. The merged PR stays in the history if you want to see what Gemini changed.

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
