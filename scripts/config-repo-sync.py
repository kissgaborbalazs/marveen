#!/usr/bin/env python3
"""Mirror the fleet's hand-written configuration into a PRIVATE git repo.

WHY (measured 2026-09-10)
-------------------------
The marveen checkout versions code only. `store/`, `CLAUDE.md`, `agents/`,
`~/.claude/scheduled-tasks/` and both skill trees are all outside git -- and
those are exactly the parts that cannot be regenerated: the persona, the two
sub-agents, fifteen scheduled-task descriptions, the skills. The daily Drive
backup (scripts/daily-backup.sh) captures them as a nightly tarball, which
restores state but gives no history: it cannot answer "what did this skill say
last week" or "when did this threshold change".

DESIGN: ALLOWLIST, NOT BLACKLIST
--------------------------------
A secret leaked into a git history is not fixable by deleting the file, so the
copy rule is an explicit allowlist of paths and suffixes. A blacklist ("skip
*.env") fails the first time someone adds a new secret file shape; an allowlist
fails closed -- an unknown file is simply not copied, and --dry-run lists what
was skipped so the list can be widened deliberately.

SECOND LINE: every file that passes the allowlist is still scanned for secret
material (API keys, bot tokens, private keys, OAuth blobs). A hit means the file
is NOT copied and is reported. Both gates must pass.

Usage:
    config-repo-sync.py --dry-run          # report only, touch nothing
    config-repo-sync.py                    # sync + commit + push
    config-repo-sync.py --no-push          # sync + commit, stay local

Exit code: 0 on success (including "nothing changed"), 1 on failure.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

HOME = os.path.expanduser('~')
PROJECT_ROOT = os.environ.get('CONFIG_REPO_SOURCE_ROOT') or '/home/kgb/marveen'
WORK_DIR = os.environ.get('CONFIG_REPO_WORKDIR') or os.path.join(HOME, '.marveen-config-repo')
REMOTE = os.environ.get('CONFIG_REPO_REMOTE') or 'https://github.com/kissgaborbalazs/marveen-config.git'

# (source path, destination subdir, allowed suffixes or exact names)
# Only these are ever copied. Anything else -- including a new file type in an
# allowed directory -- is skipped and reported.
PLAN = [
    (os.path.join(PROJECT_ROOT, 'CLAUDE.md'), 'main-agent/CLAUDE.md', None),
    (os.path.join(PROJECT_ROOT, 'SOUL.md'), 'main-agent/SOUL.md', None),
    (os.path.join(PROJECT_ROOT, 'HEARTBEAT.md'), 'main-agent/HEARTBEAT.md', None),
    (os.path.join(PROJECT_ROOT, 'agents'), 'agents', ('.md', '.json')),
    (os.path.join(HOME, '.claude', 'scheduled-tasks'), 'scheduled-tasks', ('.md', '.json', '.sh', '.py')),
    (os.path.join(HOME, '.claude', 'skills'), 'skills-global', ('.md', '.sh', '.py', '.json')),
    (os.path.join(PROJECT_ROOT, '.claude', 'skills'), 'skills-agent', ('.md', '.sh', '.py', '.json')),
    (os.path.join(HOME, '.claude', 'settings.json'), 'claude/settings.json', None),
    (os.path.join(PROJECT_ROOT, 'store', 'autonomy-config.json'), 'store/autonomy-config.json', None),
    (os.path.join(PROJECT_ROOT, 'store', 'context-guard.json'), 'store/context-guard.json', None),
    (os.path.join(PROJECT_ROOT, 'store', 'alias-mail-allowlist.json'), 'store/alias-mail-allowlist.json', None),
]

# Never copied even when the suffix matches. These names carry credentials or
# per-install identity, and an agent directory is free to contain them.
DENY_NAMES = {
    'access.json', '.env', 'credentials.json', 'token.json',
    '.credentials.json', '.dashboard-token', '.claude-oauth-token',
    # Runtime state, not configuration, and `.claude.json` additionally carries
    # the signed-in account's identity plus a per-project history blob. Measured
    # 2026-09-10 on the first dry-run: the allowlist pulled one per agent.
    '.claude.json', '.last-update-result.json',
}
DENY_DIR_PARTS = {'channels', 'projects', 'plugins', 'cache', 'node_modules',
                  '__pycache__', 'memory', 'inbox', '.git', 'history'}

# Secret shapes. Deliberately broad: a false positive only costs one skipped
# file (reported), a false negative writes a credential into git history.
SECRET_PATTERNS = [
    (re.compile(r'\b\d{8,12}:[A-Za-z0-9_-]{30,}\b'), 'telegram bot token'),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b'), 'github token'),
    (re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b'), 'openai-style key'),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{30,}\b'), 'google api key'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'private key'),
    (re.compile(r'"refresh_token"\s*:\s*"[^"]{20,}"'), 'oauth refresh token'),
    (re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}\b'), 'anthropic key'),
    (re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'), 'slack token'),
]

MAX_BYTES = 512 * 1024  # a hand-written config file is never this big


def scan_secret(path: str) -> str | None:
    try:
        with open(path, 'rb') as fh:
            blob = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        return f'unreadable ({exc})'
    if len(blob) > MAX_BYTES:
        return 'too large for a config file'
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError:
        return 'not utf-8 text'
    for pattern, label in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def walk_allowed(src_dir: str, suffixes) -> list[str]:
    out = []
    for dirpath, dirnames, files in os.walk(src_dir):
        dirnames[:] = [d for d in dirnames if d not in DENY_DIR_PARTS]
        for name in files:
            if name in DENY_NAMES:
                continue
            if suffixes and not name.endswith(suffixes):
                continue
            out.append(os.path.join(dirpath, name))
    return out


def collect() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (copies, skips): copies as (src, rel_dest), skips as (src, reason)."""
    copies: list[tuple[str, str]] = []
    skips: list[tuple[str, str]] = []
    for src, dest, suffixes in PLAN:
        if not os.path.exists(src):
            skips.append((src, 'nem letezik'))
            continue
        if os.path.isfile(src):
            reason = scan_secret(src)
            (skips if reason else copies).append((src, reason) if reason else (src, dest))
            continue
        for path in sorted(walk_allowed(src, suffixes)):
            rel = os.path.relpath(path, src)
            reason = scan_secret(path)
            if reason:
                skips.append((path, reason))
            else:
                copies.append((path, os.path.join(dest, rel)))
    return copies, skips


def run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)
    return out.returncode, ((out.stdout or '') + (out.stderr or '')).strip()


def ensure_repo() -> None:
    if not os.path.isdir(os.path.join(WORK_DIR, '.git')):
        os.makedirs(WORK_DIR, exist_ok=True)
        run(['git', 'init', '-q', '-b', 'main'], cwd=WORK_DIR)
        run(['git', 'remote', 'add', 'origin', REMOTE], cwd=WORK_DIR)
    else:
        rc, out = run(['git', 'remote', 'get-url', 'origin'], cwd=WORK_DIR)
        if rc != 0:
            run(['git', 'remote', 'add', 'origin', REMOTE], cwd=WORK_DIR)
        elif out.strip() != REMOTE:
            run(['git', 'remote', 'set-url', 'origin', REMOTE], cwd=WORK_DIR)


def sync(copies: list[tuple[str, str]]) -> int:
    """Replace the mirrored trees wholesale so deletions propagate too."""
    for top in ('main-agent', 'agents', 'scheduled-tasks', 'skills-global',
                'skills-agent', 'claude', 'store'):
        shutil.rmtree(os.path.join(WORK_DIR, top), ignore_errors=True)
    written = 0
    for src, rel in copies:
        dest = os.path.join(WORK_DIR, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        written += 1
    return written


README = """# marveen-config

A Marveen flotta KEZZEL IRT konfiguracioja, verziozva. PRIVAT repo.

Miert letezik: a marveen kod-repo csak a kodot verziozza. Ez a mappa-fa azt tartalmazza,
ami kivul van rajta es nem regeneralhato: a fo agens personaja, a sub-agensek, az utemezett
feladatok leirasai, a skillek, a hook- es autonomia-beallitasok.

Amit NEM tartalmaz, szandekosan: token, .env, credentials, OAuth, access.json, adatbazis.
A szinkron ALLOWLIST-alapu (scripts/config-repo-sync.py a kod-repoban), es minden atmaso
fajlt kulon titok-szuron is atnyom. Ismeretlen fajltipus nem kerul be -- inkabb kimarad.

Visszaallitas: a fak egy-egy helyre valok.
  main-agent/      -> a telepites gyokere (CLAUDE.md, SOUL.md, HEARTBEAT.md)
  agents/          -> <telepites>/agents/
  scheduled-tasks/ -> ~/.claude/scheduled-tasks/
  skills-global/   -> ~/.claude/skills/
  skills-agent/    -> <telepites>/.claude/skills/
  claude/          -> ~/.claude/
  store/           -> <telepites>/store/
A titkok NEM innen jonnek: azokat a napi Drive-mentes (scripts/daily-backup.sh) tartalmazza.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-push', action='store_true')
    args = ap.parse_args()

    copies, skips = collect()
    print(f'masolando: {len(copies)} fajl | kihagyva: {len(skips)}')
    secret_hits = [(p, r) for p, r in skips if r not in ('nem letezik',)]
    if secret_hits:
        print('\nKIHAGYVA (titok-szuro vagy formatum):')
        for path, reason in secret_hits[:40]:
            print(f'  - {path}  [{reason}]')
    missing = [p for p, r in skips if r == 'nem letezik']
    if missing:
        print('\nNEM LETEZIK (a terv szerint jott volna):')
        for path in missing:
            print(f'  - {path}')

    if args.dry_run:
        by_top: dict[str, int] = {}
        for _src, rel in copies:
            by_top[rel.split(os.sep)[0]] = by_top.get(rel.split(os.sep)[0], 0) + 1
        print('\nfa szerint:')
        for top, count in sorted(by_top.items()):
            print(f'  {top:18s} {count}')
        return 0

    ensure_repo()
    written = sync(copies)
    with open(os.path.join(WORK_DIR, 'README.md'), 'w', encoding='utf-8') as fh:
        fh.write(README)

    run(['git', 'add', '-A'], cwd=WORK_DIR)
    rc, _ = run(['git', 'diff', '--cached', '--quiet'], cwd=WORK_DIR)
    if rc == 0:
        print('nincs valtozas, nincs commit')
        return 0
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    rc, out = run(['git', 'commit', '-q', '-m', f'config snapshot {stamp} ({written} fajl)'], cwd=WORK_DIR)
    if rc != 0:
        print(f'HIBA: commit sikertelen: {out}')
        return 1
    if args.no_push:
        print(f'commit kesz ({written} fajl), push kihagyva')
        return 0
    rc, out = run(['git', 'push', '-q', '-u', 'origin', 'main'], cwd=WORK_DIR)
    if rc != 0:
        print(f'HIBA: push sikertelen: {out}')
        return 1
    print(f'kesz: {written} fajl, commit + push')
    return 0


if __name__ == '__main__':
    sys.exit(main())
