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
#   UPSTREAM_SHA=<sha> ANTHROPIC_API_KEY=<key> (or OPENROUTER_API_KEY for openrouter/ models) [AI_MODEL=<m>] [APK=<id>] [CACHE_BUST=<t>] \
#       scripts/ai_fix_drift.sh <patched-checkout> <overlay-checkout>
#
# The AI (Aider on Claude Haiku 5.5 via Anthropic by default; any openrouter/ model works too) sees the failing check's
# log and AGENTS.md as read-only context and may edit only the overlay's own files. Every edit is
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
root=$(cd "$(dirname "$0")/.." && pwd)
model=${AI_MODEL:-anthropic/claude-haiku-5-5}
key_var=$(python3 "$root/scripts/openrouter_provider.py" key-var "$model")
[ -n "${!key_var:-}" ] || { echo "::error::$key_var is not set (needed for $model)"; exit 1; }
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

# The editable set: exactly the overlay's own files the patch adds. Upstream's app/__init__.py
# (the one hook line, inserted by apply-patches.sh) is not editable, and validate.sh rejects
# any change to an upstream file beyond that line. The AI may touch nothing else.
( cd "$up" && git diff --name-only "$base" HEAD -- . ':(exclude)app/__init__.py' ) > "$editable"

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
-- there is nobody to ask. Never ask a question or wait for confirmation: decide from the
rules below and act. This chat is upstream's repository (kineticman/FastChannels). You have,
read-only, the FULL CURRENT SOURCE of every upstream module the overlay relies on (e.g.
app/scrapers/directv.py, app/routes/directv_proxy.py), plus upstream_index.txt (their
def/class list) and the repo map (a summary of the rest of upstream). reproduce.log is the
failing check's output. Read upstream's current code to find where it moved what the overlay
uses.

The files you can edit are the FastChannels DirecTV DAI overlay, which upstream
kineticman/FastChannels does not have:
  app/scrapers/directv_dai.py, directv_dai_device.py, directv_dai_install.py,
  dtv_aac_ads.py, dtv_aac_gain.py
Upstream's own files are NOT editable. The overlay's only line in them (the install() call in
create_app) is inserted by CI, not by you, and CI rejects any other change to an upstream file.

install() (directv_dai_install.py) attaches the overlay at runtime. Everything it relies on
in upstream is listed there: TARGETS (functions it wraps) and USES (names the overlay's
modules call or read) map a fixed ROLE (the key) to upstream's (module, 'name' or
'Class.name'); SOURCE_MARKERS (strings in upstream files) and RELAY_PREFIX (the URL prefix
of upstream's DirecTV relay). The overlay's code AND the smoke test reach upstream only
through the roles (up('role'), up_name, up_owner, up_set), never by name, so a rename or
move upstream is usually a one-line fix: the role's (module, name) value. A build check is failing because upstream renamed,
moved or reshaped one of those; reproduce.log names it ("upstream renamed or removed X",
"no longer takes <argument>", "no longer has <string>", or a failing smoke-test assertion).

Rules, highest priority first:
1. SCOPE. Edit only the overlay files listed at the end of this message. The tests
   (smoke_test.py, validate.sh) are read-only: they are the spec your fix must pass.
2. FOLLOW UPSTREAM. Point the overlay at upstream's current name, location, argument or
   string: change the role's (module, name) value in TARGETS/USES (or the SOURCE_MARKERS
   entry). Never rename or drop a role key (the smoke test requires every role), never add
   an alias or shim for the old name, and never weaken a check.
3. KEEP THE BEHAVIOR. If a wrapped function changed shape (new arguments, a different return
   value, a classmethod became something else), adapt the wrapper so it does the same job
   on the new shape. Wrappers bind arguments by name with inspect.signature.
4. REBUILD FROM REAL UPSTREAM DATA. A name in USES may be gone while the data it returned is
   still in upstream. Example: bridge_devices.known_devices() is removed, but the bridge
   devices are still in upstream's settings, its ah4c tuner list and its BridgeDevice rows
   (find them in the repo map or upstream_index.txt). Then re-implement that small read in OUR module (for
   the device list: directv_dai_device.known_bridge_devices(), which returns
   [{'address', 'host'}]), reading those real sources, and point the role's USES value at
   the upstream name it now reads. That is a port, not fabrication.
5. NEVER FABRICATE. Don't invent or hard-code values (no made-up address/host), don't write
   a stub that returns nothing, and don't copy removed upstream code wholesale. Make no edit
   (and say why in one sentence) only when no real upstream source is left to read from.
6. Minimal diff, valid Python, no refactoring or reformatting. Never write a secret (API
   key, bearer, token) into any file.
PROMPT
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
  # Name the exact files the AI may edit, as Aider shows them (paths from the repo root).
  { printf '\nThe overlay files added to this chat -- the ONLY files you may edit (editing any other file ends the run):\n'
    printf '  - %s\n' "${edit[@]}"; } >> "$prompt"
  # Aider edits only the files named here; the log, AGENTS.md, the overlay's two tests (the
  # spec) and -- on a retry -- its own prior chat history are read-only context. CI does all
  # git/validation, so Aider's own git, lint, test, shell and URL features are off.
  # Aider runs with git ON so upstream's checkout is the project root: the model sees
  # upstream as a repo (paths like app/scrapers/directv.py), and Aider's repo map gives it
  # every upstream file's definitions to find where a name moved. (With --no-git Aider's
  # root is just the folder holding the editable files, app/scrapers/, and the model saw
  # three loose files and none of upstream: a live rehearsal failed that way.) Commits,
  # dirty commits and .gitignore edits stay off; CI does all git operations and checks the
  # AI changed only the overlay's files.
  # The repo map favors names already mentioned in the chat, so a RENAMED function can be
  # missing from it; upstream_index.txt adds every def/class of the upstream modules the
  # overlay relies on (TARGETS/USES/SOURCE_MARKERS) and of any file the failure names
  # (~5k tokens), so the new name is always in front of the model.
  local mods
  mods=$( cd "$up" && python3 - "$log" <<'PY'
import ast, os, re, sys
t = ast.parse(open('app/scrapers/directv_dai_install.py', encoding='utf-8').read())
c = {n.targets[0].id: ast.literal_eval(n.value) for n in t.body if isinstance(n, ast.Assign)
     and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ('TARGETS', 'USES', 'SOURCE_MARKERS')}
files = set()
for module, _ in (*c.get('TARGETS', {}).values(), *c.get('USES', {}).values()):
    base = module.replace('.', '/')
    files.update(f for f in (base + '.py', base + '/__init__.py') if os.path.isfile(f))
files.update(p for p, _, _ in c.get('SOURCE_MARKERS', ()) if p.endswith('.py') and os.path.isfile(p))
log = open(sys.argv[1], encoding='utf-8', errors='replace').read()
# Tracebacks from the image say /app/app/...; take the app/... part either way.
files.update(f for f in re.findall(r'(?:/app/)?(app/[\w/]+\.py)\b', log) if os.path.isfile(f))
print(' '.join(sorted(files)))
PY
  )
  { echo "# definitions in the upstream modules the overlay relies on (path:line: def/class):"
    # shellcheck disable=SC2086
    ( cd "$up" && grep -nHE '^[[:space:]]*(async def|def|class) ' $mods 2>/dev/null )
  } > "$work/upstream_index.txt"
  # And upstream's ACTUAL code: every module the overlay relies on, in full, read-only. The
  # model can't re-point a reference into code it can't see (the repo map is only a
  # summary). In the old patch these files were in the chat because the patch edited them;
  # rehearsals without them failed.
  local upstream_reads=() f
  for f in $mods; do upstream_reads+=(--read "$f"); done
  local hist=()
  [ "$pass" -gt 1 ] && [ -f "$up/.aider.chat.history.md" ] && hist=(--read "$up/.aider.chat.history.md")
  # Model settings (see openrouter_provider.py): output budget (Anthropic) or provider routing (OpenRouter).
  python3 "$root/scripts/openrouter_provider.py" aider "$model" > "$work/aider_model_settings.json"
  ( cd "$up" && aider --model "$model" --model-settings-file "$work/aider_model_settings.json" \
        --edit-format diff --yes-always \
        --no-auto-commits --no-dirty-commits --no-gitignore --no-attribute-author \
        --no-attribute-committer --no-auto-lint --no-auto-test --no-suggest-shell-commands \
        --no-detect-urls --no-show-model-warnings --no-check-update --no-analytics \
        --no-pretty --map-tokens 2048 --read "$log" --read "$work/upstream_index.txt" \
        "${upstream_reads[@]}" --read "$root/AGENTS.md" \
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
  local k
  for k in "${ANTHROPIC_API_KEY:-}" "${OPENROUTER_API_KEY:-}"; do
    if [ -n "$k" ] && ( cd "$up" && grep -qF -- "$k" $changed ); then
      echo "::error::an API key appeared in an edited file"; return 1
    fi
  done
  # Keep Aider's chat history (the per-cycle handoff for the next attempt); drop its other
  # scratch files. Neither is ever staged -- only the patch's files are added and amended.
  ( cd "$up" && find . -maxdepth 1 -name '.aider*' ! -name '.aider.chat.history.md' -exec rm -rf {} + \
      && git add -- $(cat "$editable") && GIT_EDITOR=true git commit -q --amend --no-edit )
}

# 0. The hook line is CI's job (insert_hook.py), not a file the AI may edit: without it
#    nothing here can pass, so don't spend AI attempts on it.
python3 "$root/scripts/insert_hook.py" --check "$dir" \
  || { echo "::error::the DAI hook line is not in create_app; fix scripts/insert_hook.py (not AI-fixable)"; exit 1; }

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
