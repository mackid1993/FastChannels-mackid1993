#!/usr/bin/env bash
# Rewrite patches/ from the commits sitting on top of an upstream commit.
#
#   scripts/refresh-patches.sh <checkout> <upstream-sha>
#
# CI runs this after every successful build, so the patches always carry the
# context lines of the newest upstream they were applied to. Small upstream edits
# near our changes then never accumulate into a conflict.
#
# To change the patches by hand: check out upstream, apply the patches
# (scripts/apply-patches.sh), edit and commit (amend, or add new commits), then run
# this script against that checkout and commit the result here.
set -euo pipefail

dir=${1:?usage: refresh-patches.sh <checkout> <upstream-sha>}
base=${2:?usage: refresh-patches.sh <checkout> <upstream-sha>}
root=$(cd "$(dirname "$0")/.." && pwd)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git -C "$dir" format-patch -q --zero-commit --no-signature -o "$tmp" "$base..HEAD"
rm -f "$root"/patches/*.patch
cp "$tmp"/*.patch "$root/patches/"
ls "$root/patches"

# Keep source/ (readable copies of the overlay's own modules — files upstream doesn't
# have, which the patch adds in full) in sync with the patch. See source/README.md.
for f in app/scrapers/directv_dai.py app/scrapers/dtv_android.py app/scrapers/dtv_aac_ads.py app/scrapers/dtv_stereo_downmix.py; do
  if [ -f "$dir/$f" ]; then
    mkdir -p "$root/source/$(dirname "$f")"
    cp "$dir/$f" "$root/source/$f"
  fi
done
