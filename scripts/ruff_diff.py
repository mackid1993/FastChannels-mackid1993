"""Fail only on ruff findings that the patched tree has and pristine upstream doesn't.

Findings are compared by (file, rule, message) rather than line number, so
upstream code shifting down a few lines doesn't look like a new error.
"""
import collections
import json
import os
import sys


def load(path, root):
    with open(path, encoding='utf-8') as fh:
        items = json.load(fh)
    counts = collections.Counter()
    by_key = collections.defaultdict(list)
    for item in items:
        rel = os.path.relpath(os.path.realpath(item['filename']), os.path.realpath(root))
        key = (rel, item['code'], item['message'])
        counts[key] += 1
        by_key[key].append(item['location']['row'])
    return counts, by_key


base, _ = load(sys.argv[1], sys.argv[4])
patched, rows = load(sys.argv[2], sys.argv[3])
new = patched - base
for (rel, code, message), n in sorted(new.items()):
    for row in rows[(rel, code, message)][-n:]:
        print(f'::error file={rel},line={row},title=ruff {code}::{message}')
print(f'{sum(base.values())} pre-existing upstream finding(s) ignored, {sum(new.values())} new')
sys.exit(1 if new else 0)
