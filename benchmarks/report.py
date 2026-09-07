"""Summarize paired trials against the agreed criterion: tokens, gated on correctness."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
CHECKS = {'ttl': 11, 'ledger': 12, 'queue': 14}


def load(path):
    records = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def correctness(record):
    """A trial passes only if it exits cleanly, keeps the visible tests, and grades clean."""
    try:
        grading = json.loads(record.get('grading') or '{}')
    except json.JSONDecodeError:
        return None, 'unparsable grader output'
    if not grading:
        return None, 'no grader output'
    total = grading.get('tests', 0)
    passed = total - grading.get('failures', 0) - grading.get('errors', 0)
    notes = []
    if record.get('exit_code') != 0:
        notes.append(f"exit {record.get('exit_code')}")
    if not record.get('visible_tests_unchanged'):
        notes.append('visible tests modified')
    expected = CHECKS.get(record.get('task'))
    if expected is not None and total != expected:
        notes.append(f'{total} checks, expected {expected}')
    return (passed, total), '; '.join(notes)


def main():
    records = load(sys.argv[1] if len(sys.argv) > 1 else ROOT / '.benchmarks/paired-results.jsonl')
    pairs = {}
    for record in records:
        pairs.setdefault((record.get('pair'), record.get('task')), {})[record['harness']] = record
    rows = []
    for (pair, task), sides in sorted(pairs.items(), key=lambda item: (item[0][1], item[0][0])):
        for harness in ('gcodex', 'agy'):
            record = sides.get(harness)
            if record is None:
                continue
            score, notes = correctness(record)
            tokens = record.get('tokens') or {}
            rows.append(dict(pair=pair, task=task, harness=harness,
                             score='n/a' if score is None else f'{score[0]}/{score[1]}',
                             clean=bool(score) and score[0] == score[1] and not notes,
                             total=tokens.get('total'), input=tokens.get('input'),
                             output=tokens.get('output'), seconds=record.get('duration_seconds'),
                             notes=notes))
    print(f"| {'Pair':4} | {'Task':6} | {'Harness':7} | {'Checks':6} | {'Tokens':>9} | "
          f"{'Input':>9} | {'Output':>7} | {'Seconds':>7} | Notes")
    print('| ---- | ------ | ------- | ------ | --------- | --------- | ------- | ------- | -----')
    for row in rows:
        print(f"| {row['pair']:<4} | {row['task']:<6} | {row['harness']:<7} | {row['score']:<6} | "
              f"{row['total'] if row['total'] is not None else '?':>9} | "
              f"{row['input'] if row['input'] is not None else '?':>9} | "
              f"{row['output'] if row['output'] is not None else '?':>7} | "
              f"{row['seconds'] if row['seconds'] is not None else '?':>7} | {row['notes']}")

    print()
    wins = {'gcodex': 0, 'agy': 0, 'excluded': 0}
    for (pair, task), sides in sorted(pairs.items()):
        if set(sides) != {'gcodex', 'agy'}:
            continue
        scored = {}
        for harness, record in sides.items():
            score, notes = correctness(record)
            scored[harness] = (score, notes, (record.get('tokens') or {}).get('total'))
        if any(s is None or s[0] != s[1] or n or t is None for s, n, t in scored.values()):
            wins['excluded'] += 1
            detail = ', '.join(f"{h}: {('n/a' if s is None else f'{s[0]}/{s[1]}')} {n}".strip()
                               for h, (s, n, t) in scored.items())
            print(f'pair {pair} ({task}) excluded from the token comparison — {detail}')
            continue
        winner = min(scored, key=lambda h: scored[h][2])
        loser = 'agy' if winner == 'gcodex' else 'gcodex'
        wins[winner] += 1
        saved = scored[loser][2] - scored[winner][2]
        share = saved / scored[loser][2] * 100
        print(f'pair {pair} ({task}): {winner} used {saved:,} fewer tokens '
              f'({share:.1f}% less than {loser})')
    print()
    print(f"token wins — gcodex {wins['gcodex']}, agy {wins['agy']}, "
          f"not comparable {wins['excluded']}")


if __name__ == '__main__':
    main()
