#!/usr/bin/env python3
"""Insert the overlay's one line into upstream's create_app (app/__init__.py).

    scripts/insert_hook.py <upstream-checkout>          # insert (idempotent)
    scripts/insert_hook.py --check <upstream-checkout>  # exit 1 unless it's in place

The hook is NOT carried as a patch hunk: a hunk carries context lines from upstream's
file, and any upstream edit near the end of create_app would conflict it. This finds
create_app with Python's own parser and puts the line before each `return app` in
it, at that statement's indentation, wherever upstream has moved it. Already present
(an older patch that still carries the hunk): nothing to do.
"""
import ast
import sys

HOOK = 'from .scrapers import directv_dai_install; directv_dai_install.install(app)  # mackid1993 overlay: DirecTV DAI'
PATH = 'app/__init__.py'


def _returns(fn: ast.FunctionDef):
    """`return app` statements in create_app's own body (not in nested functions)."""
    out, todo = [], list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id == 'app':
            out.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return out


def main(argv) -> int:
    check = '--check' in argv
    args = [a for a in argv if a != '--check']
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    path = f'{args[0].rstrip("/")}/{PATH}'
    src = open(path, encoding='utf-8').read()
    if HOOK in src:
        print(f'{PATH}: the DAI hook is in create_app.')
        return 0
    if check:
        print(f'::error::{PATH}: the DAI hook is not in create_app')
        return 1
    tree = ast.parse(src)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'create_app'), None)
    if fn is None:
        print(f'::error title=DAI hook::{PATH} has no module-level create_app any more; '
              'update scripts/insert_hook.py to the new app factory')
        return 1
    lines = src.splitlines(keepends=True)
    # A return on its own line only (`if x: return app` can't take a line before it).
    spots = sorted({r.lineno for r in _returns(fn)
                    if lines[r.lineno - 1][r.col_offset:].lstrip().startswith('return')}, reverse=True)
    if not spots:
        print(f'::error title=DAI hook::create_app in {PATH} no longer ends in `return app`; '
              'update scripts/insert_hook.py to where the app is returned now')
        return 1
    for lineno in spots:
        line = lines[lineno - 1]
        indent = line[:len(line) - len(line.lstrip())]
        lines.insert(lineno - 1, f'{indent}{HOOK}\n')
    open(path, 'w', encoding='utf-8').write(''.join(lines))
    print(f'{PATH}: inserted the DAI hook before {len(spots)} `return app` in create_app.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
