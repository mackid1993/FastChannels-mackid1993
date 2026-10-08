#!/usr/bin/env bash
# Apply every patch in patches/ (in order) on top of an upstream checkout, then insert
# the overlay's one line into upstream's create_app.
#
#   scripts/apply-patches.sh <upstream-checkout>
#
# The patches only ADD the overlay's own files, so `git am` has no upstream context to
# conflict with. The one line in app/__init__.py is not a hunk: scripts/insert_hook.py
# finds create_app with Python's parser and inserts it, wherever upstream moved it, then
# it's folded into the last patch commit (so `git diff <upstream> HEAD` is the whole
# overlay). Exits non-zero (leaving the checkout clean) when a patch genuinely conflicts.
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

if ! python3 "$root/scripts/insert_hook.py" .; then
    git reset -q --hard "HEAD~${#patches[@]}"
    echo "::error title=DAI hook::could not insert the one DAI line into create_app"
    exit 1
fi
if ! git diff --quiet -- app/__init__.py; then
    git add app/__init__.py
    git commit -q --amend --no-edit
fi
echo "Applied ${#patches[@]} patch(es) on top of upstream."
