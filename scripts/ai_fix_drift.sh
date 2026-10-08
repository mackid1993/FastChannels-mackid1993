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
# The AI (Aider on an OpenRouter model, GLM 5.3 Flash by default) sees the failing check's
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
model=${AI_MODEL:-openrouter/z-ai/glm-5.3-flash}
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

ai_edit() {  # $1 = attempt number
  local pass=$1
  pip install --quiet aider-chat==0.86.2
  cat > "$prompt" <<'PROMPT'
You are running FULLY AUTONOMOUSLY in CI. No human will read your reply or answer anything
-- there is nobody to ask. Never ask a question, request clarification, or wait for
confirmation: decide from the rules below and act. You are given upstream_index.txt -- a map
of upstream's CURRENT files and every def/class in them. Use it to find where upstream moved
the code a hook needs. This can be a large adaptation (effectively backporting the patch onto
a different upstream): re-wire every hook to upstream's current structure. If a capability
looks gone, first check whether the real DATA it was built from is still in upstream and can
be re-implemented (rule 5); make no edit (and say why in one sentence) only when there is
truly no real source left to rebuild it from.

The files you can edit are the FastChannels DirecTV overlay: a PATCH on upstream
kineticman/FastChannels. The patch is exactly two things:
  OURS (whole files, change freely): app/scrapers/directv_dai.py, directv_dai_device.py,
    directv_dai_install.py, dtv_aac_ads.py, dtv_aac_gain.py
  ONE HOOK: a single line at the end of create_app in UPSTREAM's app/__init__.py,
    `from .scrapers import directv_dai_install; directv_dai_install.install(app)`.
    Every other line of every upstream file is UPSTREAM's; never add code to one.
install() wires the feature in at RUNTIME by wrapping upstream functions BY NAME; the names
it depends on are listed in directv_dai_install.TARGETS (plus SAVE_CONFIG_RULE and
TEMPLATE_MARKERS). So the normal drift is: upstream renamed or moved one of those names.
The fix goes in directv_dai_install.py (and TARGETS): point the wrapper at upstream's
current name/location, keeping the wrapper's behavior. Never edit an upstream file to put
the old name back.

A build check is failing, almost always because upstream renamed or moved something a hook
sits in or calls. reproduce.log holds the failing output. AGENTS.md explains every hook.
smoke_test.py and validate.sh are the overlay's own tests -- the exact spec your fix must
satisfy; both are given to you to read. Satisfy them honestly; you cannot edit them.

Keep every hook correctly wired into upstream's CURRENT code. Rules, highest priority first
(an earlier rule wins any conflict between them):

1. SCOPE. Edit only the patch files listed at the end of this message. The tests and every
   other file are read-only; editing any file not in that list ENDS the run as a failure.
   Never edit another file, never ask to add one.
2. HONESTY. Keep all the patch's behavior; the tests are its spec. Never weaken, bypass or
   fake a check.
3. A HOOK FOLLOWS UPSTREAM. When upstream renamed or moved a name a hook calls or sits in,
   update the hook's reference -- anywhere in the files you edit -- to the new name or
   location. This is the normal fix.
4. KEEP A TEST-PINNED NAME REACHABLE. A test may reference, by its OLD name, an upstream name
   that upstream renamed or MOVED. Point that old name at the REAL relocated code (find it in
   upstream_index.txt), by the narrowest mechanism that fits:
     - a renamed function/attribute in a file you edit -> a module-level alias next to it,
       `old_name = new_name` (the real object under both names; inspect.signature/getsource
       resolve through it);
     - a module the patch does not own that moved -> from one of our own modules (imported
       before the test uses the name) register it, e.g.
       `import sys, app.<newpkg>.<newmod> as _m; sys.modules['app.<oldname>'] = _m`,
       so the pinned import resolves to the REAL relocated module.
   No wrapper, no new logic -- just make the old name resolve to real upstream code. This
   settles rule 2's "can't edit the test" against "don't restore old code": you keep one name
   reachable, pointing at real code.
5. BACKPORT A REMOVED CAPABILITY FROM ITS REAL SOURCE; NEVER FABRICATE DATA. A capability a
   hook or test needs may be gone as a named function/module yet still be REBUILDABLE from real
   upstream data that is still present (check upstream_index.txt). Example pattern: a *registry*
   function that returned a list is gone, but the underlying thing(s) it listed are still in
   upstream (a settings field, a single-item accessor, etc.). When that is so, BACKPORT it:
   re-implement the capability in one of OUR OWN modules, reading that REAL upstream source, and
   expose it under the name the hook/test expects -- a module-level alias, or a runtime module
   registered in sys.modules (e.g. build a types.ModuleType carrying your real function and do
   `sys.modules['app.<oldname>'] = that`). That is a genuine port to upstream's current shape,
   NOT fabrication, and it is the correct fix -- do it. The ported code MUST read upstream's
   real data so the feature actually works. FORBIDDEN is only: inventing or hardcoding data
   (made-up `address`/`host`), or a hollow stub that returns nothing/placeholder. Make no edit
   (and say in one sentence what is gone) ONLY when no real data source exists anywhere to
   rebuild the capability from.
6. Never add other non-hook code to an upstream file -- no stubs, no try/except around an
   import to swallow it, no deleted or no-op'd hooks. Your own modules you may change as needed.
7. Minimal diff, valid Python, the patch still applies. No refactoring or reformatting.
8. Never write a secret (API key, bearer, token) into any file.
PROMPT
  # Name the exact files the AI may edit, in the prompt itself.
  { printf '\nThe patch files -- the ONLY files you may edit (editing any other file ends the run):\n'
    sed 's|^|  - |' "$editable"; } >> "$prompt"
  # On a retry, hand Aider its own previous cycle: its chat history (.aider.chat.history.md,
  # passed with --read below) carries what it already tried, its reasoning, and the edits it
  # made -- so it continues instead of restarting cold and repeating a dead end.
  if [ "$pass" -gt 1 ]; then
    printf '\nThis is attempt %s of %s. Your earlier attempt(s) are in the chat history you were given, and the gauntlet STILL failed (the latest failure is in reproduce.log). Do not repeat an approach that already failed -- commit to the fix the rules point to.\n' "$pass" "$max_passes" >> "$prompt"
  fi
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
  # Aider edits only the files named here; the log, AGENTS.md, the overlay's two tests (the
  # spec) and -- on a retry -- its own prior chat history are read-only context. CI does all
  # git/validation, so Aider's own git, lint, test, shell and URL features are off.
  # Map of upstream's CURRENT tree (files + every def/class) so the AI can find where code
  # moved: it edits only the patch's files, but must see the rest to re-wire the hooks.
  { echo "# upstream files:"
    ( cd "$up" && find app -type f \( -name '*.py' -o -name '*.html' \) | sort )
    echo; echo "# definitions (path:line: def/class):"
    ( cd "$up" && find app -name '*.py' -type f -print0 | sort -z \
        | xargs -0 grep -nHE '^[[:space:]]*(async def|def|class) ' 2>/dev/null )
  } > "$work/upstream_index.txt"
  local hist=()
  [ "$pass" -gt 1 ] && [ -f "$up/.aider.chat.history.md" ] && hist=(--read "$up/.aider.chat.history.md")
  ( cd "$up" && aider --model "$model" --edit-format diff --yes-always --no-git \
        --no-auto-commits --no-auto-lint --no-auto-test --no-suggest-shell-commands \
        --no-detect-urls --no-show-model-warnings --no-check-update --no-analytics \
        --no-pretty --map-tokens 0 --read "$log" --read "$root/AGENTS.md" \
        --read "$work/upstream_index.txt" \
        --read "$root/scripts/smoke_test.py" --read "$root/scripts/validate.sh" \
        "${hist[@]}" --message-file "$prompt" "${edit[@]}" )
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
  # Keep Aider's chat history (the per-cycle handoff for the next attempt); drop its other
  # scratch files. Neither is ever staged -- only the patch's files are added and amended.
  ( cd "$up" && find . -maxdepth 1 -name '.aider*' ! -name '.aider.chat.history.md' -exec rm -rf {} + \
      && git add -- $(cat "$editable") && GIT_EDITOR=true git commit -q --amend --no-edit )
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
  ai_edit "$pass" || exit 1
  verify_scope_and_commit || exit 1
  out=$(reproduce); code=$?
  printf '%s\n' "$out"
  if [ "$code" = 20 ]; then
    echo "::error::an AI edit led to an image/APK build failure — not recoverable here"; exit 1
  fi
done
echo "Fixed: the gauntlet passes after $pass AI attempt(s)."
exit 0
