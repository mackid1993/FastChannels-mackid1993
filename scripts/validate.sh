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
# 4. The DAI feature is actually present after the merge.
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

echo "== DAI feature present"
missing=0
check() { grep -q "$2" "$1" || { echo "::error file=$1::expected '$2' after patching"; missing=1; }; }
check app/scrapers/directv.py "ConfigField('use_dai'"
check app/scrapers/directv.py "def _build_dai_query"
check app/scrapers/directv.py "yospace.pool=livepause"
check app/templates/admin/sources.html "use_dai"
check app/routes/api_sources.py "persist_source_cache_updates"
[ "$missing" -eq 0 ]
echo "All static checks passed."
