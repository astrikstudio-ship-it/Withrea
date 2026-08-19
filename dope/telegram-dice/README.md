# 🎲 Telegram Dice

Throws a real dice into a chat, deletes it if it is not your number, throws again.
The dice that survives is a genuine roll with a genuine animation.

One Python process, no bot, no Node.

## Setup

```bash
pip install -r E:\Utkarsh\Claude-code\telegram-dice\userbot\requirements.txt
```

1. Get `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org) → API development
   tools. Copy `userbot/.env.example` to `userbot/.env` and paste them in.
2. Start it:

```bash
python E:\Utkarsh\Claude-code\telegram-dice\userbot\dice.py
```

3. Open http://localhost:3000 → sign in (phone → code → two-step password if you use one).
4. Pick a chat, tap your numbers, **Send**.

The session is saved to `userbot/dice-session.session` and reused, so the login is one-time.
Keep that file private — it is a logged-in copy of your account.

## How it works

Telegram decides a dice value on its servers. `InputMediaDice` takes one field, `emoticon` —
there is no value to set. The number comes back only in `MessageMediaDice.value`, after the
message exists. So the only lever is which rolls are allowed to stay.

This process throws the dice rather than you, on purpose. When you tap the dice yourself, the
value reaches us in an update Telegram pushes afterwards — measured around 700ms on this
account. Sending it here puts the value in the send reply instead, leaving only one delete
round trip, around 100–150ms.

Each number takes about six throws on average. The log reports how long each losing throw
existed:

```
🎲 6 on roll 4 — kept · misses lived 98-140ms
```

Three things keep that number down: the peer is resolved once and cached, the delete is a
single raw request rather than Telethon's generic helper, and a light call every 20 seconds
keeps the socket warm so the first packet after a pause is not slow.

It is not zero and cannot be. A losing throw exists on Telegram's servers for one round trip
before it is pulled.

## What was tried and ruled out

| Idea | Result |
|---|---|
| Ask Telegram for a specific value | `InputMediaDice(emoticon)` — no value field exists |
| Pre-draw privately with a scheduled message | Scheduled dice carry value **0** until delivery: `scheduled 0, arrived 1` |
| Roll privately and forward the winner in | Works, but `drop_author=True` is ignored — `fwd_from` present, tested against a real group |
| Send the official face animation instead | Works, but arrives as a sticker, not a dice message |
| A sandbox outside Telegram | A dice value only exists inside a Telegram message; nothing local can mint one |

## Files

| Path | What it is |
|---|---|
| `userbot/dice.py` | the whole app — Telegram client, web server, API |
| `userbot/common.py` | paths, config loading, dice table |
| `userbot/login.py` | optional terminal login, if you prefer it to the web form |
| `public/index.html` | the dashboard |
| `userbot/.env` | your api_id / api_hash |
| `userbot/settings.json` | chosen chat and the cached peer list |

## Worth knowing

Automating a personal account is against Telegram's terms of service and can get it limited.
Keep this to games among friends — not anywhere money is riding on the roll.
