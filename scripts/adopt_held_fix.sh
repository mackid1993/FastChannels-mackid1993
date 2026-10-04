#!/usr/bin/env bash
# Land a held fix on main by ADOPTING its files, not by `gh pr merge`. A held PR
# (auto/drift-* / auto/port-*) edits patches/ + source/ from an older main; once a routine
# build refreshes main's single patch file (new index hashes, offsets, context) the held PR
# diverges on that same file and goes CONFLICTING, so `gh pr merge --squash` fails ("not
# mergeable"). But the held PR's patches/ is a complete set we've already validated (and, for
# the build/reconcile callers, adversarially reviewed) against the target upstream — so the
# correct landing is to REPLACE main's patches/ + source/ with the held PR's version, commit,
# push, and close the PR. Pushes with the token don't trigger workflows, so the caller still
# kicks the publish build (which re-runs the full gauntlet before anything reaches :latest).
#
#   GH_TOKEN=<token> GITHUB_REPOSITORY=<owner/repo> scripts/adopt_held_fix.sh <overlay> <pr-number>
set -euo pipefail

ov=$(cd "${1:?usage: adopt_held_fix.sh <overlay-checkout> <pr-number>}" && pwd)
num=${2:?PR number}
: "${GH_TOKEN:?set GH_TOKEN}" "${GITHUB_REPOSITORY:?set GITHUB_REPOSITORY}"

branch=$(gh pr view "$num" -R "$GITHUB_REPOSITORY" --json headRefName -q .headRefName)
[ -n "$branch" ] || { echo "::error::could not resolve the branch for PR #$num"; exit 1; }

git -C "$ov" config user.name  fastchannels-ci
git -C "$ov" config user.email fastchannels-ci@users.noreply.github.com
git -C "$ov" fetch -q origin main "$branch"
# Land on the latest main tip (concurrency serializes builds, so it shouldn't have moved), then
# replace patches/ + source/ wholesale with the held PR's validated version.
git -C "$ov" checkout -q -B main origin/main
git -C "$ov" checkout -q "origin/$branch" -- patches source

git -C "$ov" add -A patches source
if git -C "$ov" diff --cached --quiet; then
  echo "Held fix #$num already matches main; nothing to adopt."
else
  git -C "$ov" commit -q -m "Adopt held fix #$num (reused from the cache)"
  # Token in the URL is the Actions GITHUB_TOKEN; GitHub masks it in logs. Never echo it.
  git -C "$ov" push -q "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" HEAD:main
  echo "Adopted held fix #$num onto main."
fi
# Close silently (a comment is an extra notification email) and remove the branch.
gh pr close "$num" -R "$GITHUB_REPOSITORY" --delete-branch 2>/dev/null || true
