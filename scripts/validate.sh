#!/usr/bin/env bash
# Static checks on a patched upstream checkout, before any image is built.
#
#   UPSTREAM_SHA=<sha> scripts/validate.sh <patched-checkout>
#
# 1. Every Python file compiles.
# 2. ruff's error-class rules (syntax errors, undefined names, invalid comparisons)
#    report nothing NEW compared with pristine upstream. Upstream's own existing
#    findings don't fail the build; anything our patches introduce does.
# 3. Every Jinja template parses.
# 4. The DAI module and every hook into upstream's files are present.
set -euo pipefail

dir=${1:?usage: validate.sh <patched-checkout>}
base=${UPSTREAM_SHA:?set UPSTREAM_SHA to the upstream commit the patches were applied to}
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$dir"

echo "== compile"
python -m compileall -q app wsgi.py run_migrations.py gunicorn.conf.py

echo "== ruff (new errors only)"
rules=E9,F63,F7,F82
targets=(app wsgi.py run_migrations.py gunicorn.conf.py)
tmp=$(mktemp -d)
trap 'git worktree remove --force "$tmp/base" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT
git worktree add --detach -q "$tmp/base" "$base"
ruff check --select "$rules" --output-format=json --exit-zero "${targets[@]}" > "$tmp/patched.json"
(cd "$tmp/base" && ruff check --select "$rules" --output-format=json --exit-zero "${targets[@]}") > "$tmp/base.json"
python "$root/scripts/ruff_diff.py" "$tmp/base.json" "$tmp/patched.json" "$PWD" "$tmp/base"

echo "== templates"
python - <<'PY'
import pathlib, sys
import jinja2
env = jinja2.Environment(loader=jinja2.FileSystemLoader('app/templates'),
                         extensions=['jinja2.ext.do', 'jinja2.ext.loopcontrols'])
errors = 0
paths = sorted(pathlib.Path('app/templates').rglob('*.html'))
for path in paths:
    try:
        env.parse(path.read_text(encoding='utf-8'))
    except jinja2.TemplateSyntaxError as exc:
        errors += 1
        print(f'::error file={path},line={exc.lineno}::{exc.message}')
print(f'{len(paths)} templates parsed, {errors} errors')
sys.exit(1 if errors else 0)
PY

echo "== DAI module and hooks present"
# Nearly all of the DAI code lives in app/scrapers/directv_dai.py, a file upstream
# doesn't have. What sits in upstream's files is a set of one-line hooks; a merge
# (or an AI conflict fix) must not lose any of them.
missing=0
check() { grep -qF "$2" "$1" || { echo "::error file=$1::missing DAI hook: $2"; missing=1; }; }
d=app/scrapers/directv.py
[ -f app/scrapers/directv_dai.py ] || { echo "::error::app/scrapers/directv_dai.py is missing"; missing=1; }
check $d "from . import directv_dai"
check $d "dai: bool = False, dai_extra: dict | None = None,"
check $d "directv_dai.pick_stream_url(pb, dai, dai_extra)"
check $d "'dai': dai,"
check $d "**directv_dai.login_fields(session, bearer, token_data),"
check $d "captured.update(directv_dai.login_fields_from_cookies(captured))"
check $d "directv_dai.store_login_result(cfg, result)"
check $d "directv_dai.CONFIG_FIELD,"
check $d "dai_channel_names: dict = {}"
check $d "directv_dai.note_channel(dai_channel_names, row, ccid)"
check $d "self._update_cache('dai_channel_names', dai_channel_names)"
check $d "directv_dai.cached_url_usable(cached, directv_dai.enabled(self.config))"
check $d "dai_extra=directv_dai.request_flags(self.config, self.cache.get('dai_channel_names'), ccid),"
check $d "dai=directv_dai.enabled(config),"
check app/routes/api_sources.py "directv_dai.clear_cache_if_toggled(source, old, current)"
check app/templates/admin/sources.html "toggleHtml('use_dai'"
[ "$missing" -eq 0 ]
echo "All static checks passed."
