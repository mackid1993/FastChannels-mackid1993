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
# 4. Upstream's files differ by the one hook line only.
# 5. The overlay's modules and every upstream name and string they rely on are present.
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

echo "== upstream's files: exactly the one hook line, nothing else"
# The overlay owns its five modules outright; upstream's files get exactly ONE line, the
# install() call in create_app (inserted by scripts/insert_hook.py). Checked mechanically,
# so no change -- human or AI -- can slip other code into an upstream file.
python - "$base" <<'PY'
import subprocess, sys
base = sys.argv[1]
ours = {f'app/scrapers/{f}' for f in ('directv_dai.py', 'directv_dai_device.py', 'directv_dai_install.py',
                                       'dtv_aac_ads.py', 'dtv_aac_gain.py')}
hook = 'from .scrapers import directv_dai_install; directv_dai_install.install(app)  # mackid1993 overlay: DirecTV DAI'
changed = subprocess.run(['git', 'diff', '--name-only', base, 'HEAD'], capture_output=True, text=True, check=True).stdout.split()
ok = True
for f in changed:
    if f in ours or f == 'app/__init__.py':
        continue
    ok = False
    print(f'::error file={f}::the patch changes upstream file {f}; the overlay may only add its own modules and the one hook line')
diff = subprocess.run(['git', 'diff', '-U0', base, 'HEAD', '--', 'app/__init__.py'], capture_output=True, text=True, check=True).stdout
added = [l[1:] for l in diff.splitlines() if l.startswith('+') and not l.startswith('+++')]
removed = [l[1:] for l in diff.splitlines() if l.startswith('-') and not l.startswith('---')]
if not added or removed or any(l.strip() != hook for l in added):
    ok = False
    print('::error file=app/__init__.py::app/__init__.py must differ from upstream by the DAI hook line(s) only '
          f'(added: {added!r}, removed: {removed!r})')
print('upstream files: only the hook line' if ok else 'upstream files were changed beyond the hook line')
sys.exit(0 if ok else 1)
PY

echo "== overlay modules, the one hook, and the upstream names it uses"
# install() wraps upstream functions by name at runtime (TARGETS) and the modules call a
# few more (USES), so the thing that can drift is a NAME upstream renames or removes, or a
# string the wiring keys on (SOURCE_MARKERS). This checks every one of them and fails with
# the exact name, before any image is built. Fix a moved name in directv_dai_install.py
# (and the module that uses it), never by editing upstream's files.
export DAI_ADMIN_JS="$tmp/dai_admin.js"
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
if not os.path.isfile('app/scrapers/directv_dai_install.py'):
    sys.exit(1)
tree = ast.parse(open('app/scrapers/directv_dai_install.py', encoding='utf-8').read())
consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
          for t in n.targets if isinstance(t, ast.Name)
          and t.id in ('TARGETS', 'USES', 'SOURCE_MARKERS', 'RELAY_PREFIX', '_ADMIN_JS')}

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

def module_file(module):
    base = module.replace('.', '/')
    for f in (base + '.py', base + '/__init__.py'):
        if os.path.isfile(f):
            return f
    return None

parsed = {}
for kind, items in (('wraps', consts['TARGETS']), ('uses', consts['USES'])):
    for module, path in items:
        f = module_file(module)
        if not f:
            err(f'upstream module {module} is gone (DAI {kind} {module}.{path})')
            continue
        mod = parsed.setdefault(f, ast.parse(open(f, encoding='utf-8').read()))
        head, _, attr = path.partition('.')
        if head not in names(mod.body):
            err(f'upstream renamed or removed {module}.{head} (DAI {kind} it; update directv_dai_install.py)')
            continue
        if attr:
            cls = next((n for n in mod.body if isinstance(n, ast.ClassDef) and n.name == head), None)
            if cls is None or attr not in names(cls.body):
                err(f'upstream renamed or removed {module}.{path} (DAI {kind} it; update directv_dai_install.py)')
for path, text, why in consts['SOURCE_MARKERS']:
    if not os.path.isfile(path) or text not in open(path, encoding='utf-8').read():
        err(f'{path} no longer has {text!r}: {why} (update directv_dai_install.py)')
prefix = consts['RELAY_PREFIX']
routes = [os.path.join(d, f) for d, _, fs in os.walk('app/routes') for f in fs if f.endswith('.py')]
if not any(f"route('{prefix}" in open(f, encoding='utf-8').read() for f in routes):
    err(f'no upstream route under {prefix} any more (the ad audio swap and loudness cut key on it)')
print('all DAI wiring targets present' if ok else 'DAI wiring targets missing')
# Hand the admin script to the panel check below.
open(os.environ.get('DAI_ADMIN_JS', '/dev/null'), 'w').write(consts.get('_ADMIN_JS', ''))
sys.exit(0 if ok else 1)
PY

echo "== DAI settings panel script"
# Runs our panel script against a minimal DOM: upstream's settings box for the DirecTV
# source (id source-config-<id>, as sources.html renders it) gets the DAI panel, placed
# just before config-actions. Needs node (preinstalled on GitHub runners).
if command -v node >/dev/null 2>&1; then
  node - "$DAI_ADMIN_JS" <<'JS'
const fs = require('fs');
const js = fs.readFileSync(process.argv[2], 'utf8');
const fail = m => { console.log('::error::DAI settings panel: ' + m); process.exitCode = 1; };
try { new Function(js); } catch (e) { fail('the script does not parse: ' + e); process.exit(1); }
const els = {};
function el(id, tag) {
  const e = {id, tagName: tag || 'DIV', children: [], style: {}, parentNode: null, innerHTML: '', className: '',
    appendChild(c) { c.parentNode = this; this.children.push(c); if (c.id) els[c.id] = c; return c; },
    insertBefore(c, ref) { c.parentNode = this; this.children.splice(this.children.indexOf(ref), 0, c); if (c.id) els[c.id] = c; return c; },
    querySelector(sel) { return sel === '.config-actions' ? this.children.find(c => c.className === 'config-actions') || null : null; },
    addEventListener() {}};
  if (id) els[id] = e;
  return e;
}
const box = el('source-config-7');
const actions = el('', 'DIV');
actions.className = 'config-actions';
box.appendChild(el('', 'DIV'));
box.appendChild(actions);
global.window = global;
global.document = {body: el('body'), getElementById: id => els[id] || null,
                   createElement: t => el('', t.toUpperCase()), addEventListener() {}};
global.MutationObserver = class { observe() {} };
global.setTimeout = f => f();
global.fetch = async url => ({ok: true, json: async () => url === '/directv-dai/sources'
  ? {sources: [{id: 7, name: 'DirecTV'}]}
  : {ok: true, use_dai: false, label: 'Use DirecTV ad insertion (DAI)', help: 'h', issues: []}});
(0, eval)(js);
setTimeout = global.setTimeout;
const done = () => {
  const panel = els['directv-dai-7'];
  if (!panel) return fail('the panel was not added to the DirecTV settings box');
  if (box.children.indexOf(panel) !== box.children.indexOf(actions) - 1) fail('the panel is not placed just before config-actions');
  if (!process.exitCode) console.log('the DAI panel mounts inside the DirecTV settings box');
};
setImmediate(() => setImmediate(() => setImmediate(done)));
JS
else
  echo "node not found; skipped (CI runners have it)"
fi
echo "All static checks passed."
