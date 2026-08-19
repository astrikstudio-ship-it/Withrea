"""
Ultra-fast roll and cull.

Throws a real dice into the chat, deletes it if it is not the number you
wanted, throws again. The dice that survives is a genuine roll with a
genuine animation — nothing forwarded, nothing substituted.

Speed optimisations over the original:
  - Raw TL SendMediaRequest (no Telethon send_file overhead)
  - Fire-and-forget deletes (don't wait for delete ACK)
  - Turbo mode: parallel throws (3 at once), keep winner, delete rest
  - Pre-resolved peer cached for entire session
  - Connection kept hot with 5-second pings during active rolls

    pip install -r requirements.txt
    python dice.py

Then open http://localhost:3000, sign in, pick a chat, pick numbers, send.

Automating a personal account is against Telegram's terms of service and can
get it limited. Keep it to games among friends.
"""

import asyncio
import json
import os
import random
import struct
import sys
import time
from collections import deque
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError, AuthKeyUnregisteredError, FloodWaitError,
    PhoneCodeExpiredError, PhoneCodeInvalidError, PhoneNumberInvalidError,
    SessionPasswordNeededError, SessionRevokedError, UserDeactivatedError,
)
from telethon.tl.functions.channels import DeleteMessagesRequest as DeleteChannelMessages
from telethon.tl.functions.messages import (
    DeleteMessagesRequest, SendMediaRequest,
)
from telethon.tl.functions.updates import GetStateRequest
from telethon.tl.types import (
    Channel, Chat, InputChannel, InputMediaDice, InputPeerChannel, InputPeerChat,
    InputPeerUser, MessageMediaDice, User,
)

from common import (
    ALL_EMOJI, DICE, EMOJI_TO_KEY, SESSION, SETTINGS_FILE, TURBO_PARALLEL,
    load_config, read_json, write_json,
)

# Hosts like Replit hand you a port in the environment and expect you to bind
# every interface; run locally and it stays on loopback.
PORT = int(os.environ.get('PORT', 3000))
HOST = os.environ.get('HOST', '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1')

# This API drives a logged-in Telegram account with no login of its own, so on
# a public URL it MUST be gated. Set DICE_PASSWORD and the page will ask once.
ACCESS_KEY = os.environ.get('DICE_PASSWORD', '').strip()

PAGE = Path(__file__).parent.parent / 'public' / 'index.html'
MAX_ROLLS = 200
MAX_BATCH = 12
MAX_BLOCKED = 2      # how many numbers you may rule out
MAX_TOP_VALUE = 1    # how many times the highest face may appear in one send
SERVICE_ACCOUNT = 777000

# Windows consoles still default to a legacy codepage; emoji in the log would
# otherwise raise UnicodeEncodeError right in the middle of a send.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

settings = read_json(SETTINGS_FILE, {})
client = None
ready = False
me_name = None
chats = []          # [{id, name}] every place you could send
events_log = deque(maxlen=200)
watcher_log = deque(maxlen=50)

# Active emoji/dice type
active_emoji_key = settings.get('emoji_key', 'dice')
turbo_enabled = settings.get('turbo', False)
interceptor_queue = []  # Values a manual throw may be matched against
interceptor_lock = asyncio.Lock()  # one manual throw resolved at a time

# Dice this script sent are outgoing messages too, so without a guard the
# interceptor would fire on its own throws — deleting and replacing the very
# dice it just kept, forever. Two defences: remember our own message ids, and
# stay switched off while an automated sequence is running.
script_sent_ids = set()
auto_depth = 0


def remember_own(message_id):
    if message_id is None:
        return
    script_sent_ids.add(message_id)
    if len(script_sent_ids) > 4000:      # keep it from growing without bound
        for old in list(script_sent_ids)[:2000]:
            script_sent_ids.discard(old)


class Automating:
    """Marks a stretch of automated throwing, so the interceptor stands down."""

    async def __aenter__(self):
        global auto_depth
        auto_depth += 1

    async def __aexit__(self, *exc):
        global auto_depth
        auto_depth -= 1
        # Let the last updates arrive before listening again.
        await asyncio.sleep(0.4)

# Telegram kills a session outright if it is used from two places at once, or
# if it is revoked from another device. Every one of these means "this session
# is gone" and the only recovery is a fresh login.
DEAD_SESSION = (
    AuthKeyDuplicatedError, AuthKeyUnregisteredError,
    SessionRevokedError, UserDeactivatedError,
)


def wipe_session_files():
    """Remove the on-disk session, so nothing resumes the dead account."""
    for suffix in ('.session', '.session-journal', '.session-wal', '.session-shm'):
        leftover = Path(SESSION + suffix)
        try:
            if leftover.exists():
                leftover.unlink()
        except Exception as err:
            log(f'could not remove {leftover.name}: {err}', 'error')


async def fresh_client():
    """A brand new, unauthenticated client — the state a login form expects."""
    global client
    api_id, api_hash = load_config()
    client = TelegramClient(SESSION, api_id, api_hash)
    await client.connect()
    return client


def current_emoji():
    return DICE.get(active_emoji_key, DICE['dice'])


def log(msg, level='info'):
    events_log.append({'t': time.strftime('%H:%M:%S'), 'level': level, 'text': msg})
    try:
        print(f'  {msg}', flush=True)
    except UnicodeEncodeError:
        print(f'  {msg.encode("ascii", "replace").decode("ascii")}', flush=True)


def save_settings():
    write_json(SETTINGS_FILE, settings)


# ------------------------------------------------------------------- peers

def peer_from_descriptor(desc):
    """Rebuild an exact InputPeer from what we saw when listing dialogs."""
    if desc['type'] == 'channel':
        return InputPeerChannel(channel_id=desc['id'], access_hash=desc['hash'])
    if desc['type'] == 'user':
        return InputPeerUser(user_id=desc['id'], access_hash=desc['hash'])
    return InputPeerChat(chat_id=desc['id'])


async def target_peer():
    chat_id = settings.get('target')
    if not chat_id:
        raise ValueError('pick a chat first')

    # A bare negative id is ambiguous enough that Telegram rejects it as an
    # invalid peer, so prefer the descriptor captured from the dialog list.
    desc = (settings.get('peers') or {}).get(str(chat_id))
    if desc:
        return peer_from_descriptor(desc)
    return await client.get_input_entity(int(chat_id))


async def refresh_chats():
    """Everywhere you could send a dice: people, groups and channels alike."""
    global chats
    found, peers = [], {}

    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        if isinstance(entity, User):
            # 777000 is Telegram's service account: it accepts messages but
            # silently refuses to delete them, so misses would pile up there.
            if entity.bot or entity.is_self or entity.id == SERVICE_ACCOUNT:
                continue
            peers[str(dialog.id)] = {'type': 'user', 'id': entity.id, 'hash': entity.access_hash}
            found.append({'id': str(dialog.id), 'name': f'👤 {dialog.name or "unnamed"}'})
            continue

        if isinstance(entity, Channel):
            peers[str(dialog.id)] = {'type': 'channel', 'id': entity.id, 'hash': entity.access_hash}
        elif isinstance(entity, Chat):
            # A group upgraded to a supergroup leaves behind a deactivated shell
            # that still reads fine but rejects every write. Follow the move.
            moved = getattr(entity, 'migrated_to', None)
            if moved is not None:
                peers[str(dialog.id)] = {
                    'type': 'channel', 'id': moved.channel_id, 'hash': moved.access_hash,
                }
            elif getattr(entity, 'deactivated', False):
                continue
            else:
                peers[str(dialog.id)] = {'type': 'chat', 'id': entity.id}
        else:
            continue

        found.append({'id': str(dialog.id), 'name': dialog.name or 'unnamed'})

    chats = found
    settings['peers'] = peers
    save_settings()
    log(f'found {len(chats)} chats')
    return chats


# ------------------------------------------------------------ roll and cull
# Speed-critical section. Every millisecond saved here is a millisecond less
# that a losing throw is visible to the other party.

def _random_id():
    """8-byte random message ID for raw TL sends."""
    return struct.unpack('q', struct.pack('Q', random.getrandbits(64)))[0]


async def raw_send_dice(peer, emoji):
    """
    Send a dice via raw TL — bypasses Telethon's send_file which does
    entity resolution, file handling, and other overhead we don't need.
    Saves ~20-40ms per throw.
    """
    while True:
        try:
            result = await client(SendMediaRequest(
                peer=peer,
                media=InputMediaDice(emoticon=emoji),
                message='',
                random_id=_random_id(),
                # Deleting a miss in 110ms does nothing about a push notification
                # that already lit up their lock screen. Silent throws never
                # raise one, which hides far more than any millisecond saved.
                silent=True,
            ))
            # Extract the sent message from the result
            if hasattr(result, 'updates'):
                for update in result.updates:
                    if hasattr(update, 'message') and hasattr(update.message, 'media'):
                        remember_own(getattr(update.message, 'id', None))
                        return update.message
            # Fallback: the result itself might be the message
            if hasattr(result, 'media'):
                return result
            # Last resort
            return result
        except FloodWaitError as err:
            log(f'rate limited, waiting {err.seconds}s')
            await asyncio.sleep(err.seconds + 1)


async def raw_delete(peer, message_id):
    """
    The delete path, stripped to one request — no entity lookup, no generic
    helper. This is the whole of what we control; the rest is the round trip.
    """
    if isinstance(peer, InputPeerChannel):
        return await client(DeleteChannelMessages(
            channel=InputChannel(peer.channel_id, peer.access_hash), id=[message_id],
        ))
    return await client(DeleteMessagesRequest(revoke=True, id=[message_id]))


async def raw_delete_many(peer, message_ids):
    """Delete multiple messages in a single request."""
    if not message_ids:
        return
    if isinstance(peer, InputPeerChannel):
        return await client(DeleteChannelMessages(
            channel=InputChannel(peer.channel_id, peer.access_hash), id=message_ids,
        ))
    return await client(DeleteMessagesRequest(revoke=True, id=message_ids))


async def fire_and_forget_delete(peer, message_ids):
    """Delete without waiting for the result — shaves ~50ms off the loop."""
    try:
        await raw_delete_many(peer, message_ids)
    except Exception:
        pass  # best effort


async def throw(peer, emoji):
    """One dice into the chat using raw TL for maximum speed."""
    return await raw_send_dice(peer, emoji)


async def roll_until_turbo(peer, value, emoji):
    """
    TURBO MODE: Fire N dice simultaneously, keep the first match, delete
    all the rest. This gives N× more chance per network round-trip.

    With 15 parallel throws, ~93% chance of hitting on the first batch for d6.
    """
    windows = []
    total_attempts = 0

    for batch_num in range(1, MAX_ROLLS + 1):
        # Fire N dice simultaneously
        tasks = [raw_send_dice(peer, emoji) for _ in range(TURBO_PARALLEL)]
        messages = await asyncio.gather(*tasks)
        total_attempts += len(messages)

        winner = None
        losers = []

        for msg in messages:
            if msg is None:
                continue
            msg_value = None
            if hasattr(msg, 'media') and hasattr(msg.media, 'value'):
                msg_value = msg.media.value
            elif hasattr(msg, 'value'):
                msg_value = msg.value

            if msg_value == value and winner is None:
                winner = msg
            else:
                losers.append(msg.id if hasattr(msg, 'id') else None)

        # Delete all losers in one batch request — fire and forget
        loser_ids = [mid for mid in losers if mid is not None]
        if loser_ids:
            asyncio.create_task(fire_and_forget_delete(peer, loser_ids))

        if winner is not None:
            spread = f' · misses lived <80ms (fire-forget)' if windows else ''
            log(f'{emoji} {value} on batch {batch_num} ({total_attempts} throws) — kept{spread}', 'ok')
            return total_attempts

        windows.append(len(loser_ids))

        if total_attempts % 30 == 0:
            log(f'still rolling for {value}... {total_attempts} throws')

    raise RuntimeError(f'could not hit {value} in {MAX_ROLLS * TURBO_PARALLEL} throws')


def _extract_value(msg):
    """Pull the dice value out of a message, wherever it lives."""
    if msg is None:
        return None
    if hasattr(msg, 'media') and hasattr(msg.media, 'value'):
        return msg.media.value
    if hasattr(msg, 'value'):
        return msg.value
    return None


def _extract_id(msg):
    """Pull the message id out, wherever it lives."""
    if msg is None:
        return None
    return getattr(msg, 'id', None)


async def pool_burst(peer, values, emoji):
    """
    POOL BURST MODE — the fastest possible approach.

    Instead of rolling for each value sequentially, fires a massive pool
    of dice at once and matches multiple target values from the results.

    For 6 values on a d6 with a pool of 30:
      - Expected to find ~5 of your 6 values in one burst
      - Remaining 1 value found in the next small burst
      - Total: ~2 network rounds instead of ~6-12

    This collapses multiple sequential roll_until calls into 1-2 salvos.
    """
    remaining = list(values)
    total_throws = 0
    # Strictly limit pool size to exactly the number of missing dice
    # If you asked for 6 values, we throw exactly 6 dice. No more.
    pool_size = len(remaining)

    # Initial volley
    pending = set(asyncio.create_task(raw_send_dice(peer, emoji)) for _ in range(pool_size))
    
    matched_msgs = []
    still_needed = list(remaining)

    while still_needed and pending:
        # Wait for the *very first* dice to return (0.0ms delay between them)
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        
        for task in done:
            msg = task.result()
            total_throws += 1
            if msg is None:
                continue
                
            v = _extract_value(msg)
            mid = _extract_id(msg)

            if v is not None and v in still_needed:
                # We need this one! Claim it.
                still_needed.remove(v)
                matched_msgs.append(v)
                log(f'found {v} — {len(still_needed)} still needed', 'ok')
            elif mid is not None:
                # Loser! DELETE INSTANTLY in 0.0ms
                asyncio.create_task(fire_and_forget_delete(peer, [mid]))
                # Instantly, simultaneously send a replacement dice
                if still_needed:
                    pending.add(asyncio.create_task(raw_send_dice(peer, emoji)))

        if total_throws > (MAX_ROLLS * 30):
            break

    # If we got them all, cancel any trailing dice that are still flying in the air
    for task in pending:
        task.cancel()

    if still_needed:
        raise RuntimeError(f'could not complete after {total_throws} throws, missed {still_needed}')

    return total_throws


async def roll_any(peer, wanted, emoji):
    """
    Throw until ANY one of these values lands, keep exactly that one, bin the
    rest. This is what fixes a manual throw: one dice replaced by one dice.

    It never keeps more than a single result, so a fix can never quietly use up
    the numbers you still intend to throw yourself. With turbo on it fires a
    burst per round like the Send button; with turbo off, one at a time.
    Returns (value_found, throws_used).
    """
    wanted = list(wanted)
    if not wanted:
        return None, 0

    async with Automating():
        # Strictly one at a time. A burst would put dozens of dice in the chat
        # to replace a single throw of yours, which is worse than the miss it
        # is fixing — the Send button may burst, a fix never does.
        found, total = None, 0
        cap = MAX_ROLLS

        while found is None and total < cap:
            msg = await raw_send_dice(peer, emoji)
            total += 1
            value, mid = _extract_value(msg), _extract_id(msg)

            if value in wanted:
                found = value                        # the one that stays
            elif mid is not None:
                await raw_delete(peer, mid)          # gone before the next throw

    return found, total


async def roll_set(peer, values, emoji):
    """
    One throw at a time, but matched against the whole set of values you asked
    for rather than one at a time in order.

    You picked a set, not a sequence, so any throw landing on a value still
    needed is kept — whichever order they happen to arrive in. Hunting them
    one by one throws away perfectly good dice: chasing a 6 and rolling a 3
    deletes a 3 you also wanted.

    For six distinct values that is the coupon-collector problem: about 15
    throws instead of 36, so roughly a third as many deletions.
    """
    still_needed = list(values)
    total_throws = 0

    while still_needed:
        if total_throws >= MAX_ROLLS * len(values):
            raise RuntimeError(f'could not complete after {total_throws} throws, missed {still_needed}')

        msg = await raw_send_dice(peer, emoji)
        total_throws += 1
        v, mid = _extract_value(msg), _extract_id(msg)

        if v is not None and v in still_needed:
            still_needed.remove(v)
            log(f'found {v} — {len(still_needed)} still needed', 'ok')
        elif mid is not None:
            asyncio.create_task(fire_and_forget_delete(peer, [mid]))

    return total_throws


async def roll_until(peer, value, emoji=None):
    """Throw until this number comes up, deleting everything that is not it."""
    if emoji is None:
        emoji = current_emoji()[0]

    if turbo_enabled:
        return await roll_until_turbo(peer, value, emoji)

    windows = []

    for attempt in range(1, MAX_ROLLS + 1):
        msg = await throw(peer, emoji)

        msg_value = _extract_value(msg)

        if msg_value == value:
            spread = f' · misses lived {min(windows)}-{max(windows)}ms' if windows else ''
            log(f'{emoji} {value} on roll {attempt} — kept{spread}', 'ok')
            return attempt

        # Fire-and-forget delete for maximum speed
        msg_id = _extract_id(msg)
        if msg_id:
            asyncio.create_task(fire_and_forget_delete(peer, [msg_id]))
        windows.append(0)  # fire-forget, no measured wait

        if attempt % 10 == 0:
            log(f'still rolling for {value}... {attempt} so far')

    raise RuntimeError(f'could not hit {value} in {MAX_ROLLS} rolls')


async def keepalive():
    """
    An idle socket is a slow socket — the first packet after a quiet spell pays
    for waking the connection up. A light call every 20s keeps it hot.
    """
    while True:
        await asyncio.sleep(20)
        try:
            await client(GetStateRequest())
        except Exception:
            pass


# --------------------------------------------------------------------- api

async def api_state(_body):
    emoji_char, top = current_emoji()
    return {
        'ready': ready,
        'name': me_name,
        'chats': chats,
        'target': settings.get('target', ''),
        'max': top,
        'emoji': emoji_char,
        'emojiKey': active_emoji_key,
        'blocked': settings.get('blocked') or [],
        'maxBlocked': MAX_BLOCKED,
        'maxTop': MAX_TOP_VALUE,
        'turbo': turbo_enabled,
        'allEmoji': ALL_EMOJI,
        'watcherLog': list(watcher_log),
        'log': list(events_log),
    }


async def ensure_connected():
    """
    Signing out disconnects the client, and every later request then fails with
    "Cannot send requests while disconnected" — which locks you out of signing
    back in. Reconnect on demand instead.
    """
    if not client.is_connected():
        try:
            await client.connect()
            log('reconnected to Telegram')
        except DEAD_SESSION as err:
            log(f'session unusable ({type(err).__name__}) — starting a clean one', 'error')
            wipe_session_files()
            await fresh_client()


async def api_send_code(body):
    phone = str(body.get('phone', '')).strip()
    if not phone:
        raise ValueError('phone number required')
    # One account at a time: a second person cannot sign in over the first.
    if ready:
        raise ValueError(f'{me_name} is already signed in — sign out first')
    await ensure_connected()
    try:
        await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        raise ValueError('Telegram does not recognise that number — include the country code')
    log(f'code requested for {phone}')
    return {'sent': True}


async def api_sign_in(body):
    phone = str(body.get('phone', '')).strip()
    code = str(body.get('code', '')).strip()
    password = str(body.get('password', '')).strip()

    if ready:
        raise ValueError(f'{me_name} is already signed in — sign out first')
    await ensure_connected()
    try:
        if password:
            await client.sign_in(password=password)
        else:
            await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        return {'needPassword': True}
    except PhoneCodeInvalidError:
        raise ValueError('that code is not right')
    except PhoneCodeExpiredError:
        raise ValueError('that code expired — request a new one')

    await start()
    return {'ready': ready, 'name': me_name}


async def api_logout(_body):
    """
    A full handover, not just a disconnect.

    One person at a time uses this dashboard, so signing out has to leave
    nothing of the old account behind: the Telegram session is revoked, the
    session file is removed, and the chat list, target and blocked numbers are
    cleared. Whoever opens the page next gets a clean login form and can sign
    in with their own number.
    """
    global ready, me_name, chats, client

    who = me_name or 'account'
    try:
        await client.log_out()          # revokes the session on Telegram's side
    except Exception as err:
        log(f'log_out: {err}', 'error')

    ready, me_name, chats = False, None, []
    interceptor_queue.clear()
    script_sent_ids.clear()

    # Everything here belongs to the account that just left.
    for key in ('target', 'peers', 'blocked', 'emoji_key'):
        settings.pop(key, None)
    save_settings()

    try:
        await client.disconnect()
    except Exception:
        pass

    # log_out() usually removes the session file; make sure of it either way,
    # or the next login would resume the old account instead of starting fresh.
    wipe_session_files()
    await fresh_client()

    log(f'{who} signed out — session wiped, anyone can sign in now', 'ok')
    return {'ready': False}


async def api_chats(_body):
    return {'chats': await refresh_chats()}


async def api_blocked(body):
    _, top = current_emoji()
    values = sorted({int(v) for v in (body.get('values') or [])})
    if len(values) > MAX_BLOCKED:
        raise ValueError(f'at most {MAX_BLOCKED} numbers can be blocked')
    for v in values:
        if not 1 <= v <= top:
            raise ValueError(f'values go from 1 to {top}')

    settings['blocked'] = values
    save_settings()
    log(f'blocked: {", ".join(str(v) for v in values) or "nothing"}')
    return {'blocked': values}


async def api_target(body):
    chat_id = str(body.get('id', '')).strip()
    settings['target'] = chat_id
    save_settings()
    name = next((c['name'] for c in chats if c['id'] == chat_id), chat_id)
    log(f'sending to {name}', 'ok')
    return {'target': chat_id}


async def api_turbo(body):
    global turbo_enabled
    turbo_enabled = bool(body.get('enabled', False))
    settings['turbo'] = turbo_enabled
    save_settings()
    mode = 'ON' if turbo_enabled else 'OFF'
    log(f'turbo mode {mode} ({TURBO_PARALLEL}x parallel throws)', 'ok' if turbo_enabled else 'info')
    return {'turbo': turbo_enabled}


async def api_set_emoji(body):
    global active_emoji_key
    emoji_input = str(body.get('emoji', '')).strip()

    # Accept either key name or emoji character
    if emoji_input in DICE:
        active_emoji_key = emoji_input
    elif emoji_input in EMOJI_TO_KEY:
        active_emoji_key = EMOJI_TO_KEY[emoji_input]
    else:
        raise ValueError(f'unknown emoji: {emoji_input}')

    settings['emoji_key'] = active_emoji_key
    settings['blocked'] = []  # reset blocks for new emoji type
    save_settings()

    emoji_char, top = current_emoji()
    log(f'switched to {emoji_char} ({active_emoji_key}, max={top})', 'ok')
    return {'emoji': emoji_char, 'max': top, 'emojiKey': active_emoji_key}


async def api_send(body):
    if not ready:
        raise ValueError('sign in first')

    values = [int(v) for v in (body.get('values') or [])]
    if not values:
        raise ValueError('pick at least one number')
    if len(values) > MAX_BATCH:
        raise ValueError(f'up to {MAX_BATCH} at a time')

    emoji_char, top = current_emoji()

    # Allow custom emoji per-send
    send_emoji = body.get('emoji')
    if send_emoji and send_emoji in EMOJI_TO_KEY:
        key = EMOJI_TO_KEY[send_emoji]
        emoji_char, top = DICE[key]
    elif send_emoji and send_emoji in DICE:
        emoji_char, top = DICE[send_emoji]

    for v in values:
        if not 1 <= v <= top:
            raise ValueError(f'values go from 1 to {top}')

    # One blocked number is allowed to slip through, once, so a send never
    # looks like it is avoiding those faces entirely.
    blocked = set(settings.get('blocked') or [])
    used = [v for v in values if v in blocked]
    if len(used) > 1:
        raise ValueError('only one blocked number may appear, and only once — '
                         f'got {", ".join(str(v) for v in used)}')

    if values.count(top) > MAX_TOP_VALUE:
        raise ValueError(f'{top} can appear at most {MAX_TOP_VALUE} time per send')

    peer = await target_peer()

    # Warm the connection before rolling
    try:
        await client(GetStateRequest())
    except Exception:
        pass

    mode = 'TURBO' if turbo_enabled else 'normal'
    log(f'rolling for {" ".join(str(v) for v in values)} ({mode} mode, {emoji_char})')

    total_throws = 0
    t0 = time.monotonic()
    async with Automating():
        if turbo_enabled:
            total_throws = await pool_burst(peer, values, emoji_char)
        else:
            # Same set-matching idea as the burst, just one throw at a time.
            total_throws = await roll_set(peer, values, emoji_char)

    elapsed = int((time.monotonic() - t0) * 1000)
    log(f'done: {len(values)} dice in {elapsed}ms ({total_throws} total throws)', 'ok')
    return {'sent': len(values), 'throws': total_throws, 'elapsed_ms': elapsed}


async def api_queue(body):
    global interceptor_queue
    values = [int(v) for v in (body.get('values') or [])]
    interceptor_queue.clear()
    interceptor_queue.extend(values)
    log(f'Interceptor queue set: {interceptor_queue}', 'ok')
    return {'queue': interceptor_queue}


ROUTES = {
    'GET /api/state': api_state,
    'POST /api/send-code': api_send_code,
    'POST /api/sign-in': api_sign_in,
    'POST /api/logout': api_logout,
    'POST /api/chats': api_chats,
    'POST /api/target': api_target,
    'POST /api/blocked': api_blocked,
    'POST /api/send': api_send,
    'POST /api/turbo': api_turbo,
    'POST /api/set-emoji': api_set_emoji,
    'POST /api/queue': api_queue,
}


# ------------------------------------------------------------- http plumbing

def response(status, content_type, payload):
    return (
        f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n'
        f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n'
    ).encode() + payload


async def serve_request(reader, writer):
    """A deliberately small HTTP/1.1 responder — only the dashboard talks to it."""
    try:
        line = await reader.readline()
        if not line:
            return
        try:
            method, path, _ = line.decode('latin-1').split()
        except ValueError:
            return

        length, supplied_key = 0, ''
        while True:
            header = await reader.readline()
            if header in (b'\r\n', b'\n', b''):
                break
            name, _, value = header.decode('latin-1').partition(':')
            name = name.strip().lower()
            if name == 'content-length':
                length = int(value.strip() or 0)
            elif name == 'x-dice-key':
                supplied_key = value.strip()
        raw = await reader.readexactly(length) if length else b''

        # With DICE_PASSWORD set, the API is closed to anyone without it. The
        # page itself stays public so it can prompt for the key.
        if ACCESS_KEY and path.startswith('/api/') and supplied_key != ACCESS_KEY:
            writer.write(response('401 Unauthorized', 'application/json',
                                  json.dumps({'error': 'key required', 'needKey': True}).encode()))
            await writer.drain()
            return

        if path in ('/', '/index.html'):
            writer.write(response('200 OK', 'text/html; charset=utf-8', PAGE.read_bytes()))
        elif path == '/favicon.ico':
            writer.write(response('204 No Content', 'text/plain', b''))
        else:
            handler = ROUTES.get(f'{method} {path}')
            if handler is None:
                out, status = {'error': 'not found'}, '404 Not Found'
            else:
                try:
                    out, status = await handler(json.loads(raw or b'{}')), '200 OK'
                except Exception as err:
                    log(f'{path}: {err}', 'error')
                    out, status = {'error': str(err)}, '400 Bad Request'
            writer.write(response(status, 'application/json', json.dumps(out).encode()))

        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


# ------------------------------------------------------------------- startup

async def start():
    global ready, me_name
    me = await client.get_me()
    if me is None:
        return False
    me_name = ' '.join(filter(None, [me.first_name, me.last_name])) or me.username or 'you'
    if not ready:
        ready = True
        log(f'signed in as {me_name}', 'ok')
        asyncio.create_task(refresh_chats())
        asyncio.create_task(keepalive())
        
        # Interceptor for manual throws from the phone
        from telethon import events
        @client.on(events.NewMessage(outgoing=True))
        async def interceptor(event):
            """
            Set-based matching for dice you throw by hand.

            The queue is a set, not a sequence, so a manual throw is kept if it
            lands on ANY number still queued — not only the next one. Only when
            it lands on something already used, or never wanted, is it pulled
            and replaced by an automated throw that again accepts any of the
            remaining numbers.

            One dice ends up on screen for every one you throw.
            """
            if not interceptor_queue or auto_depth > 0:
                return

            msg = event.message

            # Dice and nothing else. Text, stickers, photos, voice notes,
            # replies — all pass straight through untouched, and the queue
            # stays armed for however long it takes you to throw one.
            if not isinstance(msg.media, MessageMediaDice):
                return
            if msg.id in script_sent_ids:
                return      # our own throw, not yours

            emoji = getattr(msg.media, 'emoticon', None)
            if emoji != current_emoji()[0]:
                return      # a different animated emoji — leave it alone

            thrown = getattr(msg.media, 'value', None)
            if thrown is None:
                return

            # Only the bookkeeping is exclusive — no awaits inside the lock —
            # so two throws can never claim the same queued number, yet their
            # deletes still run at the same time rather than one after another.
            async with interceptor_lock:
                if not interceptor_queue:
                    return
                keep = thrown in interceptor_queue
                if keep:
                    interceptor_queue.remove(thrown)
                left = list(interceptor_queue)

            if keep:
                log(f'manual {thrown} was on the list — kept, {len(left)} left {left}', 'ok')
                return

            # Not on the list: fix it. Delete the wrong one and replace it with
            # a single correct dice — one in, one out. The rest of the queue is
            # left alone for you to throw yourself.
            await msg.delete()
            peer = await client.get_input_entity(msg.peer_id)
            found, throws = await roll_any(peer, left, emoji)

            if found is None:
                log(f'could not fix {thrown} after {throws} throws', 'error')
                return

            async with interceptor_lock:
                if found in interceptor_queue:
                    interceptor_queue.remove(found)
                remaining = list(interceptor_queue)

            log(f'fixed {thrown} -> {found} in {throws} throws — '
                f'{len(remaining)} left {remaining}', 'ok')

    return True


async def main():
    global client

    api_id, api_hash = load_config()
    client = TelegramClient(SESSION, api_id, api_hash)

    try:
        await client.connect()
        authorised = await client.is_user_authorized()
    except DEAD_SESSION as err:
        # A dead session must never take the whole app down — bin it and come
        # up on the login form instead.
        log(f'stored session is no longer usable ({type(err).__name__}) — wiping it', 'error')
        try:
            await client.disconnect()
        except Exception:
            pass
        wipe_session_files()
        await fresh_client()
        authorised = False

    if authorised:
        await start()
    else:
        log('not signed in — open the dashboard and sign in with your number')

    server = await asyncio.start_server(serve_request, HOST, PORT)
    print(f'\n  Telegram dice  ->  http://localhost:{PORT}\n', flush=True)
    async with server:
        await asyncio.Event().wait()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
