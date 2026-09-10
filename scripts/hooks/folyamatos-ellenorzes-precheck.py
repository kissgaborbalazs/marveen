#!/usr/bin/env python3
"""Pre-check for the `folyamatos-ellenorzes` scheduled task (agent: sherlock).

WHY (measured 2026-09-10): the round costs 630 000 tokens and fires hourly. It
asks three questions, and all three are measurement, not judgement:
  calendar  is there an event in the next two hours?
  mail      did anything land in the inbox since the previous tick?
  kanban    is there an open card due today or overdue?
The model is only needed for the fourth step -- deciding whether any of it is
worth waking Gábor for.

DIVISION OF LABOUR: this script answers "is there anything at all", never "is it
urgent". Spam, newsletters and promo mail are NOT filtered here: filtering is
judgement, and a pre-check that silently decided a mail was unimportant would be
the worst of both worlds. One extra LLM round costs tokens; a dropped urgent mail
costs trust.

PROTOCOL (src/web/scheduled-tasks-io.ts:49-55):
  exit 0 + "SKIP"       -> skip the LLM
  exit 0 + other stdout -> run the LLM with this text prepended
  non-zero exit         -> fail-open, run the LLM anyway
Every probe failure is reported as a signal (so the round runs), never as zero.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time

PROJECT_ROOT = os.environ.get('FOLYAMATOS_PRECHECK_ROOT') or '/home/kgb/marveen'
DB = os.environ.get('FOLYAMATOS_PRECHECK_DB') or os.path.join(PROJECT_ROOT, 'store', 'claudeclaw.db')
PROBE = os.environ.get('GOOGLE_PROBE_BIN') or os.path.join(
    PROJECT_ROOT, 'scripts', 'hooks', 'google-probe.py')

# The four calendars the round itself queries. The primary alone is NOT the whole
# picture (measured 2026-09-09: primary empty while dankox held an event), so the
# gate must look at the same four, or it would skip on a half-measurement.
CALENDARS = os.environ.get('FOLYAMATOS_PRECHECK_CALENDARS') or ','.join([
    'kissgaborbalazs@gmail.com',
    'dankox@gmail.com',
    '2net2mkf2dnqilr65c8npifc84mp65rs@import.calendar.google.com',
    'hu.hungarian#holiday@group.v.calendar.google.com',
])
LOOKAHEAD_HOURS = float(os.environ.get('FOLYAMATOS_PRECHECK_HOURS') or 2)
# One cron period plus a margin: the round is hourly, so 70 minutes of mail
# history guarantees no gap between consecutive ticks.
MAIL_WINDOW_MINUTES = int(os.environ.get('FOLYAMATOS_PRECHECK_MAIL_MINUTES') or 70)


def run_probe(args: list[str]) -> tuple[int | None, str]:
    try:
        out = subprocess.run([sys.executable, PROBE, *args],
                             capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return None, f'a probe nem indult el ({exc})'
    if out.returncode != 0:
        return None, (out.stderr or out.stdout or 'ismeretlen hiba').strip()[:200]
    try:
        return int((out.stdout or '').strip()), ''
    except ValueError:
        return None, f'a probe nem szamot adott: {(out.stdout or "")[:80]!r}'


def kanban_due() -> tuple[int | None, str]:
    """Open cards due today or overdue. due_date is stored as 'YYYY-MM-DD'."""
    today = time.strftime('%Y-%m-%d')
    try:
        conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5)
        row = conn.execute(
            """select count(*) from kanban_cards
               where archived_at is null and status in ('planned','in_progress','waiting')
                 and due_date is not null and due_date <> '' and due_date <= ?""",
            (today,)).fetchone()
        conn.close()
        return int(row[0] if row and row[0] is not None else 0), ''
    except Exception as exc:  # noqa: BLE001
        return None, f'kanban-olvasas hiba ({exc})'


def main() -> int:
    signals: list[str] = []
    failures: list[str] = []

    events, err = run_probe(['calendar-count', '--hours', str(LOOKAHEAD_HOURS),
                             '--calendars', CALENDARS])
    if events is None:
        failures.append(f'naptar-probe: {err}')
    elif events:
        signals.append(f'{events} naptar-esemeny a kovetkezo {LOOKAHEAD_HOURS:g} oraban')

    after = int(time.time()) - MAIL_WINDOW_MINUTES * 60
    mails, err = run_probe(['gmail-count', '--query', 'in:inbox', '--after', str(after)])
    if mails is None:
        failures.append(f'email-probe: {err}')
    elif mails:
        signals.append(f'{mails} bejovo level az elmult {MAIL_WINDOW_MINUTES} percben '
                       '(hogy SURGOS-e, az a te dontesed, a szkript nem szurt)')

    due, err = kanban_due()
    if due is None:
        failures.append(err)
    elif due:
        signals.append(f'{due} nyitott kanban-kartya mai vagy lejart hatarido-vel')

    if failures and not signals:
        print('[PRECHECK] Reszleges meres, ezert NEM hagyom ki a kort. Sikertelen agak: '
              + '; '.join(failures) + '. Merd meg magad, amit a szkript nem tudott, '
              'es ha a hiba ismetlodik, az maga a jelentenivalo.')
        return 0

    if not signals:
        print('SKIP')
        return 0

    line = '[PRECHECK] MERT jelek: ' + '; '.join(signals) + '.'
    if failures:
        line += ' NEM MERT (merd magad): ' + '; '.join(failures) + '.'
    print(line)
    print('A "van-e egyaltalan valami" kerdes eldolt -- ne merd ujra a harom forrast. '
          'A te feladatod innen: eldonteni, hogy ez eleg-e a felebresztesere, es ha igen, '
          'inter-agent uzenetben szolni Marveennek.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
