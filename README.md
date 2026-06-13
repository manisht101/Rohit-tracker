# 🏏 Rohit Sharma Cricket Tracker

A lightweight Python bot that watches live Cricbuzz scorecard data and sends
instant Telegram alerts **only** when Rohit Sharma is on strike or hits a boundary.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your .env file
cp .env.example .env
# → edit .env and fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

# 3. Run
python rohit_tracker.py
```

---

## Getting your Telegram credentials

| Variable | How to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message [@userinfobot](https://t.me/userinfobot) → copy the `Id` field |

---

## State Machine

```
┌──────────────────────────────────────────────────────────────────┐
│  State 1 – SCHEDULE (24 h poll)                                  │
│  • Fetch today's fixtures                                        │
│  • If India/MI not playing → sleep 24 h                         │
│  • If match found → wait until start time → State 2             │
└──────────────────────┬───────────────────────────────────────────┘
                       │ match starts
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  State 2 – INNINGS (15 min poll)                                 │
│  • Check which team is batting                                   │
│  • If target team bowling/break → sleep 15 min                  │
│  • If target team batting → State 3                             │
│  • If match over → State 1                                      │
└──────────────────────┬───────────────────────────────────────────┘
                       │ team batting
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  State 3 – PRESENCE (60 s poll)                                  │
│  • Scan active batsmen for "Rohit Sharma"                        │
│  • Not there → sleep 60 s                                       │
│  • Found → reset state cache → send "at crease" alert → State 4 │
│  • Team no longer batting → State 2                             │
└──────────────────────┬───────────────────────────────────────────┘
                       │ Rohit at crease
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  State 4 – TRACKING (5–10 s poll)                                │
│  • is_on_strike(t) == True AND was False(t-1) → ⚡ strike alert  │
│  • fours(t) > fours(t-1)  → 🔴 FOUR alert                      │
│  • sixes(t) > sixes(t-1)  → 💥 SIX alert                       │
│  • Rohit absent (dismissed) → ❌ out alert → State 2            │
└──────────────────────────────────────────────────────────────────┘
```

---

## Swapping the data source

The entire HTTP layer lives in `CricbuzzFetcher._raw_get()`.
To switch to RapidAPI or another provider:

1. Set `RAPIDAPI_KEY` in `.env`
2. Edit `CricbuzzFetcher._BASE` and `CricbuzzFetcher._HEADERS`
3. Adjust the JSON path parsing in `fetch_schedule()` / `fetch_match_data()`
   to match your provider's response shape.

No other file needs to change.

---

## Notification examples

| Trigger | Message |
|---|---|
| Rohit walks in | `🏏 Rohit Sharma is at the crease! Runs: 0 (0 balls) \| 4s: 0 \| 6s: 0` |
| Comes on strike | `🎯 Rohit Sharma is on strike! 12 (8) \| 4s: 2 \| 6s: 0` |
| Hits a four | `🔴 FOUR! Rohit Sharma hits a four! 28 (18) \| 4s: 3 \| 6s: 1` |
| Hits a six | `💥 SIX! Rohit Sharma hits a six! 34 (21) \| 4s: 3 \| 6s: 2` |
| Out | `❌ Rohit Sharma is OUT! Score: 47 (31 balls) \| 4s: 4 \| 6s: 3` |

---

## Resilience features

- Retries with exponential back-off (3 attempts, 5 s / 10 s / 15 s)
- All exceptions caught per-state; loop never crashes
- Dual-channel logging: console + `rohit_tracker.log`
- Graceful shutdown on `Ctrl-C` with Telegram notification
