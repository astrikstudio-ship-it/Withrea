"""Shared paths, config loading and the dice tables."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).parent
ENV_FILE = ROOT / '.env'
CARDS_FILE = ROOT / 'cards.json'
QUEUE_FILE = ROOT / 'queue.json'
SETTINGS_FILE = ROOT / 'settings.json'
SESSION = str(ROOT / 'dice-session')

# emoji → (emoji_char, highest value)
DICE = {
    'dice':       ('\U0001F3B2', 6),    # 🎲
    'dart':       ('\U0001F3AF', 6),    # 🎯
    'basketball': ('\U0001F3C0', 5),    # 🏀
    'football':   ('\u26BD',     5),    # ⚽
    'slots':      ('\U0001F3B0', 64),   # 🎰
    'bowling':    ('\U0001F3B3', 6),    # 🎳
}

# Look up by emoji character → key
EMOJI_TO_KEY = {v[0]: k for k, v in DICE.items()}

ALL_EMOJI = [
    {'emoji': v[0], 'name': k.capitalize(), 'max': v[1]}
    for k, v in DICE.items()
]

ALIASES = {
    'd': 'dice', 'dice': 'dice', 'roll': 'dice',
    'dart': 'dart', 'darts': 'dart', 'target': 'dart',
    'basketball': 'basketball', 'basket': 'basketball', 'b': 'basketball',
    'football': 'football', 'soccer': 'football', 'f': 'football',
    'slots': 'slots', 'slot': 'slots', 's': 'slots',
    'bowling': 'bowling', 'bowl': 'bowling',
}

# Turbo mode: how many parallel throws per round
# 15 gives 93.5% first-batch hit rate on a d6, near-instant for most values
TURBO_PARALLEL = 30


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"\''))


def load_config():
    """api_id and api_hash from my.telegram.org — nothing account-specific."""
    load_env()
    api_id = os.environ.get('TELEGRAM_API_ID', '').strip()
    api_hash = os.environ.get('TELEGRAM_API_HASH', '').strip()

    if not api_id or not api_hash:
        raise SystemExit(
            '\n  Missing credentials.\n'
            '  Go to https://my.telegram.org -> API development tools, create an app,\n'
            f'  then put api_id and api_hash in {ENV_FILE}:\n\n'
            '      TELEGRAM_API_ID=1234567\n'
            '      TELEGRAM_API_HASH=abc123...\n'
        )
    return int(api_id), api_hash


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return fallback


def write_json(path, data):
    try:
        path.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception:
        pass  # a lost cache entry is not worth crashing over
