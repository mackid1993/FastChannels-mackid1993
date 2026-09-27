#!/usr/bin/env bash
# Apply every patch in patches/ (in order) on top of an upstream checkout.
#
#   scripts/apply-patches.sh <upstream-checkout>
#
# Uses `git am --3way`, so a patch still applies when upstream has moved the
# surrounding code, as long as it hasn't rewritten the same lines. Exits non-zero
# (leaving the checkout clean) when a patch genuinely conflicts.
set -euo pipefail

dir=${1:?usage: apply-patches.sh <upstream-checkout>}
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$dir"

export GIT_COMMITTER_NAME=${GIT_COMMITTER_NAME:-fastchannels-ci}
export GIT_COMMITTER_EMAIL=${GIT_COMMITTER_EMAIL:-fastchannels-ci@users.noreply.github.com}

shopt -s nullglob
patches=("$root"/patches/*.patch)
if [ ${#patches[@]} -eq 0 ]; then
    echo "No patches to apply."
    exit 0
fi

for patch in "${patches[@]}"; do
    name=$(basename "$patch")
    echo "Applying $name"
    if ! git am --3way --keep-cr "$patch"; then
        git am --abort >/dev/null 2>&1 || true
        echo "::error title=Patch conflict::$name no longer applies to upstream $(git rev-parse --short HEAD)"
        exit 1
    fi
done
echo "Applied ${#patches[@]} patch(es) on top of upstream."
