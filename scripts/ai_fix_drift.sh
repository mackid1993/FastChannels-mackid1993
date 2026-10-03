#!/usr/bin/env bash
# Make a freshly patched upstream checkout pass the full build gauntlet, by adapting the
# patch's OWN files to upstream's current code and nothing else. Shared by both AI repair
# paths in build.yml:
#   - resolve       — a merge conflict was already resolved (git am --continue). The conflict
#                     path used to stop there, so a drift the SAME upstream change also caused
#                     (e.g. an added module importing a name upstream renamed) went unfixed and
#                     the image failed. Now the conflict path runs this too and self-heals.
#   - resolve-drift — the patch applied cleanly but a later check failed (pure drift).
# In both cases the checkout is already patched and committed as one commit on <base>, so
# `git diff <base> HEAD` is exactly the patch and the editable set is exactly its files.
#
#   UPSTREAM_SHA=<sha> OPENROUTER_API_KEY=<key> [AI_MODEL=<m>] [APK=<id>] [CACHE_BUST=<t>] \
#       scripts/ai_fix_drift.sh <patched-checkout> <overlay-checkout>
#
# The AI (Aider on an OpenRouter model, Gemini 2.5 Pro by default) sees the failing check's
# log and AGENTS.md as read-only context and may edit only the patch's files. Every edit is
# verified to stay in that set and to carry no API key, amended into the patch commit, then
# re-checked against the WHOLE gauntlet (not just the stage that failed) — the same bar as a
# human change. Up to 3 attempts, each shown the latest failure; a fix is kept only when
# everything passes.
#
# Exit codes:
#   0  the gauntlet passes and the patch commit was amended with a fix — export it.
#   2  the gauntlet already passed with nothing for the AI to do (no drift). The caller
#      decides what that means: the conflict path exports anyway (its earlier merge
#      resolution is the change); the drift path treats it as a transient build-job hiccup
#      and exports nothing.
#   1  unfixable — an image/APK (infrastructure) failure, a scope violation, a leaked key, or
#      still failing after 3 attempts. Export nothing; the failure stays reproducible.
#
# No `set -e`: reproduce.sh's non-zero exit is a classification, handled explicitly.
set -uo pipefail

dir=${1:?usage: ai_fix_drift.sh <patched-checkout> <overlay-checkout>}
overlay_arg=${2:?usage: ai_fix_drift.sh <patched-checkout> <overlay-checkout>}
base=${UPSTREAM_SHA:?set UPSTREAM_SHA to the upstream commit the patches were applied to}
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY}"
root=$(cd "$(dirname "$0")/.." && pwd)
model=${AI_MODEL:-openrouter/google/gemini-2.5-pro}
max_passes=3

work=$PWD
up=$(cd "$dir" && pwd)
ov=$(cd "$overlay_arg" && pwd)
log="$work/reproduce.log"                 # written by reproduce.sh; read by the AI
editable="$work/ai_editable.txt"
before="$work/ai_before.sha1"
after="$work/ai_after.sha1"
prompt="$work/ai_fix_prompt.txt"

# ruff + jinja2 back validate.sh, which reproduce.sh runs. aider is installed lazily, only if
# there is actually a drift to fix.
pip install --quiet ruff jinja2

# The editable set: exactly the files the patch adds or modifies. The AI may touch nothing else.
( cd "$up" && git diff --name-only "$base" HEAD ) > "$editable"

snapshot() { ( cd "$up" && find . -path ./.git -prune -o -type f -print0 | xargs -0 sha1sum | sort -k2 ) > "$1"; }

reproduce() {  # full gauntlet; prints DRIFT_STAGE on a drift; 0 pass / 10 drift / 20 infrastructure
  UPSTREAM_SHA="$base" APK="${APK:-}" CACHE_BUST="${CACHE_BUST:-ci}-$RANDOM" \
      "$root/scripts/reproduce.sh" "$dir"
}

ai_edit() {
  pip install --quiet aider-chat==0.86.2
  cat > "$prompt" <<'PROMPT'
The files you can edit are from the FastChannels repository with the DirecTV "DAI" patch
already applied. The patch adds its own modules (app/scrapers/directv_dai.py, dtv_android.py,
dtv_aac_ads.py, dtv_aac_gain.py) plus a few one-line hooks in upstream's files. A build check
is failing — almost always because upstream renamed or moved something a hook or an added
module depends on. reproduce.log holds the failing check's output, and AGENTS.md explains the
patch, every hook, and where each one belongs.

Adapt the patch's code to upstream's current structure so the failing check passes. Rules:
- The overlay's own tests (scripts/validate.sh, scripts/smoke_test.py) are the spec for the
  behavior the patch must keep. You cannot edit them. Make the code satisfy them; never try to
  defeat them.
- If upstream renamed or moved a name the patch uses, update the patch's reference to the new
  name or location. That is the fix.
- Do NOT wrap imports in try/except, stub or shadow a missing name, delete a hook, or skip real
  work just to make a check pass. Keep every bit of the patch's behavior.
- Change only what the failure requires. No refactoring, reformatting or unrelated edits.
- Keep the Python valid.
PROMPT
  # The two self-contained audio modules import nothing from upstream (they can't drift), so
  # keep their large bitstream tables out of the edit set unless the failure names one.
  local edit=() f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      *dtv_aac_ads.py|*dtv_aac_gain.py)
        grep -qF "$(basename "$f")" "$log" && edit+=("$f") ;;
      *) edit+=("$f") ;;
    esac
  done < "$editable"
  [ ${#edit[@]} -gt 0 ] || { echo "::error::no editable files to offer the AI"; return 1; }
  # Aider edits only the files named here; the log and AGENTS.md are read-only context. CI does
  # all git/validation, so Aider's own git, lint, test, shell and URL features are off.
  ( cd "$up" && aider --model "$model" --edit-format diff --yes-always --no-git \
        --no-auto-commits --no-auto-lint --no-auto-test --no-suggest-shell-commands \
        --no-detect-urls --no-show-model-warnings --no-check-update --no-analytics \
        --no-pretty --map-tokens 0 --read "$log" --read "$root/AGENTS.md" \
        --message-file "$prompt" "${edit[@]}" )
}

verify_scope_and_commit() {  # 0 if the AI's edits are in-scope, key-free and committed; else 1
  if [ -n "$(git -C "$ov" status --porcelain --untracked-files=no)" ]; then
    echo "::error::the AI modified the overlay checkout (its tests are read-only)"; return 1
  fi
  snapshot "$after"
  local changed stray
  changed=$(diff "$before" "$after" | awk '/^[<>]/{print $3}' | sed 's|^\./||' | sort -u | grep -vE '^\.aider' || true)
  echo "Files the AI changed:"; echo "${changed:-(none)}"
  [ -n "$changed" ] || { echo "::error::the AI made no changes"; return 1; }
  stray=$(printf '%s\n' $changed | grep -vxF -f "$editable" || true)
  [ -z "$stray" ] || { echo "::error::the AI changed files outside the patch: $stray"; return 1; }
  # Belt and braces: the key is never in the model's context, but make sure it can't reach a
  # file that becomes a public PR.
  if ( cd "$up" && grep -qF -- "$OPENROUTER_API_KEY" $changed ); then
    echo "::error::the API key appeared in an edited file"; return 1
  fi
  ( cd "$up" && rm -rf .aider* && git add -- $(cat "$editable") && GIT_EDITOR=true git commit -q --amend --no-edit )
}

# 1. Classify the current state of the applied patch.
out=$(reproduce); code=$?
printf '%s\n' "$out"
if [ "$code" = 0 ]; then
  echo "The gauntlet already passes; no drift for the AI to fix."
  exit 2
fi
if [ "$code" = 20 ]; then
  echo "::error::an image build or APK failure during reproduce — infrastructure or an upstream build break, not a patch-fixable drift"
  exit 1
fi

# 2. A drift (code 10). Let the AI try, re-checking the whole gauntlet after each attempt.
echo "Files the patch touches (the AI may edit only these):"; cat "$editable"
pass=0
while [ "$code" = 10 ]; do
  pass=$((pass + 1))
  if [ "$pass" -gt "$max_passes" ]; then
    echo "::error::still failing the gauntlet after $max_passes AI attempt(s)"; exit 1
  fi
  echo "== AI repair attempt $pass =="
  snapshot "$before"
  ai_edit || exit 1
  verify_scope_and_commit || exit 1
  out=$(reproduce); code=$?
  printf '%s\n' "$out"
  if [ "$code" = 20 ]; then
    echo "::error::an AI edit led to an image/APK build failure — not recoverable here"; exit 1
  fi
done
echo "Fixed: the gauntlet passes after $pass AI attempt(s)."
exit 0
