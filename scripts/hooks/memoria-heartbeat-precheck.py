#!/usr/bin/env python3
"""Deterministic pre-check for the `memoria-heartbeat` scheduled task.

WHY THIS EXISTS (measured 2026-09-10)
-------------------------------------
The memory round costs ~935 000 tokens per firing, 12 firings a day = ~11 M
tokens, and it runs in the MAIN agent's tmux session, i.e. the whole
conversation is re-read on every call. The two rounds at 04:25 and 06:25 on
2026-09-10 each spent ~900 000 tokens to establish that *nothing had happened*.

The round's first question -- "was there anything since the previous round?" --
is not judgement, it is a measurement: new Telegram turns, new inter-agent
messages, kanban movement, new memories, other task firings, approvals, changed
files. A script answers it for zero model tokens. Only the *second* question
(what is worth saving, which skill to patch) needs the model.

PROTOCOL (src/web/scheduled-tasks-io.ts:49-55)
  exit 0 + stdout "SKIP"   -> the tick skips the LLM entirely (zero tokens)
  exit 0 + other stdout    -> the LLM runs, stdout prepended to the prompt
  non-zero exit            -> fail-open, the LLM runs anyway

FAIL-OPEN BY DESIGN: every unexpected condition (missing DB, unreadable state,
clock going backwards) must end in "the LLM runs", never in a silent SKIP. A
false SKIP is an invisible gap in the memory trail; a false run only costs
tokens.

CURSOR: store/memoria-heartbeat-precheck-state.json is rewritten on every run,
including SKIP runs. Each signal is therefore reported exactly once. A signal
lost to a crashed round is acceptable -- the live session saves important things
immediately (CLAUDE.md: "NINCS MENTAL NOTE"); this round is the safety net, not
the primary path.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time

# Paths are env-overridable for ONE reason: the contract tests
# (scripts/__tests__/memoria-heartbeat-precheck.test.py) must exercise the SKIP
# branch against a throwaway database. Without the override a test either reads
# the live DB (so "unchanged state" is never reproducible) or the SKIP branch
# goes untested -- and an untested SKIP branch is exactly how the uuid bug
# survived the first run: the script never crashed, it just never skipped.
# Defaults are the live paths, so the scheduler needs no environment at all.
PROJECT_ROOT = os.environ.get('MEMORIA_PRECHECK_ROOT') or '/home/kgb/marveen'
DB = os.environ.get('MEMORIA_PRECHECK_DB') or os.path.join(PROJECT_ROOT, 'store', 'claudeclaw.db')
STATE = (os.environ.get('MEMORIA_PRECHECK_STATE')
         or os.path.join(PROJECT_ROOT, 'store', 'memoria-heartbeat-precheck-state.json'))
AGENT = 'marveen'
SELF_TASK = 'memoria-heartbeat'

# Paths whose mtime churns on its own (logs, state files, build output, caches).
# A change here is NOT a signal: the monitors rewrite them every minute, so
# including them would make every round report "files changed" and the SKIP
# branch would be dead code.
NOISE_PREFIXES = ('store/', 'node_modules/', 'dist/', '.git/', 'out.tgz')
NOISE_SUFFIXES = ('.log', '.pyc', '.db', '.db-wal', '.db-shm')


def read_state() -> dict:
    try:
        with open(STATE, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # Corrupt state: treat as "no cursor", which forces a run (fail-open).
        return {}


def db_conn() -> sqlite3.Connection:
    # Read-only: this script must never block the dashboard's writers.
    return sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5)


def scalar(conn: sqlite3.Connection, sql: str, args: tuple = ()):
    """Raw cursor value, 0 for an empty table.

    Deliberately NOT coerced to int: `approvals.id` is a uuid string, so an
    int() cast here crashed the whole measurement on the first run (2026-09-10)
    and the script fell back to fail-open on every tick -- a pre-check that
    never skips is the same as no pre-check, only slower. Comparison is by
    equality, so a string cursor works; only the "+delta" display needs ints.
    """
    row = conn.execute(sql, args).fetchone()
    return row[0] if row and row[0] is not None else 0


def git_fingerprint() -> str:
    """Hash of the working tree's real edits: path + size + mtime per file.

    `git status --porcelain` (not a full tree walk) keeps this cheap and makes
    the signal mean "someone edited the project", not "a log line was appended".
    """
    try:
        out = subprocess.run(
            ['git', '-C', PROJECT_ROOT, 'status', '--porcelain', '--untracked-files=all'],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return 'git-error'
        parts = []
        for line in out.stdout.splitlines():
            path = line[3:].strip().strip('"')
            if not path or path.startswith(NOISE_PREFIXES) or path.endswith(NOISE_SUFFIXES):
                continue
            full = os.path.join(PROJECT_ROOT, path)
            try:
                st = os.stat(full)
                parts.append(f'{path}:{st.st_size}:{int(st.st_mtime)}')
            except OSError:
                parts.append(f'{path}:gone')
        parts.sort()
        return hashlib.sha256('\n'.join(parts).encode()).hexdigest()[:16]
    except Exception:
        return 'git-error'


def head_commit() -> str:
    try:
        out = subprocess.run(['git', '-C', PROJECT_ROOT, 'rev-parse', 'HEAD'],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()[:12] if out.returncode == 0 else 'unknown'
    except Exception:
        return 'unknown'


def skills_mtime() -> int:
    """Newest mtime across both skill levels -- a skill edit is a signal."""
    newest = 0
    for root in (os.path.expanduser('~/.claude/skills'), os.path.join(PROJECT_ROOT, '.claude', 'skills')):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith('.md'):
                    continue
                try:
                    newest = max(newest, int(os.stat(os.path.join(dirpath, name)).st_mtime))
                except OSError:
                    continue
    return newest


def collect(conn: sqlite3.Connection) -> dict:
    return {
        'conversation_max_id': scalar(conn, 'select max(id) from conversation_log'),
        'agent_messages_max_id': scalar(conn, 'select max(id) from agent_messages'),
        'memories_max_id': scalar(conn, 'select max(id) from memories'),
        'kanban_events_max_id': scalar(conn, 'select max(id) from kanban_card_events'),
        'kanban_comments_max_id': scalar(conn, 'select max(id) from kanban_comments'),
        'kanban_updated_max': scalar(conn, 'select max(updated_at) from kanban_cards'),
        # approvals.id is a uuid, so max(id) is lexicographic and NOT monotonic
        # (a new row can sort below the previous max and the change vanishes).
        # Count plus the two timestamps catch both a new request and a decision.
        'approvals_count': scalar(conn, 'select count(*) from approvals'),
        'approvals_last_requested': scalar(conn, 'select coalesce(max(requested_at),0) from approvals'),
        'approvals_last_resolved': scalar(conn, 'select coalesce(max(resolved_at),0) from approvals'),
        'idea_box_max_id': scalar(conn, 'select max(id) from idea_box'),
        'idea_box_updated_max': scalar(conn, 'select coalesce(max(updated_at),0) from idea_box'),
        # Other tasks that actually fired (this task's own firings excluded:
        # counting them would make the round its own trigger, forever).
        'other_task_runs_max_id': scalar(
            conn, "select max(id) from task_runs where status='fired' and name<>?", (SELF_TASK,)),
        'git_fingerprint': git_fingerprint(),
        'head_commit': head_commit(),
        'skills_mtime': skills_mtime(),
    }


# Human-readable names for the prompt context, so the round does not have to
# re-measure what this script already knows.
LABELS = {
    'conversation_max_id': 'Telegram beszelgetes-sor',
    'agent_messages_max_id': 'inter-agent uzenet',
    'memories_max_id': 'uj memoria',
    'kanban_events_max_id': 'kanban statusz-valtas',
    'kanban_comments_max_id': 'kanban komment',
    'kanban_updated_max': 'kanban kartya-modositas',
    'approvals_count': 'jovahagyas-keres',
    'approvals_last_requested': 'jovahagyas-keres',
    'approvals_last_resolved': 'jovahagyas-dontes',
    'idea_box_max_id': 'otlet-lada bejegyzes',
    'idea_box_updated_max': 'otlet-lada modositas',
    'other_task_runs_max_id': 'mas utemezett feladat futott',
    'git_fingerprint': 'munkakonyvtar valtozas',
    'head_commit': 'uj commit',
    'skills_mtime': 'skill-fajl modositas',
}


def main() -> int:
    try:
        conn = db_conn()
    except Exception as exc:
        print(f'precheck: DB unreachable ({exc}) -- fail-open, a kor fusson le.')
        return 0

    try:
        now = collect(conn)
    except Exception as exc:
        print(f'precheck: meres hiba ({exc}) -- fail-open, a kor fusson le.')
        return 0
    finally:
        conn.close()

    prev = read_state()
    prev_values = prev.get('values') if isinstance(prev.get('values'), dict) else None

    changed: list[str] = []
    if prev_values is None:
        changed.append('nincs korabbi cursor (elso futas)')
    else:
        for key, value in now.items():
            before = prev_values.get(key)
            if before is None or before != value:
                label = LABELS.get(key, key)
                if isinstance(value, int) and isinstance(before, int) and value > before:
                    delta = value - before
                    changed.append(f'{label} (+{delta})' if key.endswith('_max_id') else label)
                else:
                    changed.append(label)

    # Write the cursor before printing: if the print side is lost, the next run
    # still measures against the current state instead of replaying old signals.
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'checked_at': int(time.time()), 'signals': changed, 'values': now}, fh)
        os.replace(tmp, STATE)
    except Exception as exc:
        print(f'precheck: cursor-iras hiba ({exc}) -- fail-open, a kor fusson le.')
        return 0

    if not changed:
        print('SKIP')
        return 0

    since = prev.get('checked_at')
    when = time.strftime('%H:%M', time.localtime(since)) if since else 'ismeretlen'
    print(f'[PRECHECK] Az elozo ellenorzes ({when}) ota MERT jelek: ' + '; '.join(changed) + '.')
    print('A "tortent-e valami" kerdest ez a szkript mar megvalaszolta -- ne merd ujra. '
          'A te feladatod innen: amit erdemes, mentsd memoriaba, es dontsd el a skill-akciot.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
