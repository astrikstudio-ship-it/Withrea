"""
One-time login for your own Telegram account.

Run this yourself in a terminal — it asks for your phone number and the code
Telegram sends you. Nothing else ever sees those; they go straight to Telegram
and the result is a session file (dice-session.session) sitting next to this
script. Keep that file private: it is a logged-in copy of your account.

    pip install -r requirements.txt
    python login.py
"""

import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient

from common import SESSION, load_config


async def main():
    api_id, api_hash = load_config()

    # Phone can come from the command line to save typing; the code Telegram
    # sends never can — that prompt is always yours to answer.
    phone = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('TELEGRAM_PHONE')
    kwargs = {'phone': phone} if phone else {}
    if phone:
        print(f'\n  Logging in as {phone}')
        print('  Telegram will send a code to that number — type it below.\n')

    async with TelegramClient(SESSION, api_id, api_hash).start(**kwargs) as client:
        me = await client.get_me()
        name = ' '.join(filter(None, [me.first_name, me.last_name]))
        print(f'\n  Logged in as {name} (@{me.username})')
        print(f'  Session saved to {Path(SESSION).name}.session')
        print('\n  Now run:  python dice.py\n')


if __name__ == '__main__':
    asyncio.run(main())
