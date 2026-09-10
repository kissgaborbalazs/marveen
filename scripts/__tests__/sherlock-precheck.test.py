#!/usr/bin/env python3
"""Contract tests for sherlock's two scheduled-task pre-checks.

Run: python3 scripts/__tests__/sherlock-precheck.test.py

Covers scripts/hooks/alias-postafiok-precheck.py and
scripts/hooks/folyamatos-ellenorzes-precheck.py. Google is never contacted: the
probe is replaced through GOOGLE_PROBE_BIN with a stub whose answers come from
the environment, so every branch (including the failure branches) is reachable.

The assertions are on WHICH branch ran, not on the exit code. Both scripts are
fail-open, so a broken script exits 0 on every tick and looks exactly like a
working one -- that is how the memory pre-check's uuid bug survived its first
run (2026-09-10). "Never SKIP on a failed probe" is the property being locked.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL_DIR = os.path.dirname(os.path.dirname(HERE))
ALIAS = os.path.join(INSTALL_DIR, 'scripts', 'hooks', 'alias-postafiok-precheck.py')
FOLYAMATOS = os.path.join(INSTALL_DIR, 'scripts', 'hooks', 'folyamatos-ellenorzes-precheck.py')

PASS = 0
FAIL = 0

STUB = '''#!/usr/bin/env python3
import os, sys
mode = sys.argv[1] if len(sys.argv) > 1 else ''
if os.environ.get('STUB_FAIL_' + mode.replace('-', '_').upper()):
    print('probe-error: stub forced failure', file=sys.stderr)
    sys.exit(2)
if os.environ.get('STUB_GARBAGE_' + mode.replace('-', '_').upper()):
    print('nem szam')
    sys.exit(0)
print(os.environ.get('STUB_COUNT_' + mode.replace('-', '_').upper(), '0'))
'''


def check(name: str, cond: bool, detail: str = '') -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS: {name}')
    else:
        FAIL += 1
        print(f'  FAIL: {name}{(" -- " + detail) if detail else ""}')


def make_stub(tmp: str) -> str:
    path = os.path.join(tmp, 'probe-stub.py')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(STUB)
    os.chmod(path, 0o755)
    return path


def run(script: str, env_extra: dict, tmp: str) -> tuple[int, str]:
    env = dict(os.environ)
    env['GOOGLE_PROBE_BIN'] = make_stub(tmp)
    env.update({k: str(v) for k, v in env_extra.items()})
    out = subprocess.run([sys.executable, script], env=env, capture_output=True,
                         text=True, timeout=90)
    return out.returncode, (out.stdout or '').strip()


def is_skip(text: str) -> bool:
    return text.strip() == 'SKIP'


def make_root(tmp: str, last_seen: int | None) -> str:
    root = os.path.join(tmp, 'root')
    os.makedirs(os.path.join(root, 'store'), exist_ok=True)
    if last_seen is not None:
        with open(os.path.join(root, 'store', 'alias-mail-state.json'), 'w') as fh:
            json.dump({'last_seen_epoch': last_seen}, fh)
    return root


def make_db(tmp: str, due_dates: list[str]) -> str:
    path = os.path.join(tmp, 'kanban.db')
    conn = sqlite3.connect(path)
    conn.execute("""create table kanban_cards (id text primary key, status text,
                    due_date text, archived_at integer)""")
    for i, due in enumerate(due_dates):
        conn.execute("insert into kanban_cards values (?,?,?,NULL)", (f'c{i}', 'in_progress', due))
    conn.commit()
    conn.close()
    return path


def test_alias() -> None:
    print('\n[1] alias-postafiok pre-check')
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(tmp, None)
        rc, out = run(ALIAS, {'ALIAS_PRECHECK_ROOT': root}, tmp)
        check('nincs kurzor -> nem SKIP', rc == 0 and not is_skip(out), out)

        root = make_root(tmp, int(time.time()) - 3600)
        rc, out = run(ALIAS, {'ALIAS_PRECHECK_ROOT': root, 'STUB_COUNT_GMAIL_COUNT': 0}, tmp)
        check('nulla talalat -> SKIP', is_skip(out), out)

        rc, out = run(ALIAS, {'ALIAS_PRECHECK_ROOT': root, 'STUB_COUNT_GMAIL_COUNT': 2}, tmp)
        check('ket talalat -> jel', not is_skip(out) and '2 talalat' in out, out)
        check('a jel figyelmeztet, hogy a szam FELSO korlat', 'FELSO korlat' in out, out)

        rc, out = run(ALIAS, {'ALIAS_PRECHECK_ROOT': root, 'STUB_FAIL_GMAIL_COUNT': 1}, tmp)
        check('probe hiba -> exit 0 es NEM SKIP (fail-open)', rc == 0 and not is_skip(out), f'rc={rc} {out}')
        check('probe hiba -> a kimenet megnevezi az okot', 'fail-open' in out, out)

        rc, out = run(ALIAS, {'ALIAS_PRECHECK_ROOT': root, 'STUB_GARBAGE_GMAIL_COUNT': 1}, tmp)
        check('nem-szam valasz -> NEM SKIP', not is_skip(out), out)

        # The round owns the cursor: the gate must not advance it, or the round
        # would be told "nothing new" about the mail it was woken for.
        state = os.path.join(root, 'store', 'alias-mail-state.json')
        before = open(state).read()
        run(ALIAS, {'ALIAS_PRECHECK_ROOT': root, 'STUB_COUNT_GMAIL_COUNT': 3}, tmp)
        check('a pre-check NEM irja at a kor allapotfajljat', open(state).read() == before)


def test_folyamatos() -> None:
    print('\n[2] folyamatos-ellenorzes pre-check')
    with tempfile.TemporaryDirectory() as tmp:
        db_empty = make_db(tmp, [])
        base = {'FOLYAMATOS_PRECHECK_DB': db_empty}

        rc, out = run(FOLYAMATOS, dict(base, STUB_COUNT_CALENDAR_COUNT=0, STUB_COUNT_GMAIL_COUNT=0), tmp)
        check('minden tiszta -> SKIP', rc == 0 and is_skip(out), f'rc={rc} {out}')

        rc, out = run(FOLYAMATOS, dict(base, STUB_COUNT_CALENDAR_COUNT=1, STUB_COUNT_GMAIL_COUNT=0), tmp)
        check('naptar-esemeny -> jel', not is_skip(out) and 'naptar-esemeny' in out, out)

        rc, out = run(FOLYAMATOS, dict(base, STUB_COUNT_CALENDAR_COUNT=0, STUB_COUNT_GMAIL_COUNT=4), tmp)
        check('bejovo level -> jel', not is_skip(out) and 'bejovo level' in out, out)
        check('a jel kimondja, hogy a surgosseg NEM a szkript dontese',
              'SURGOS-e, az a te dontesed' in out, out)

    with tempfile.TemporaryDirectory() as tmp:
        today = time.strftime('%Y-%m-%d')
        db_due = make_db(tmp, [today, '2020-01-01', '2999-01-01'])
        rc, out = run(FOLYAMATOS, {'FOLYAMATOS_PRECHECK_DB': db_due,
                                   'STUB_COUNT_CALENDAR_COUNT': 0,
                                   'STUB_COUNT_GMAIL_COUNT': 0}, tmp)
        check('mai + lejart hatarido -> jel (a jovobeli NEM szamit)',
              not is_skip(out) and '2 nyitott kanban' in out, out)

    with tempfile.TemporaryDirectory() as tmp:
        db_empty = make_db(tmp, [])
        rc, out = run(FOLYAMATOS, {'FOLYAMATOS_PRECHECK_DB': db_empty,
                                   'STUB_FAIL_CALENDAR_COUNT': 1,
                                   'STUB_COUNT_GMAIL_COUNT': 0}, tmp)
        check('naptar-probe hiba + semmi jel -> NEM SKIP (reszleges meres)',
              rc == 0 and not is_skip(out), f'rc={rc} {out}')
        check('a reszleges meres megnevezi a sikertelen agat', 'naptar-probe' in out, out)

        rc, out = run(FOLYAMATOS, {'FOLYAMATOS_PRECHECK_DB': db_empty,
                                   'STUB_FAIL_CALENDAR_COUNT': 1,
                                   'STUB_COUNT_GMAIL_COUNT': 2}, tmp)
        check('jel + hibas ag -> a jel mellett ott a NEM MERT lista',
              'NEM MERT' in out and 'bejovo level' in out, out)

        rc, out = run(FOLYAMATOS, {'FOLYAMATOS_PRECHECK_DB': os.path.join(tmp, 'nincs.db'),
                                   'STUB_COUNT_CALENDAR_COUNT': 0,
                                   'STUB_COUNT_GMAIL_COUNT': 0}, tmp)
        check('hianyzo kanban-DB -> NEM SKIP', rc == 0 and not is_skip(out), f'rc={rc} {out}')


def main() -> int:
    for path in (ALIAS, FOLYAMATOS):
        if not os.path.exists(path):
            print(f'HIBA: nem talalhato: {path}')
            return 1
    print('sherlock pre-check contract tests')
    test_alias()
    test_folyamatos()
    print(f'\n{PASS} pass, {FAIL} fail')
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
