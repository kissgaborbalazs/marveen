#!/bin/bash
# Pre-check a memoria-heartbeat feladathoz.
#
# MIERT: a kor 935 000 tokenbe kerul (merve 2026-09-10), naponta 12-szer fut, es
# a FO session tmuxaban dol el, tehat minden hivas ujraolvassa a teljes
# beszelgetest. A 04:25-os es 06:25-os kor egyarant ~900 000 tokent koltott
# arra, hogy megallapitsa: nem tortent semmi. Ez a kerdes MERES, nem megitelés,
# ezert szkript donti el, nulla modell-tokenbol.
#
# PROTOKOLL (src/web/scheduled-tasks-io.ts:49-55):
#   exit 0 + "SKIP"      -> a tick kihagyja az LLM-et
#   exit 0 + mas stdout  -> az LLM megkapja a kimenetet a prompt ele fuzve
#   nem-nulla exit       -> fail-open, az LLM ugyis lefut
#
# A szkript maga fail-open: minden varatlan allapot (nincs DB, romlott cursor)
# futast eredmenyez, sosem csendes SKIP-et.
exec python3 /home/kgb/marveen/scripts/hooks/memoria-heartbeat-precheck.py
