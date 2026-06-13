"""
rohit_tracker.py  (v3 – adds two-way Telegram commands)
=========================================================
New in v3:
  • TelegramListener runs in a background thread
  • You can message the bot:
      /next     – When is Rohit's/target team's next match?
      /status   – Current bot state + live score (if tracking)
      /help     – List available commands
  • Everything from v2 (RapidAPI endpoints, timezone fixes) retained
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

UTC = timezone.utc

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("rohit_tracker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("rohit_tracker")


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
    TELEGRAM_CHAT_ID: str   = os.environ["TELEGRAM_CHAT_ID"]
    RAPIDAPI_KEY: str       = os.getenv("RAPIDAPI_KEY", "")

    TARGET_PLAYER: str  = os.getenv("TARGET_PLAYER", "Rohit Sharma")
    TARGET_TEAMS: tuple = tuple(
        t.strip()
        for t in os.getenv("TARGET_TEAMS", "India,Mumbai Indians").split(",")
    )

    # Poll intervals (seconds)
    POLL_SCHEDULE: int = 24 * 3600   # State 1
    POLL_INNINGS:  int = 15 * 60     # State 2
    POLL_PRESENCE: int = 60          # State 3
    POLL_TRACKING: int = 8           # State 4

    # HTTP
    REQUEST_TIMEOUT: int = 10
    MAX_RETRIES:     int = 3
    RETRY_BACKOFF:   int = 5


# ══════════════════════════════════════════════════════════════════════════════
# State definitions
# ══════════════════════════════════════════════════════════════════════════════

class BotState(Enum):
    SCHEDULE = auto()   # State 1
    INNINGS  = auto()   # State 2
    PRESENCE = auto()   # State 3
    TRACKING = auto()   # State 4


# ══════════════════════════════════════════════════════════════════════════════
# Player state cache
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlayerCache:
    is_on_strike: bool = False
    fours:        int  = 0
    sixes:        int  = 0
    runs:         int  = 0
    balls:        int  = 0
    dismissed:    bool = False

    def update(self, is_on_strike: bool, fours: int, sixes: int,
               runs: int, balls: int) -> None:
        self.is_on_strike = is_on_strike
        self.fours        = fours
        self.sixes        = sixes
        self.runs         = runs
        self.balls        = balls


@dataclass
class BotContext:
    state:        BotState            = BotState.SCHEDULE
    match_id:     str                 = ""
    match_start:  Optional[datetime]  = None
    match_desc:   str                 = ""
    player_cache: PlayerCache         = field(default_factory=PlayerCache)


# ══════════════════════════════════════════════════════════════════════════════
# Telegram notification layer (outgoing)
# ══════════════════════════════════════════════════════════════════════════════

class TelegramNotifier:
    _BASE = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str) -> None:
        self._url     = self._BASE.format(token=token)
        self._chat_id = chat_id

    def send(self, text: str) -> bool:
        payload = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }
        try:
            r = requests.post(self._url, json=payload,
                              timeout=Config.REQUEST_TIMEOUT)
            r.raise_for_status()
            log.info("Telegram ✓ sent: %s", text[:80])
            return True
        except requests.RequestException as exc:
            log.error("Telegram send failed (chat_id=%s): %s",
                      self._chat_id, exc)
            if hasattr(exc, "response") and exc.response is not None:
                log.error("Telegram response body: %s", exc.response.text)
            return False


# ══════════════════════════════════════════════════════════════════════════════
# Telegram command listener (incoming) – v3
# ══════════════════════════════════════════════════════════════════════════════

class TelegramListener:
    """
    Polls Telegram's getUpdates endpoint in a background thread and replies
    to a small set of commands. Only responds to messages from
    Config.TELEGRAM_CHAT_ID (your own chat) to avoid abuse.
    """

    def __init__(self, token: str, chat_id: str,
                 fetcher: "CricbuzzFetcher", ctx: BotContext) -> None:
        self._base    = f"https://api.telegram.org/bot{token}"
        self._chat_id = str(chat_id)
        self._fetcher = fetcher
        self._ctx     = ctx
        self._offset  = 0

    # ── Public ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._skip_backlog()
        t = threading.Thread(target=self._poll_loop, daemon=True,
                             name="TelegramListener")
        t.start()
        log.info("[Listener] Telegram command listener started.")

    # ── Internal ──────────────────────────────────────────────────────────

    def _skip_backlog(self) -> None:
        """Advance the offset past any old/pending messages on startup."""
        try:
            resp = requests.get(f"{self._base}/getUpdates",
                                params={"timeout": 0},
                                timeout=Config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            results = resp.json().get("result", [])
            if results:
                self._offset = results[-1]["update_id"] + 1
        except Exception as exc:
            log.warning("[Listener] Could not skip backlog: %s", exc)

    def _poll_loop(self) -> None:
        while True:
            try:
                resp = requests.get(
                    f"{self._base}/getUpdates",
                    params={"offset": self._offset, "timeout": 30},
                    timeout=35,
                )
                resp.raise_for_status()
                for update in resp.json().get("result", []):
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(3)
            except Exception as exc:
                log.warning("[Listener] poll error: %s", exc)
                time.sleep(5)

    def _handle_update(self, update: dict) -> None:
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        if chat_id != self._chat_id:
            log.info("[Listener] Ignoring message from unknown chat %s", chat_id)
            return

        cmd = text.lower()
        log.info("[Listener] Received command: %s", text)

        if cmd in ("/next", "/nextmatch", "next match", "when is the next match",
                   "when is rohit's next match", "when is rohit next match"):
            reply = self._next_match_reply()
        elif cmd in ("/status", "/score", "status", "score"):
            reply = self._status_reply()
        elif cmd in ("/help", "/start", "help"):
            reply = self._help_reply()
        else:
            reply = (
                "🤔 I didn't understand that.\n\n" + self._help_reply()
            )

        self._send(reply)

    def _send(self, text: str) -> None:
        try:
            requests.post(
                f"{self._base}/sendMessage",
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=Config.REQUEST_TIMEOUT,
            )
        except Exception as exc:
            log.error("[Listener] reply send failed: %s", exc)

    # ── Reply builders ───────────────────────────────────────────────────

    def _next_match_reply(self) -> str:
        # If we're already tracking/watching a match, show that first
        ctx = self._ctx
        if ctx.match_id and ctx.state != BotState.SCHEDULE:
            when = (ctx.match_start.strftime("%d %b %Y, %I:%M %p UTC")
                   if ctx.match_start else "in progress")
            return (f"🏏 Currently tracking:\n"
                   f"<b>{ctx.match_desc or 'Match'}</b>\n"
                   f"🕒 {when}")

        matches = self._fetcher.fetch_upcoming()
        for m in matches:
            team1 = m.get("team1", {}).get("teamName", "")
            team2 = m.get("team2", {}).get("teamName", "")
            if any(t.lower() in team1.lower() or t.lower() in team2.lower()
                   for t in Config.TARGET_TEAMS):
                start_ms = int(m.get("startDate", 0) or 0)
                start_time = (datetime.fromtimestamp(start_ms / 1000, UTC)
                              if start_ms else None)
                when = (start_time.strftime("%d %b %Y, %I:%M %p UTC")
                       if start_time else "TBD")
                desc   = m.get("matchDesc", "")
                series = m.get("seriesName", "")
                return (f"📅 <b>Next match for {'/'.join(Config.TARGET_TEAMS)}:</b>\n"
                       f"{team1} vs {team2}\n"
                       f"{desc} — {series}\n"
                       f"🕒 {when}")

        return (f"I couldn't find any upcoming scheduled match for "
               f"{'/'.join(Config.TARGET_TEAMS)} right now. "
               f"Try again closer to the series dates.")

    def _status_reply(self) -> str:
        ctx = self._ctx
        state_names = {
            BotState.SCHEDULE: "💤 Waiting for next match (State 1)",
            BotState.INNINGS:  "👀 Watching innings status (State 2)",
            BotState.PRESENCE: f"⏳ Waiting for {Config.TARGET_PLAYER} to bat (State 3)",
            BotState.TRACKING: f"🎯 Tracking {Config.TARGET_PLAYER} ball-by-ball (State 4)",
        }
        lines = [state_names.get(ctx.state, "Unknown state")]

        if ctx.match_desc:
            lines.append(f"\n🏟️ Match: {ctx.match_desc}")

        if ctx.state in (BotState.PRESENCE, BotState.TRACKING):
            pc = ctx.player_cache
            strike_tag = "🎯 On strike" if pc.is_on_strike else "⏳ Non-striker"
            lines.append(
                f"\n🏏 {Config.TARGET_PLAYER}: {pc.runs} ({pc.balls} balls)"
                f"\n4s: {pc.fours} | 6s: {pc.sixes}"
                f"\n{strike_tag}"
            )

        return "\n".join(lines)

    def _help_reply(self) -> str:
        return (
            "🏏 <b>Rohit Tracker — Commands</b>\n\n"
            "/next — When is the next match?\n"
            "/status — Current tracking status & live score\n"
            "/help — Show this message\n\n"
            "Alerts (strike, fours, sixes, wickets) are sent automatically."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Data fetcher  –  RapidAPI Cricbuzz
# ══════════════════════════════════════════════════════════════════════════════

class CricbuzzFetcher:
    """
    Uses the RapidAPI Cricbuzz wrapper.
    Endpoint reference (RapidAPI):
      GET /matches/v1/live      – currently live matches
      GET /matches/v1/recent    – recently finished matches
      GET /matches/v1/upcoming  – upcoming scheduled matches
      GET /mcenter/v1/{id}/comm – live scorecard / commentary
      GET /mcenter/v1/{id}/score – fallback scorecard
    """

    _BASE = "https://cricbuzz-cricket.p.rapidapi.com"

    @property
    def _headers(self) -> dict:
        return {
            "X-RapidAPI-Key":  Config.RAPIDAPI_KEY,
            "X-RapidAPI-Host": "cricbuzz-cricket.p.rapidapi.com",
        }

    # ── Internal HTTP helper ───────────────────────────────────────────────

    def _raw_get(self, path: str) -> dict:
        url = f"{self._BASE}{path}"
        for attempt in range(1, Config.MAX_RETRIES + 1):
            try:
                resp = requests.get(url, headers=self._headers,
                                    timeout=Config.REQUEST_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as exc:
                log.warning("HTTP %s on %s (attempt %d)",
                            exc.response.status_code, url, attempt)
            except (requests.ConnectionError, requests.Timeout) as exc:
                log.warning("Network error on %s (attempt %d): %s",
                            url, attempt, exc)
            except ValueError as exc:
                log.warning("JSON decode error on %s (attempt %d): %s",
                            url, attempt, exc)

            if attempt < Config.MAX_RETRIES:
                time.sleep(Config.RETRY_BACKOFF * attempt)

        raise RuntimeError(f"All {Config.MAX_RETRIES} retries failed for {url}")

    @staticmethod
    def _extract_matches(data: dict) -> list[dict]:
        matches = []
        for type_block in data.get("typeMatches", []):
            for series_block in type_block.get("seriesMatches", []):
                wrapper = series_block.get("seriesAdWrapper", {})
                for m in wrapper.get("matches", []):
                    info = m.get("matchInfo", {})
                    # Stash the series name for nicer display
                    info["seriesName"] = wrapper.get("seriesName", "")
                    matches.append(info)
        return matches

    # ── Public API ─────────────────────────────────────────────────────────

    def fetch_schedule(self) -> list[dict]:
        """Live matches first, falling back to recent."""
        for endpoint in ("/matches/v1/live", "/matches/v1/recent"):
            try:
                data = self._raw_get(endpoint)
                matches = self._extract_matches(data)
                if matches:
                    log.info("fetch_schedule: got %d matches from %s",
                             len(matches), endpoint)
                    return matches
            except Exception as exc:
                log.warning("fetch_schedule endpoint %s failed: %s",
                            endpoint, exc)
        return []

    def fetch_upcoming(self) -> list[dict]:
        """Upcoming scheduled matches (for /next command)."""
        try:
            data = self._raw_get("/matches/v1/upcoming")
            matches = self._extract_matches(data)
            # Sort by start date ascending
            matches.sort(key=lambda m: int(m.get("startDate", 0) or 0))
            log.info("fetch_upcoming: got %d matches", len(matches))
            return matches
        except Exception as exc:
            log.warning("fetch_upcoming failed: %s", exc)
            return []

    def fetch_match_data(self, match_id: str) -> dict:
        """Live scorecard via RapidAPI, with fallback path."""
        for path in (f"/mcenter/v1/{match_id}/comm",
                     f"/mcenter/v1/{match_id}/score"):
            try:
                data = self._raw_get(path)
                if data:
                    return data
            except Exception as exc:
                log.warning("fetch_match_data path %s failed: %s", path, exc)
        log.error("fetch_match_data(%s): all paths failed", match_id)
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# Scorecard parsers
# ══════════════════════════════════════════════════════════════════════════════

def _team_playing_today(matches: list[dict], target_teams: tuple) -> Optional[dict]:
    from datetime import timedelta
    today = datetime.now(UTC).date()
    for m in matches:
        team1    = m.get("team1", {}).get("teamName", "")
        team2    = m.get("team2", {}).get("teamName", "")
        start_ms = int(m.get("startDate", 0) or 0)
        match_date = (
            datetime.fromtimestamp(start_ms / 1000, UTC).date()
            if start_ms else None
        )

        if match_date and match_date < today - timedelta(days=1):
            continue

        if any(t.lower() in team1.lower() or t.lower() in team2.lower()
               for t in target_teams):
            log.info("Match candidate: %s vs %s (matchId=%s)",
                     team1, team2, m.get("matchId", "?"))
            return m
    return None


def _is_batting(live_data: dict, target_teams: tuple) -> bool:
    try:
        score_card = live_data.get("scoreCard", [])
        if score_card:
            for innings in score_card:
                bat_team_name = (
                    innings.get("batTeamDetails", {})
                           .get("batTeamName", "")
                )
                if any(t.lower() in bat_team_name.lower() for t in target_teams):
                    return True

        batting_team = (
            live_data.get("miniscore", {})
                     .get("batTeam", {})
                     .get("teamScore", {})
                     .get("id", "")
            or live_data.get("miniscore", {}).get("battingTeam", "")
        )
        return any(t.lower() in batting_team.lower() for t in target_teams)
    except Exception as e:
        log.debug("_is_batting error: %s", e)
        return False


def _find_player(live_data: dict, player_name: str) -> Optional[dict]:
    needle = player_name.lower()
    try:
        for innings in live_data.get("scoreCard", []):
            batsmen_data: dict = (
                innings.get("batTeamDetails", {})
                       .get("batsmenData", {})
            )
            for batter in batsmen_data.values():
                name = batter.get("batName", "").lower()
                if needle in name and batter.get("outDesc", "") == "":
                    return {
                        "name":      batter.get("batName"),
                        "runs":      batter.get("runs", 0),
                        "balls":     batter.get("balls", 0),
                        "fours":     batter.get("fours", 0),
                        "sixes":     batter.get("sixes", 0),
                        "isStriker": batter.get("isStriker", False),
                    }
    except Exception as exc:
        log.debug("_find_player (scoreCard path) error: %s", exc)

    try:
        batsmen: list = (
            live_data.get("miniscore", {})
                     .get("batTeam", {})
                     .get("batsmen", [])
        )
        for b in batsmen:
            name = b.get("name", b.get("fullName", "")).lower()
            if needle in name:
                return b
    except Exception as exc:
        log.debug("_find_player (miniscore path) error: %s", exc)

    return None


# ══════════════════════════════════════════════════════════════════════════════
# State handlers
# ══════════════════════════════════════════════════════════════════════════════

def handle_schedule(ctx: BotContext, fetcher: CricbuzzFetcher,
                    notifier: TelegramNotifier) -> None:
    log.info("[State 1] Checking today's schedule…")
    matches = fetcher.fetch_schedule()
    target_match = _team_playing_today(matches, Config.TARGET_TEAMS)

    if not target_match:
        log.info("[State 1] No target team match found. Sleeping 24 h.")
        time.sleep(Config.POLL_SCHEDULE)
        return

    match_id = str(target_match.get("matchId", ""))
    start_ms = int(target_match.get("startDate", 0) or 0)
    start_time = (
        datetime.fromtimestamp(start_ms / 1000, UTC)
        if start_ms else datetime.now(UTC)
    )

    log.info("[State 1] Match found: %s  ID=%s  start=%s UTC",
             target_match.get("matchDesc", ""), match_id, start_time)

    ctx.match_id    = match_id
    ctx.match_start = start_time
    ctx.match_desc  = (
        f"{target_match.get('team1', {}).get('teamName','')} vs "
        f"{target_match.get('team2', {}).get('teamName','')} "
        f"({target_match.get('matchDesc','')})"
    )

    wait_seconds = max(0.0, (start_time - datetime.now(UTC)).total_seconds())
    if wait_seconds > 60:
        log.info("[State 1] Waiting %.0f s until match start.", wait_seconds)
        time.sleep(wait_seconds)

    ctx.state = BotState.INNINGS


def handle_innings(ctx: BotContext, fetcher: CricbuzzFetcher,
                   notifier: TelegramNotifier) -> None:
    log.info("[State 2] Checking live innings status (match %s)…", ctx.match_id)
    live = fetcher.fetch_match_data(ctx.match_id)

    if not live:
        log.warning("[State 2] Empty payload. Retrying in 15 min.")
        time.sleep(Config.POLL_INNINGS)
        return

    match_state = (
        live.get("matchHeader", {}).get("state", "").lower()
        or live.get("status", "").lower()
    )
    if "complete" in match_state or "result" in match_state:
        log.info("[State 2] Match finished. Returning to State 1.")
        ctx.match_id   = ""
        ctx.match_desc = ""
        ctx.state      = BotState.SCHEDULE
        return

    if _is_batting(live, Config.TARGET_TEAMS):
        log.info("[State 2] Target team is batting. Moving to State 3.")
        ctx.state = BotState.PRESENCE
    else:
        log.info("[State 2] Target team not batting. Sleeping 15 min.")
        time.sleep(Config.POLL_INNINGS)


def handle_presence(ctx: BotContext, fetcher: CricbuzzFetcher,
                    notifier: TelegramNotifier) -> None:
    log.info("[State 3] Scanning for %s at the crease…", Config.TARGET_PLAYER)
    live = fetcher.fetch_match_data(ctx.match_id)

    if not live:
        log.warning("[State 3] Empty payload. Retrying in 60 s.")
        time.sleep(Config.POLL_PRESENCE)
        return

    if not _is_batting(live, Config.TARGET_TEAMS):
        log.info("[State 3] Target team no longer batting. Back to State 2.")
        ctx.state = BotState.INNINGS
        return

    player = _find_player(live, Config.TARGET_PLAYER)
    if player is None:
        log.info("[State 3] %s not yet at the crease. Sleeping 60 s.",
                 Config.TARGET_PLAYER)
        time.sleep(Config.POLL_PRESENCE)
        return

    ctx.player_cache = PlayerCache(
        is_on_strike = bool(player.get("isStriker") or player.get("isStrikker")),
        fours        = int(player.get("fours", 0)),
        sixes        = int(player.get("sixes", 0)),
        runs         = int(player.get("runs",  0)),
        balls        = int(player.get("balls", 0)),
    )
    log.info("[State 3] %s is at the crease! %d(%d). Moving to State 4.",
             Config.TARGET_PLAYER,
             ctx.player_cache.runs, ctx.player_cache.balls)

    notifier.send(
        f"🏏 <b>{Config.TARGET_PLAYER} is at the crease!</b>\n"
        f"Runs: {ctx.player_cache.runs} ({ctx.player_cache.balls} balls) | "
        f"4s: {ctx.player_cache.fours} | 6s: {ctx.player_cache.sixes}"
    )
    ctx.state = BotState.TRACKING


def handle_tracking(ctx: BotContext, fetcher: CricbuzzFetcher,
                    notifier: TelegramNotifier) -> None:
    log.debug("[State 4] Ball-by-ball poll…")
    live = fetcher.fetch_match_data(ctx.match_id)

    if not live:
        log.warning("[State 4] Empty payload; continuing.")
        time.sleep(Config.POLL_TRACKING)
        return

    if not _is_batting(live, Config.TARGET_TEAMS):
        log.info("[State 4] Innings over. Returning to State 2.")
        ctx.state = BotState.INNINGS
        return

    player = _find_player(live, Config.TARGET_PLAYER)
    if player is None:
        log.info("[State 4] %s has left the crease. Final: %d(%d).",
                 Config.TARGET_PLAYER,
                 ctx.player_cache.runs, ctx.player_cache.balls)
        notifier.send(
            f"❌ <b>{Config.TARGET_PLAYER} is OUT!</b>\n"
            f"Score: {ctx.player_cache.runs} ({ctx.player_cache.balls} balls) | "
            f"4s: {ctx.player_cache.fours} | 6s: {ctx.player_cache.sixes}"
        )
        ctx.player_cache.dismissed = True
        ctx.state = BotState.INNINGS
        return

    now_strike = bool(player.get("isStriker") or player.get("isStrikker"))
    now_fours  = int(player.get("fours", 0))
    now_sixes  = int(player.get("sixes", 0))
    now_runs   = int(player.get("runs",  0))
    now_balls  = int(player.get("balls", 0))
    prev = ctx.player_cache

    if now_strike and not prev.is_on_strike:
        log.info("[State 4] %s is ON STRIKE.", Config.TARGET_PLAYER)
        notifier.send(
            f"🎯 <b>{Config.TARGET_PLAYER} is on strike!</b>\n"
            f"{now_runs} ({now_balls}) | 4s: {now_fours} | 6s: {now_sixes}"
        )

    if now_fours > prev.fours:
        new_fours = now_fours - prev.fours
        log.info("[State 4] FOUR! (+%d)", new_fours)
        notifier.send(
            f"🔴 <b>FOUR!</b> {Config.TARGET_PLAYER} hits "
            f"{'a' if new_fours == 1 else str(new_fours)} four"
            f"{'s' if new_fours > 1 else ''}!\n"
            f"{now_runs} ({now_balls}) | 4s: {now_fours} | 6s: {now_sixes}"
        )

    if now_sixes > prev.sixes:
        new_sixes = now_sixes - prev.sixes
        log.info("[State 4] SIX! (+%d)", new_sixes)
        notifier.send(
            f"💥 <b>SIX!</b> {Config.TARGET_PLAYER} hits "
            f"{'a' if new_sixes == 1 else str(new_sixes)} six"
            f"{'es' if new_sixes > 1 else ''}!\n"
            f"{now_runs} ({now_balls}) | 4s: {now_fours} | 6s: {now_sixes}"
        )

    prev.update(now_strike, now_fours, now_sixes, now_runs, now_balls)
    time.sleep(Config.POLL_TRACKING)


# ══════════════════════════════════════════════════════════════════════════════
# Main orchestration loop
# ══════════════════════════════════════════════════════════════════════════════

_STATE_HANDLERS = {
    BotState.SCHEDULE: handle_schedule,
    BotState.INNINGS:  handle_innings,
    BotState.PRESENCE: handle_presence,
    BotState.TRACKING: handle_tracking,
}


def main() -> None:
    log.info("═" * 60)
    log.info("  Rohit Sharma Cricket Tracker  v3")
    log.info("  Watching: %s", ", ".join(Config.TARGET_TEAMS))
    log.info("  Chat ID : %s", Config.TELEGRAM_CHAT_ID)
    log.info("═" * 60)

    notifier = TelegramNotifier(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)
    fetcher  = CricbuzzFetcher()
    ctx      = BotContext()

    # Start the two-way command listener in the background
    listener = TelegramListener(Config.TELEGRAM_BOT_TOKEN,
                                Config.TELEGRAM_CHAT_ID,
                                fetcher, ctx)
    listener.start()

    notifier.send(
        "🏏 <b>Rohit Tracker v3 is now running.</b>\n"
        f"Monitoring: {', '.join(Config.TARGET_TEAMS)}\n\n"
        "Send /help to see available commands."
    )

    while True:
        try:
            _STATE_HANDLERS[ctx.state](ctx, fetcher, notifier)
        except KeyboardInterrupt:
            log.info("Keyboard interrupt – shutting down.")
            notifier.send("🔴 Rohit Tracker has been stopped manually.")
            break
        except Exception as exc:
            log.exception("Unhandled error in state %s: %s", ctx.state, exc)
            time.sleep(30)


if __name__ == "__main__":
    main()
