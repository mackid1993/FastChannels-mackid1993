#!/usr/bin/env bash
# Re-run the build gauntlet on a freshly patched upstream checkout to find out whether a
# build-job failure is a real, patch-fixable drift or something the AI can't (or shouldn't)
# touch. Used by the resolve-drift job before and after the AI edits.
#
#   UPSTREAM_SHA=<sha> [APK=<id>] [CACHE_BUST=<token>] scripts/reproduce.sh <patched-checkout>
#
# Writes the combined output of every stage to ./reproduce.log. Exit codes:
#   10  a drift the AI should try to fix (compile, validate, smoke or boot failed). The
#       failing stage is printed on stdout as `DRIFT_STAGE=<stage>`.
#   20  a failure the AI can't fix (the image build or the APK bundling). Bail, no AI —
#       it's infrastructure or an upstream bug, not something the patch's files can fix.
#    0  everything passed. The build-job failure was transient (a registry/network hiccup)
#       or happened in the publish/refresh steps, which never run here. Bail, nothing to fix.
#
# No `set -e`: each stage's result is handled explicitly so the classification is exact.
set -uo pipefail

dir=${1:?usage: reproduce.sh <patched-checkout>}
base=${UPSTREAM_SHA:?set UPSTREAM_SHA to the upstream commit the patches were applied to}
root=$(cd "$(dirname "$0")/.." && pwd)
log="$PWD/reproduce.log"
image=fastchannels:ci
: > "$log"

stage() { echo "== reproduce: $1 ==" | tee -a "$log"; }

# 1. Compile only the patched Python files (upstream's own files are upstream's concern).
stage compile
py=()
while IFS= read -r f; do py+=("$f"); done \
    < <(git -C "$dir" diff --name-only --diff-filter=d "$base" HEAD -- '*.py')
if [ ${#py[@]} -gt 0 ]; then
    ( cd "$dir" && python -m py_compile "${py[@]}" ) >> "$log" 2>&1 \
        || { echo "DRIFT_STAGE=compile"; exit 10; }
fi

# 2. Static checks (compile of the diff, no-new-ruff-errors, templates, every hook present).
stage validate
UPSTREAM_SHA="$base" "$root/scripts/validate.sh" "$dir" >> "$log" 2>&1 \
    || { echo "DRIFT_STAGE=validate"; exit 10; }

# 3. Build the image. A failure here is infrastructure or an upstream build break, not a
#    hook the patch can adapt, so bail without the AI.
stage build
if ! docker build -t "$image" \
        --build-arg "YTDLP_REFRESH=${CACHE_BUST:-$RANDOM}" \
        --build-arg "FC_PLAYER_APK_REFRESH=${APK:-none}-${CACHE_BUST:-$RANDOM}" \
        -f "$dir/Dockerfile" "$dir" >> "$log" 2>&1; then
    echo "image build failed during reproduce — infrastructure or an upstream build break, not a patch-fixable drift"
    exit 20
fi

stage apk
docker run --rm --entrypoint sh "$image" -c 'test -s /app/fc_player_release.apk' >> "$log" 2>&1 \
    || { echo "the Player APK was not bundled — infrastructure or an upstream change, not a patch-fixable drift"; exit 20; }

# 4. Smoke test the DAI code inside the image, then boot the container.
stage smoke
docker run --rm -w /app -v "$root/scripts:/ci:ro" --entrypoint python "$image" /ci/smoke_test.py >> "$log" 2>&1 \
    || { echo "DRIFT_STAGE=smoke"; exit 10; }

stage boot
"$root/scripts/boot_test.sh" "$image" >> "$log" 2>&1 \
    || { echo "DRIFT_STAGE=boot"; exit 10; }

echo "everything passed on reproduce"
exit 0
