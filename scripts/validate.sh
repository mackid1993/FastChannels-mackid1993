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
# 4. The overlay's modules and every hook into upstream's files are present.
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
python - ${html_files[@]+"${html_files[@]}"} <<'PY'
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

echo "== overlay modules, the one hook, and the upstream names it wires into"
# The overlay's code lives in its own modules (files upstream doesn't have). Upstream's
# files carry exactly ONE line: directv_dai_install.install(app) in create_app. install()
# then wraps upstream functions by name at runtime, so the thing that can drift is a NAME
# upstream renames or removes. This lists every such name from directv_dai_install.TARGETS
# and fails with the exact name, before any image is built. Fix a moved name in
# directv_dai_install.py, never by editing upstream's files.
python - "$root" <<'PY'
import ast, os, sys
ok = True
def err(msg):
    global ok
    ok = False
    print(f'::error::{msg}')
for f in ('directv_dai.py', 'directv_dai_device.py', 'directv_dai_install.py', 'dtv_aac_ads.py', 'dtv_aac_gain.py'):
    if not os.path.isfile(f'app/scrapers/{f}'):
        err(f'app/scrapers/{f} is missing')
init = open('app/__init__.py', encoding='utf-8').read()
if 'directv_dai_install.install(app)' not in init:
    err('app/__init__.py: the one DAI hook (directv_dai_install.install(app) in create_app) is missing')
for f in ('app/scrapers/directv_dai_install.py',):
    if not os.path.isfile(f):
        sys.exit(1)
tree = ast.parse(open('app/scrapers/directv_dai_install.py', encoding='utf-8').read())
consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
          for t in n.targets if isinstance(t, ast.Name) and t.id in ('TARGETS', 'TEMPLATE_MARKERS', 'SAVE_CONFIG_RULE')}

def names(body):
    out = set()
    for n in body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split('.')[0] for a in n.names)
        elif isinstance(n, (ast.If, ast.Try)):
            out |= names(n.body) | names(getattr(n, 'orelse', []))
            for h in getattr(n, 'handlers', []):
                out |= names(h.body)
    return out

parsed = {}
for module, path in consts['TARGETS']:
    f = module.replace('.', '/') + '.py'
    if not os.path.isfile(f):
        err(f'upstream module {module} is gone (DAI wires into {module}.{path})')
        continue
    mod = parsed.setdefault(f, ast.parse(open(f, encoding='utf-8').read()))
    head, _, attr = path.partition('.')
    if head not in names(mod.body):
        err(f'upstream renamed or removed {module}.{head} (DAI wires into it; update directv_dai_install.py)')
        continue
    if attr:
        cls = next(n for n in mod.body if isinstance(n, ast.ClassDef) and n.name == head)
        if attr not in names(cls.body):
            err(f'upstream renamed or removed {module}.{path} (DAI wires into it; update directv_dai_install.py)')
tpl = open('app/templates/admin/sources.html', encoding='utf-8').read()
for marker in consts['TEMPLATE_MARKERS']:
    if marker not in tpl:
        err(f'sources.html no longer has {marker!r} (the DAI settings script relies on it)')
rule = consts['SAVE_CONFIG_RULE'].removeprefix('/api')
if f"route('{rule}', methods=['POST']" not in open('app/routes/api_sources.py', encoding='utf-8').read():
    err(f'the save-config route {rule} (POST) moved (DAI clears its cache / captures ad ids on save)')
print('all DAI wiring targets present' if ok else 'DAI wiring targets missing')
sys.exit(0 if ok else 1)
PY
echo "All static checks passed."
