#!/usr/bin/env python3
"""Contract tests for scripts/hooks/memoria-heartbeat-precheck.py.

Run: python3 scripts/__tests__/memoria-heartbeat-precheck.test.py

WHAT IS BEING LOCKED OUT (measured 2026-09-10)
----------------------------------------------
The first live version of the pre-check never skipped anything: `approvals.id`
is a uuid string, the int() cast raised, and the fail-open branch ran the LLM on
every tick. From the outside that is indistinguishable from working -- the round
fires, nothing complains, and only the saving is missing. So these tests do NOT
just assert "exit 0"; they assert WHICH branch was taken:

  SKIP branch      unchanged state must print exactly "SKIP"
  signal branch    each signal source must produce a non-SKIP line naming it
  fail-open branch missing DB / corrupt cursor must run the LLM, never SKIP

Every case runs against a throwaway SQLite file and a temp working tree via the
MEMORIA_PRECHECK_* env overrides, so the live database is never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL_DIR = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(INSTALL_DIR, 'scripts', 'hooks', 'memoria-heartbeat-precheck.py')

PASS = 0
FAIL = 0


def pazs(name: str) -> None:
    global PASS
    PASS += 1
    print(f'  PASS: {name}')


def fail(name: str, detail: str = '') -> None:
    global FAIL
    FAIL += 1
    print(f'  FAIL: {name}{(" -- " + detail) if detail else ""}')


def check(name: str, cond: bool, detail: str = '') -> None:
    pazs(name) if cond else fail(name, detail)


# --- fixture -----------------------------------------------------------------

SCHEMA = """
create table conversation_log (id integer primary key autoincrement, text text);
create table agent_messages (id integer primary key autoincrement, content text);
create table memories (id integer primary key autoincrement, content text);
create table kanban_card_events (id integer primary key autoincrement, card_id text);
create table kanban_comments (id integer primary key autoincrement, content text);
create table kanban_cards (id text primary key, updated_at integer);
create table approvals (id text primary key, requested_at integer, resolved_at integer);
create table idea_box (id integer primary key autoincrement, updated_at integer);
create table task_runs (id integer primary key autoincrement, name text, agent text, ts integer, status text);
"""


class Env:
    """A throwaway project root + database + git repo for one test sequence."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix='precheck-test-')
        os.makedirs(os.path.join(self.root, 'store'))
        os.makedirs(os.path.join(self.root, '.claude', 'skills'))
        self.home = tempfile.mkdtemp(prefix='precheck-home-')
        os.makedirs(os.path.join(self.home, '.claude', 'skills'))
        self.db = os.path.join(self.root, 'store', 'test.db')
        self.state = os.path.join(self.root, 'store', 'precheck-state.json')
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        # A baseline row per table: an empty table yields cursor 0, which is also
        # the "missing" value, so the first real insert would be indistinguishable
        # from a schema change.
        conn.execute("insert into conversation_log (text) values ('baseline')")
        conn.execute("insert into agent_messages (content) values ('baseline')")
        conn.execute("insert into memories (content) values ('baseline')")
        conn.execute("insert into kanban_cards (id, updated_at) values ('c1', 1000)")
        conn.execute("insert into task_runs (name, agent, ts, status) values ('memoria-heartbeat','marveen',1000,'fired')")
        conn.commit()
        conn.close()
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=False,
                       capture_output=True)

    def env(self) -> dict:
        e = dict(os.environ)
        e.update({
            'MEMORIA_PRECHECK_ROOT': self.root,
            'MEMORIA_PRECHECK_DB': self.db,
            'MEMORIA_PRECHECK_STATE': self.state,
            'HOME': self.home,
        })
        return e

    def run(self) -> tuple[int, str]:
        out = subprocess.run([sys.executable, SCRIPT], env=self.env(),
                             capture_output=True, text=True, timeout=60)
        return out.returncode, (out.stdout or '').strip()

    def sql(self, stmt: str, args: tuple = ()) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(stmt, args)
        conn.commit()
        conn.close()

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)


def is_skip(stdout: str) -> bool:
    return stdout.strip() == 'SKIP'


# --- tests -------------------------------------------------------------------

def test_skip_and_signal_branches() -> None:
    print('\n[1] SKIP és jel ágak')
    e = Env()
    try:
        rc, out = e.run()
        check('elso futas: nem SKIP (nincs cursor)', rc == 0 and not is_skip(out), f'rc={rc} out={out!r}')
        check('elso futas: allapotfajl letrejott', os.path.exists(e.state))

        rc, out = e.run()
        check('valtozatlan allapot: SKIP', rc == 0 and is_skip(out), f'rc={rc} out={out!r}')

        e.sql("insert into memories (content) values ('uj emlek')")
        rc, out = e.run()
        check('uj memoria: jel, nem SKIP', not is_skip(out) and 'uj memoria' in out, out)
        check('uj memoria: a jel szamszerusitett (+1)', '+1' in out, out)

        rc, out = e.run()
        check('jel utan ujra SKIP (a jel egyszer jelentkezik)', is_skip(out), out)

        e.sql("insert into agent_messages (content) values ('uzenet')")
        rc, out = e.run()
        check('inter-agent uzenet: jel', not is_skip(out) and 'inter-agent' in out, out)
        e.run()

        e.sql("insert into kanban_card_events (card_id) values ('c1')")
        rc, out = e.run()
        check('kanban esemeny: jel', not is_skip(out) and 'kanban' in out, out)
        e.run()

        e.sql("update kanban_cards set updated_at=2000 where id='c1'")
        rc, out = e.run()
        check('kanban kartya-modositas: jel', not is_skip(out), out)
        e.run()

        e.sql("insert into conversation_log (text) values ('Telegram sor')")
        rc, out = e.run()
        check('Telegram beszelgetes-sor: jel', not is_skip(out) and 'Telegram' in out, out)
    finally:
        e.cleanup()


def test_approvals_uuid_regression() -> None:
    print('\n[2] approvals uuid regresszio (ez volt az eredeti hiba)')
    e = Env()
    try:
        e.run()
        e.run()
        # A uuid that sorts BELOW any previous max: a lexicographic max(id)
        # cursor would not move at all, and the int() cast crashed outright.
        e.sql("insert into approvals (id, requested_at, resolved_at) values (?,?,?)",
              ('00000000-' + uuid.uuid4().hex[:8], int(time.time()), 0))
        rc, out = e.run()
        check('uuid azonosito nem okoz meres-hibat', 'meres hiba' not in out, out)
        check('uj jovahagyas-keres: jel', not is_skip(out) and 'jovahagyas' in out, out)
        e.run()
        e.sql("update approvals set resolved_at=? where resolved_at=0", (int(time.time()),))
        rc, out = e.run()
        check('jovahagyas-dontes: jel', not is_skip(out) and 'jovahagyas' in out, out)
    finally:
        e.cleanup()


def test_self_firing_is_not_a_signal() -> None:
    print('\n[3] a sajat futas NEM jel, mas feladate igen')
    e = Env()
    try:
        e.run()
        e.run()
        e.sql("insert into task_runs (name, agent, ts, status) values ('memoria-heartbeat','marveen',?,'fired')",
              (int(time.time() * 1000),))
        rc, out = e.run()
        check('sajat firing: tovabbra is SKIP', is_skip(out), out)
        e.sql("insert into task_runs (name, agent, ts, status) values ('kanban-audit','sherlock',?,'fired')",
              (int(time.time() * 1000),))
        rc, out = e.run()
        check('mas feladat firingje: jel', not is_skip(out) and 'feladat' in out, out)
    finally:
        e.cleanup()


def test_working_tree_noise_filter() -> None:
    print('\n[4] munkakonyvtar: valodi szerkesztes jel, naplo-zaj nem')
    e = Env()
    try:
        e.run()
        e.run()
        with open(os.path.join(e.root, 'store', 'churn.log'), 'w') as fh:
            fh.write('log line\n')
        rc, out = e.run()
        check('store/ alatti naplo: NEM jel (kulonben a SKIP-ag halott kod)', is_skip(out), out)

        with open(os.path.join(e.root, 'valodi-szerkesztes.txt'), 'w') as fh:
            fh.write('tartalom\n')
        rc, out = e.run()
        check('uj kovetetlen fajl: jel', not is_skip(out) and 'munkakonyvtar' in out, out)
        e.run()

        with open(os.path.join(e.root, 'valodi-szerkesztes.txt'), 'a') as fh:
            fh.write('tovabbi tartalom\n')
        rc, out = e.run()
        check('meglevo fajl meretvaltozas: jel', not is_skip(out), out)
    finally:
        e.cleanup()


def test_skill_edit_is_a_signal() -> None:
    print('\n[5] skill-fajl modositas jel')
    e = Env()
    try:
        e.run()
        e.run()
        path = os.path.join(e.home, '.claude', 'skills', 'proba', 'SKILL.md')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write('---\nname: proba\n---\n')
        os.utime(path, (time.time() + 5, time.time() + 5))
        rc, out = e.run()
        check('uj skill-fajl: jel', not is_skip(out) and 'skill' in out, out)
    finally:
        e.cleanup()


def test_fail_open_branches() -> None:
    print('\n[6] fail-open agak (soha nem csendes SKIP)')
    e = Env()
    try:
        e.run()
        e.run()
        os.remove(e.db)
        rc, out = e.run()
        check('hianyzo adatbazis: exit 0', rc == 0, f'rc={rc}')
        check('hianyzo adatbazis: NEM SKIP', not is_skip(out), out)
        check('hianyzo adatbazis: a kimenet megmondja az okot', 'fail-open' in out, out)
    finally:
        e.cleanup()

    e = Env()
    try:
        e.run()
        e.run()
        with open(e.state, 'w') as fh:
            fh.write('{ ez nem valid json')
        rc, out = e.run()
        check('romlott allapotfajl: NEM SKIP', rc == 0 and not is_skip(out), f'rc={rc} out={out!r}')
        rc, out = e.run()
        check('romlott allapot utan a cursor helyreall (ujra SKIP)', is_skip(out), out)
    finally:
        e.cleanup()

    e = Env()
    try:
        e.run()
        e.run()
        os.chmod(os.path.join(e.root, 'store'), 0o500)
        try:
            os.remove(e.state)
        except OSError:
            pass
        rc, out = e.run()
        os.chmod(os.path.join(e.root, 'store'), 0o700)
        check('irhatatlan allapot-konyvtar: exit 0, NEM SKIP', rc == 0 and not is_skip(out), f'rc={rc} out={out!r}')
    finally:
        os.chmod(os.path.join(e.root, 'store'), 0o700)
        e.cleanup()


def test_signal_line_carries_context() -> None:
    print('\n[7] a jel-sor hasznalhato kontextust ad a kornek')
    e = Env()
    try:
        e.run()
        e.run()
        e.sql("insert into memories (content) values ('x')")
        rc, out = e.run()
        check('a jel-sor [PRECHECK] prefixszel jon', out.startswith('[PRECHECK]'), out)
        check('a jel-sor tiltja az ujramerest', 'ne merd ujra' in out, out)
        check('a jel-sor megadja az elozo ellenorzes idejet', 'ota MERT jelek' in out, out)
    finally:
        e.cleanup()


def main() -> int:
    if not os.path.exists(SCRIPT):
        print(f'HIBA: a tesztelt szkript nem talalhato: {SCRIPT}')
        return 1
    print(f'memoria-heartbeat-precheck contract tests ({SCRIPT})')
    test_skip_and_signal_branches()
    test_approvals_uuid_regression()
    test_self_firing_is_not_a_signal()
    test_working_tree_noise_filter()
    test_skill_edit_is_a_signal()
    test_fail_open_branches()
    test_signal_line_carries_context()
    print(f'\n{PASS} pass, {FAIL} fail')
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
