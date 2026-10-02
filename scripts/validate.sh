#!/usr/bin/env bash
# Static checks on a patched upstream checkout, before any image is built.
#
#   UPSTREAM_SHA=<sha> scripts/validate.sh <patched-checkout>
#
# Every check is limited to what the patches change, so a change upstream makes on
# its own (a newer Python, a new template extension) can't fail the build:
# 1. The Python files the patches touch compile.
# 2. ruff's error-class rules (syntax errors, undefined names, invalid comparisons)
#    report nothing NEW compared with pristine upstream.
# 3. The templates the patches touch parse.
# 4. The DAI module and every hook into upstream's files are present.
set -euo pipefail

dir=${1:?usage: validate.sh <patched-checkout>}
base=${UPSTREAM_SHA:?set UPSTREAM_SHA to the upstream commit the patches were applied to}
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$dir"

py_files=(); html_files=()
while IFS= read -r f; do py_files+=("$f"); done < <(git diff --name-only --diff-filter=d "$base" HEAD -- '*.py')
while IFS= read -r f; do html_files+=("$f"); done < <(git diff --name-only --diff-filter=d "$base" HEAD -- '*.html')

echo "== compile (${#py_files[@]} patched Python file(s))"
[ ${#py_files[@]} -eq 0 ] || python -m py_compile "${py_files[@]}"

echo "== ruff (new errors only)"
rules=E9,F63,F7,F82
targets=(app wsgi.py run_migrations.py gunicorn.conf.py)
tmp=$(mktemp -d)
trap 'git worktree remove --force "$tmp/base" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT
git worktree add --detach -q "$tmp/base" "$base"
ruff check --select "$rules" --output-format=json --exit-zero "${targets[@]}" > "$tmp/patched.json"
(cd "$tmp/base" && ruff check --select "$rules" --output-format=json --exit-zero "${targets[@]}") > "$tmp/base.json"
python "$root/scripts/ruff_diff.py" "$tmp/base.json" "$tmp/patched.json" "$PWD" "$tmp/base"

echo "== templates (${#html_files[@]} patched template(s))"
python - "${html_files[@]}" <<'PY'
import sys
import jinja2
env = jinja2.Environment(extensions=['jinja2.ext.do', 'jinja2.ext.loopcontrols'])
errors = 0
for path in sys.argv[1:]:
    try:
        env.parse(open(path, encoding='utf-8').read())
    except jinja2.TemplateSyntaxError as exc:
        errors += 1
        print(f'::error file={path},line={exc.lineno}::{exc.message}')
print(f'{len(sys.argv) - 1} template(s) parsed, {errors} errors')
sys.exit(1 if errors else 0)
PY

echo "== DAI module and hooks present"
# Nearly all of the DAI code lives in app/scrapers/directv_dai.py, a file upstream
# doesn't have. What sits in upstream's files is a set of one-line hooks; a merge
# (or an AI conflict fix) must not lose any of them. Only the call targets are
# checked, not their arguments, so a correct port that adapts to an upstream
# rename still passes. The smoke test checks the upstream APIs the hooks rely on.
missing=0
check() { grep -qF -- "$2" "$1" || { echo "::error file=$1::missing DAI hook: $2"; missing=1; }; }
check_re() { grep -qE -- "$2" "$1" || { echo "::error file=$1::missing DAI hook: $3"; missing=1; }; }
d=app/scrapers/directv.py
[ -f app/scrapers/directv_dai.py ] || { echo "::error::app/scrapers/directv_dai.py is missing"; missing=1; }
check $d "from . import directv_dai"
check_re $d 'dai_extra: *dict' "the dai_extra parameter of _fetch_channel_playback"
check $d "directv_dai.pick_stream_url("
check $d "'dai': dai"
check $d "directv_dai.login_fields("
check $d "directv_dai.login_fields_from_cookies("
check $d "directv_dai.store_login_result("
check $d "directv_dai.CONFIG_FIELD"
check $d "directv_dai.note_channel("
check $d "_update_cache('dai_channel_names'"
check $d "directv_dai.cached_url_usable("
check $d "directv_dai.request_flags("
check $d "directv_dai.channel_auth_url("
check $d "directv_dai.android_auth_request("
# Two callers pass dai=: resolve() and the license path.
[ "$(grep -cE -- 'dai=directv_dai\.enabled\(' $d)" -ge 2 ] \
    || { echo "::error file=$d::missing DAI hook: dai=directv_dai.enabled(...) in resolve() and the license path"; missing=1; }
check app/routes/api_sources.py "directv_dai.clear_cache_if_toggled("
check app/routes/directv_proxy.py "directv_dai.keep_drm_session("
check app/routes/directv_proxy.py "directv_dai.player_user_agent()"
check app/routes/directv_proxy.py "'yospace.com',  # DAI"
check app/templates/admin/sources.html "toggleHtml('use_dai'"
[ "$missing" -eq 0 ]
echo "All static checks passed."
