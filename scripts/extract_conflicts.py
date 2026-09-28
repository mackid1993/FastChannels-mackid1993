"""Turn git conflict markers into plain upstream code plus a separate brief.

    python scripts/extract_conflicts.py <brief.md> <file> [<file> ...]

Run inside an upstream checkout where `git am --3way` stopped with conflicts. For
each conflicted file, every conflict block is replaced by upstream's side (the
part between `<<<<<<<` and `=======`), so the file is valid, marker-free upstream
code with all of the patch's non-conflicting hunks already applied. Each block's
two sides are written to the brief, for the AI to apply the patch's side by hand.

Markers are removed because AI edit formats (Aider's SEARCH/REPLACE) use the same
`=======` line as their own separator and get confused by them.
"""
import sys

START, BASE, MID, END = '<<<<<<< ', '||||||| ', '=======', '>>>>>>> '


def split_file(path):
    with open(path, encoding='utf-8', newline='') as fh:
        lines = fh.readlines()
    kept, blocks = [], []
    state, ours, theirs, first_line = None, [], [], 0
    for number, line in enumerate(lines, 1):
        bare = line.rstrip('\r\n')
        if state is None and bare.startswith(START):
            state, ours, theirs, first_line = 'ours', [], [], len(kept) + 1
        elif state == 'ours' and bare.startswith(BASE):
            state = 'base'
        elif state in ('ours', 'base') and bare == MID:
            state = 'theirs'
        elif state == 'theirs' and bare.startswith(END):
            kept.extend(ours)
            blocks.append((first_line, ''.join(ours), ''.join(theirs)))
            state = None
        elif state == 'ours':
            ours.append(line)
        elif state == 'theirs':
            theirs.append(line)
        elif state is None:
            kept.append(line)
    if state is not None:
        raise SystemExit(f'{path}: unterminated conflict block')
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        fh.writelines(kept)
    return blocks


def main():
    brief_path, files = sys.argv[1], sys.argv[2:]
    out = []
    total = 0
    for path in files:
        for index, (line, ours, theirs) in enumerate(split_file(path), 1):
            total += 1
            out.append(f'## {path}, conflict {index} (now around line {line})\n\n'
                       f'Upstream\'s code, which is what the file contains now:\n\n'
                       f'```\n{ours}```\n\n'
                       f'What the patch wanted this code to be, written against the old upstream:\n\n'
                       f'```\n{theirs}```\n')
    with open(brief_path, 'w', encoding='utf-8') as fh:
        fh.write(f'# {total} conflict(s) to resolve\n\n' + '\n'.join(out))
    print(f'Extracted {total} conflict block(s) from {len(files)} file(s) into {brief_path}')


main()
