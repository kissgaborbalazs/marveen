#!/usr/bin/env python3
"""Read-only Gmail / Calendar probe for scheduled-task pre-checks.

WHY THIS EXISTS
---------------
A `preCheck` script runs BEFORE the LLM, in plain bash, so it cannot call the
MCP tools the rounds use. Two of sherlock's heartbeats (`alias-postafiok`,
`folyamatos-ellenorzes`) ask questions that are pure measurement -- "is there a
new mail to the alias?", "is there an event in the next two hours?" -- and
without a shell path to Google those rounds must wake the model to find out.
Measured 2026-09-10: 347 000 tokens per alias round, 630 000 per hourly round.

WHAT IT DOES NOT DO: no writes, no sends, no label changes. Gmail is queried
with `users.messages.list` (ids and a count only; message bodies are never
fetched) and Calendar with `events.list`. The rounds themselves still read
content through their MCP tools -- this probe only answers "is there anything".

CREDENTIALS: reuses the MCP clients' own OAuth files, read-only:
  Gmail    ~/.gmail-mcp/token.json         + ~/.gmail-mcp/credentials.json
  Calendar ~/.config/google-calendar-mcp/tokens.json  (key "normal")
The refreshed access token is kept in memory and NEVER written back, so this
script cannot corrupt the MCP clients' state. Tokens are never printed: every
error message is scrubbed before it leaves the process.

Usage:
  google-probe.py gmail-count --query 'to:foo@bar.com is:inbox' [--after EPOCH]
  google-probe.py calendar-count --hours 2 --calendars a@b.com,c@d.com
Both print a single integer on stdout. Exit codes:
  0  the number is trustworthy
  2  the probe FAILED (no network, expired refresh token, API error) -- the
     caller must treat this as "unknown", never as zero.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

GMAIL_TOKEN = os.path.expanduser('~/.gmail-mcp/token.json')
GMAIL_CREDS = os.path.expanduser('~/.gmail-mcp/credentials.json')
CALENDAR_TOKEN = os.path.expanduser('~/.config/google-calendar-mcp/tokens.json')

OAUTH_BASE = os.environ.get('GOOGLE_OAUTH_BASE', 'https://oauth2.googleapis.com')
GMAIL_BASE = os.environ.get('GOOGLE_GMAIL_BASE', 'https://gmail.googleapis.com')
CALENDAR_BASE = os.environ.get('GOOGLE_CALENDAR_BASE', 'https://www.googleapis.com')

TIMEOUT = 25

_SECRETS: list[str] = []


def remember_secret(value: str | None) -> None:
    """Collect token material so it can be scrubbed out of error text."""
    if value and len(value) > 12:
        _SECRETS.append(value)


def scrub(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, '<redacted>')
    return text


def die(msg: str) -> None:
    print(f'probe-error: {scrub(msg)}', file=sys.stderr)
    sys.exit(2)


def read_json(path: str) -> dict:
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def access_token(kind: str) -> str:
    """Exchange the stored refresh token for a fresh access token."""
    try:
        if kind == 'gmail':
            tok = read_json(GMAIL_TOKEN)
            creds = read_json(GMAIL_CREDS)
            refresh = tok.get('refresh_token')
            client_id = creds.get('client_id')
            client_secret = creds.get('client_secret')
        else:
            raw = read_json(CALENDAR_TOKEN)
            entry = raw.get('normal') if isinstance(raw.get('normal'), dict) else raw
            refresh = entry.get('refresh_token')
            # The calendar MCP stores no client pair of its own on this install;
            # both clients were issued from the same OAuth desktop app, so the
            # gmail credentials file is the client identity for both.
            creds = read_json(GMAIL_CREDS)
            client_id = entry.get('client_id') or creds.get('client_id')
            client_secret = entry.get('client_secret') or creds.get('client_secret')
    except FileNotFoundError as exc:
        die(f'hianyzo OAuth fajl: {exc}')
    except Exception as exc:  # noqa: BLE001 - any parse failure is "unknown"
        die(f'OAuth fajl olvasasi hiba: {exc}')

    for value in (refresh, client_secret):
        remember_secret(value)
    if not (refresh and client_id and client_secret):
        die('hianyos OAuth adat (refresh_token / client_id / client_secret)')

    body = urllib.parse.urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh,
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request(f'{OAUTH_BASE}/token', data=body,
                                headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        die(f'token refresh HTTP {exc.code}: {exc.read().decode()[:200]}')
    except Exception as exc:  # noqa: BLE001
        die(f'token refresh hiba: {exc}')
    token = data.get('access_token')
    if not token:
        die('a token refresh nem adott access_token-t')
    remember_secret(token)
    return token


def api_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        die(f'API HTTP {exc.code}: {exc.read().decode()[:200]}')
    except Exception as exc:  # noqa: BLE001
        die(f'API hiba: {exc}')
    return {}


def gmail_count(query: str, after_epoch: int | None) -> int:
    if after_epoch:
        # Gmail's `after:` takes seconds and is inclusive to the day on some
        # clients; the caller compares exact timestamps anyway, so a slightly
        # wide window here is safe (a false signal costs one LLM round, a
        # missed one costs a lost mail).
        query = f'{query} after:{after_epoch}'
    url = (f'{GMAIL_BASE}/gmail/v1/users/me/messages?'
           + urllib.parse.urlencode({'q': query, 'maxResults': 5}))
    data = api_get(url, access_token('gmail'))
    msgs = data.get('messages') or []
    # resultSizeEstimate is an estimate; the id list is exact up to maxResults.
    # For a yes/no gate the list length is the honest answer.
    return len(msgs)


def calendar_count(calendars: list[str], hours: float) -> int:
    token = access_token('calendar')
    now = datetime.now(timezone.utc)
    time_min = now.isoformat().replace('+00:00', 'Z')
    time_max = (now + timedelta(hours=hours)).isoformat().replace('+00:00', 'Z')
    total = 0
    for cal in calendars:
        url = (f'{CALENDAR_BASE}/calendar/v3/calendars/{urllib.parse.quote(cal)}/events?'
               + urllib.parse.urlencode({
                   'timeMin': time_min, 'timeMax': time_max,
                   'singleEvents': 'true', 'orderBy': 'startTime', 'maxResults': 10,
               }))
        data = api_get(url, token)
        total += len(data.get('items') or [])
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('gmail-count')
    g.add_argument('--query', required=True)
    g.add_argument('--after', type=int, default=None)
    c = sub.add_parser('calendar-count')
    c.add_argument('--hours', type=float, default=2.0)
    c.add_argument('--calendars', required=True)
    args = ap.parse_args()

    if args.cmd == 'gmail-count':
        print(gmail_count(args.query, args.after))
    else:
        cals = [x.strip() for x in args.calendars.split(',') if x.strip()]
        if not cals:
            die('nincs megadott naptar')
        print(calendar_count(cals, args.hours))
    return 0


if __name__ == '__main__':
    sys.exit(main())
