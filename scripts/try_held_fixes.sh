#!/usr/bin/env bash
# The cache of fixes. Every open held PR (branch auto/drift-* or auto/port-*) carries a full,
# already-refreshed patches/ that the AI prepared earlier against some upstream — and because
# each is a complete patch set (not a delta), the newest one is the most cumulative: it folds
# in every earlier adaptation. So before paying the AI to recompute a fix, consult the cache:
# try each held PR's patches/ (newest first) against the CURRENT upstream, and return the first
# that still applies and validates. A hit means we reuse prepared work; a miss means the AI runs.
#
# A candidate that applies + validates then gets a cautious, adversarial AI review of its actual
# changes (scripts/review_fix.py) before it is accepted — a cheap second set of eyes that catches
# a fix which is mechanically fine yet subtly wrong for this upstream. Reuse only on APPROVE;
# otherwise move on (and the caller's AI fixes it properly). The review is skipped gracefully if
# OPENROUTER_API_KEY is unset (deterministic checks only).
#
# Pure lookup — no side effects. It clones upstream into a temp dir and restores the overlay's
# patches/ before returning, so the caller's tree is untouched. On a hit it prints the matching
# PR number as the only stdout line and exits 0; on a miss it prints nothing and exits 1 (all
# chatter goes to stderr). The caller decides what to do with the number (the build and the
# reconcile both merge it and kick a publish, which dedups by build key so it can't over-build).
#
#   GH_TOKEN=<token> GITHUB_REPOSITORY=<owner/repo> \
#       [OPENROUTER_API_KEY=<key> AI_MODEL=<m>]   # enable the adversarial review \
#       scripts/try_held_fixes.sh <overlay-checkout> <upstream-repo> <upstream-sha>
set -uo pipefail

ov=$(cd "${1:?usage: try_held_fixes.sh <overlay> <upstream-repo> <sha>}" && pwd)
upstream_repo=${2:?set the upstream repo, e.g. kineticman/FastChannels}
sha=${3:?set the upstream sha to test the cached fixes against}
: "${GH_TOKEN:?set GH_TOKEN}" "${GITHUB_REPOSITORY:?set GITHUB_REPOSITORY}"

log() { echo "[cache] $*" >&2; }   # chatter to stderr so stdout is just the PR number

# Newest first = most cumulative. Only our AI paths' branches are eligible.
held=$(gh pr list -R "$GITHUB_REPOSITORY" --state open --limit 100 \
  --json number,headRefName,createdAt \
  --jq 'map(select(.headRefName | test("^auto/(drift|port)-"))) | sort_by(.createdAt) | reverse | .[] | "\(.number)\t\(.headRefName)"') || true

if [ -z "$held" ]; then
  log "cache empty: no held fixes to reuse."
  exit 1
fi
log "cache has $(printf '%s\n' "$held" | grep -c .) held fix(es); trying each against upstream $sha (newest first)."

git -C "$ov" fetch -q origin || true
work=$(mktemp -d)
cleanup() { git -C "$ov" checkout -q HEAD -- patches 2>/dev/null || true; rm -rf "$work"; }
trap cleanup EXIT

while IFS=$'\t' read -r num branch; do
  [ -n "$num" ] || continue
  log "trying held fix PR #$num ($branch)…"
  # That PR's patches/, laid over a pristine upstream@sha.
  if ! git -C "$ov" checkout -q "origin/$branch" -- patches 2>/dev/null; then
    log "  can't read $branch; skipping."; continue
  fi
  rm -rf "$work/up"
  if ! git clone -q "https://github.com/$upstream_repo.git" "$work/up" 2>/dev/null; then
    log "  could not clone upstream; skipping."; git -C "$ov" checkout -q HEAD -- patches; continue
  fi
  if ! git -C "$work/up" checkout -q "$sha" 2>/dev/null; then
    log "  upstream has no $sha; skipping."; git -C "$ov" checkout -q HEAD -- patches; continue
  fi

  ok=true
  UPSTREAM_SHA="$sha" "$ov/scripts/apply-patches.sh" "$work/up" >&2 2>&1 || ok=false
  if [ "$ok" = true ]; then
    UPSTREAM_SHA="$sha" "$ov/scripts/validate.sh" "$work/up" >&2 2>&1 || ok=false
  fi

  if [ "$ok" = true ]; then
    if [ -z "${OPENROUTER_API_KEY:-}" ]; then
      log "cache HIT: PR #$num applies and validates against $sha (no key for the review — deterministic reuse)."
      echo "$num"
      exit 0
    fi
    # Second set of eyes before release: a cautious, adversarial review of the ACTUAL changes
    # the cached fix makes against this upstream (cheaper than regenerating, and catches a fix
    # that applies+validates yet is subtly wrong -- what the static checks can't see). Reuse
    # only if the reviewer approves; otherwise the AI will fix it properly.
    verdict=$(git -C "$work/up" diff "$sha" HEAD 2>/dev/null | python3 "$ov/scripts/review_fix.py" "$ov/AGENTS.md" 2>/dev/null)
    if printf '%s\n' "$verdict" | head -1 | grep -q '^APPROVE'; then
      log "cache HIT: PR #$num applies, validates, and the adversarial review APPROVED it — reuse, no AI."
      echo "$num"            # the only stdout line: the reusable PR number
      exit 0
    fi
    log "  PR #$num applies+validates but the review flagged it [$(printf '%s' "$verdict" | head -1)] — not reusing; the AI will fix it properly."
    git -C "$ov" checkout -q HEAD -- patches
    continue
  fi
  log "  PR #$num does not fit $sha."
  git -C "$ov" checkout -q HEAD -- patches   # restore before the next candidate
done <<EOF
$held
EOF

log "cache MISS: no held fix fits $sha; the AI will compute a fresh one."
exit 1
