#!/usr/bin/env python3
"""Pre-check for the `alias-postafiok` scheduled task (agent: sherlock).

WHY (measured 2026-09-10): the round costs 347 000 tokens and fires twice an
hour. Its first question -- "did a new mail arrive to the +marveen alias since
the last seen one?" -- is a measurement, and `scripts/hooks/google-probe.py`
answers it from the shell, before the model is woken.

PROTOCOL (src/web/scheduled-tasks-io.ts:49-55):
  exit 0 + "SKIP"      -> the tick skips the LLM (zero model tokens)
  exit 0 + other stdout -> the LLM runs, stdout prepended to the prompt
  non-zero exit         -> fail-open, the LLM runs anyway

IT DOES NOT TOUCH THE ROUND'S STATE FILE. `store/alias-mail-state.json` is the
ROUND's cursor: it must still see the mail it was woken for. A pre-check that
advanced that cursor would make the round report "nothing new" about the very
mail that triggered it -- the signal would be consumed by the gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PROJECT_ROOT = os.environ.get('ALIAS_PRECHECK_ROOT') or '/home/kgb/marveen'
STATE = os.path.join(PROJECT_ROOT, 'store', 'alias-mail-state.json')
PROBE = os.environ.get('GOOGLE_PROBE_BIN') or os.path.join(
    PROJECT_ROOT, 'scripts', 'hooks', 'google-probe.py')
QUERY = os.environ.get('ALIAS_PRECHECK_QUERY') or 'to:kissgaborbalazs+marveen@gmail.com is:inbox'


def last_seen() -> int:
    try:
        with open(STATE, encoding='utf-8') as fh:
            return int(json.load(fh).get('last_seen_epoch') or 0)
    except Exception:
        # No cursor (or unreadable): every mail is potentially new -> run.
        return 0


def probe(after: int) -> tuple[int | None, str]:
    cmd = [sys.executable, PROBE, 'gmail-count', '--query', QUERY]
    if after:
        cmd += ['--after', str(after)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception as exc:  # noqa: BLE001
        return None, f'a probe nem indult el ({exc})'
    if out.returncode != 0:
        return None, (out.stderr or out.stdout or 'ismeretlen hiba').strip()[:200]
    try:
        return int((out.stdout or '').strip()), ''
    except ValueError:
        return None, f'a probe nem szamot adott: {(out.stdout or "")[:80]!r}'


def main() -> int:
    after = last_seen()
    if not after:
        print('[PRECHECK] Nincs hasznalhato alias-kurzor (store/alias-mail-state.json), '
              'ezert a kor fusson le es allitsa be.')
        return 0

    count, err = probe(after)
    if count is None:
        print(f'[PRECHECK] A Gmail-probe NEM adott valaszt ({err}) -- fail-open, a kor fusson le. '
              'Ha ez ismetlodik, a probe javitasa maga a jelentenivalo.')
        return 0

    if count == 0:
        print('SKIP')
        return 0

    print(f'[PRECHECK] {count} talalat az alias-postafiokban a legutobb latott level '
          f'(epoch {after}) UTAN. A "jott-e uj level" kerdes eldolt, a tobbi a te dolgod: '
          'olvasd el, ellenorizd a feladot a default-deny listan, es frissitsd az allapotot. '
          'A darabszam a Gmail kereso ablakolasa miatt FELSO korlat, nem pontos ujdonsag-szam.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
