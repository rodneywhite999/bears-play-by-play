#!/usr/bin/env python3
"""Bears live play-by-play -> iMessage (or WhatsApp / SMS / console).

Watches ESPN's public live game feed every few seconds and sends every new
play as a text message the moment it shows up. Standard library only.

    python3 bears_pbp.py --daemon     # run forever; track every Bears game (Mac service)
    python3 bears_pbp.py              # track a game that's live or starting within 75 min, then exit
    python3 bears_pbp.py --test       # send a test message to every configured channel
    python3 bears_pbp.py --replay 401873036 --senders console   # dry-run a finished game

Channels (environment variables):
    IMESSAGE_TO         phone (+13125551234) or Apple ID email; needs macOS + Messages
    WHATSAPP_PHONE + CALLMEBOT_APIKEY   free CallMeBot WhatsApp relay (rate-limited!)
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO   Twilio SMS/WhatsApp

Tuning:
    TEAM (default CHI), POLL_SECONDS (5), START_WINDOW_MIN (75), LOCAL_TZ (America/Chicago)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

TEAM = os.environ.get("TEAM", "CHI").upper()
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
START_WINDOW_MIN = float(os.environ.get("START_WINDOW_MIN", "75"))
LOCAL_TZ = os.environ.get("LOCAL_TZ", "America/Chicago")
MAX_MSG_CHARS = 1500  # keep WhatsApp messages comfortably small


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_USE_CURL = False


def http(url, data=None, headers=None, timeout=15):
    """GET/POST returning (status, body). Falls back to the system `curl` if this
    Python can't verify HTTPS certificates (common with some Mac Python installs)."""
    global _USE_CURL
    h = {"Cache-Control": "no-cache"}
    h.update(headers or {})
    if not _USE_CURL:
        req = urllib.request.Request(url, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            if "CERTIFICATE_VERIFY_FAILED" not in str(e) or not shutil.which("curl"):
                raise
            log("Python can't verify HTTPS certificates here; using system curl instead.")
            _USE_CURL = True
    cmd = ["curl", "-sS", "-m", str(timeout), "-w", "\n%{http_code}"]
    for k, v in h.items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    cmd.append(url)
    out = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout + 5)
    if out.returncode != 0:
        raise RuntimeError(f"curl failed: {out.stderr.decode(errors='replace').strip()}")
    body, _, code = out.stdout.decode("utf-8", "replace").rpartition("\n")
    status = int(code or 0)
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")
    return status, body


def espn_json(path, **params):
    # cache-buster so we never get a stale CDN copy
    params["_"] = str(int(time.time() * 1000))
    url = f"{ESPN}/{path}?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(4):
        try:
            _, body = http(url)
            return json.loads(body)
        except Exception as e:  # network blip, 5xx, bad json...
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ESPN request failed: {path}: {last}")


# --------------------------------------------------------------------------
# Senders
# --------------------------------------------------------------------------

class Sender:
    name = "base"

    def send(self, text: str) -> bool:
        raise NotImplementedError


class ConsoleSender(Sender):
    name = "console"

    def send(self, text):
        print("-" * 40 + "\n" + text + "\n" + "-" * 40, flush=True)
        return True


class CallMeBotSender(Sender):
    """Free WhatsApp-to-yourself API: https://www.callmebot.com/blog/free-api-whatsapp-messages/"""

    name = "whatsapp(callmebot)"
    MIN_GAP = 2.0  # seconds between messages, be polite to the free service

    def __init__(self, phone, apikey):
        self.phone = phone.strip()
        self.apikey = apikey.strip()
        self._last = 0.0

    def send(self, text):
        wait = self.MIN_GAP - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
            {"phone": self.phone, "text": text, "apikey": self.apikey}
        )
        for attempt in range(4):
            try:
                status, body = http(url, timeout=30)
                self._last = time.time()
                low = body.lower()
                if status == 200 and "error" not in low and "apikey is invalid" not in low:
                    return True
                log(f"callmebot rejected (attempt {attempt + 1}): {status} {_strip_html(body)[:200]}")
            except Exception as e:
                log(f"callmebot error (attempt {attempt + 1}): {e}")
            time.sleep(2 * (attempt + 1))
        return False


class TwilioSender(Sender):
    name = "twilio"

    def __init__(self, sid, token, frm, to):
        self.sid, self.token, self.frm, self.to = sid, token, frm, to

    def send(self, text):
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        data = urllib.parse.urlencode({"From": self.frm, "To": self.to, "Body": text}).encode()
        auth = b64encode(f"{self.sid}:{self.token}".encode()).decode()
        for attempt in range(3):
            try:
                status, _ = http(url, data=data, headers={"Authorization": f"Basic {auth}"}, timeout=20)
                if status in (200, 201):
                    return True
            except urllib.error.HTTPError as e:
                log(f"twilio HTTP {e.code}: {e.read()[:200]!r}")
            except Exception as e:
                log(f"twilio error: {e}")
            time.sleep(2 * (attempt + 1))
        return False


class IMessageSender(Sender):
    """Sends via the Messages app. Only works when this script runs on a Mac
    that is signed in to iMessage."""

    name = "imessage"
    MIN_GAP = 1.0

    # Newer macOS uses "participant", older uses "buddy"; try both.
    SCRIPTS = [
        'on run argv\n'
        ' tell application "Messages"\n'
        '  set svc to 1st account whose service type = iMessage\n'
        '  send (item 2 of argv) to participant (item 1 of argv) of svc\n'
        ' end tell\n'
        'end run',
        'on run argv\n'
        ' tell application "Messages"\n'
        '  set svc to 1st service whose service type = iMessage\n'
        '  send (item 2 of argv) to buddy (item 1 of argv) of svc\n'
        ' end tell\n'
        'end run',
    ]

    def __init__(self, to):
        self.to = to.strip()
        self._last = 0.0
        self._script = 0

    def send(self, text):
        wait = self.MIN_GAP - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            for i in [self._script] + [j for j in range(len(self.SCRIPTS)) if j != self._script]:
                try:
                    r = subprocess.run(
                        ["osascript", "-e", self.SCRIPTS[i], self.to, text],
                        capture_output=True, text=True, timeout=60,
                    )
                except Exception as e:
                    log(f"imessage error: {e}")
                    continue
                if r.returncode == 0:
                    self._script = i
                    self._last = time.time()
                    return True
                log(f"imessage error (variant {i}): {r.stderr.strip()[:300]}")
            time.sleep(2 * (attempt + 1))
        return False


def _strip_html(s):
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def build_senders(names: str | None):
    env = os.environ
    available = {}
    if env.get("WHATSAPP_PHONE") and env.get("CALLMEBOT_APIKEY"):
        available["whatsapp"] = CallMeBotSender(env["WHATSAPP_PHONE"], env["CALLMEBOT_APIKEY"])
    if all(env.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO")):
        available["twilio"] = TwilioSender(
            env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"], env["TWILIO_FROM"], env["TWILIO_TO"]
        )
    if env.get("IMESSAGE_TO"):
        available["imessage"] = IMessageSender(env["IMESSAGE_TO"])
    available["console"] = ConsoleSender()

    if names:
        wanted = [n.strip() for n in names.split(",") if n.strip()]
        missing = [n for n in wanted if n not in available]
        if missing:
            sys.exit(f"Sender(s) not configured: {missing}. Configured: {list(available)}")
        return [available[n] for n in wanted]
    chosen = [s for k, s in available.items() if k != "console"]
    if not chosen:
        log("WARNING: no messaging channel configured (set IMESSAGE_TO). Printing to console.")
        chosen = [available["console"]]
    return chosen


class Outbox:
    def __init__(self, senders):
        self.senders = senders

    def send(self, text):
        for chunk in _chunks(text, MAX_MSG_CHARS):
            for s in self.senders:
                if s.send(chunk):
                    if not isinstance(s, ConsoleSender):
                        log(f"SENT via {s.name}:", chunk.replace("\n", " | "))
                else:
                    log(f"!! failed to deliver via {s.name}")


def _chunks(text, n):
    if len(text) <= n:
        return [text]
    out, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + 2 + len(block) > n:
            out.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        out.append(cur)
    return [c[:n] for c in out]


# --------------------------------------------------------------------------
# Game data
# --------------------------------------------------------------------------

def parse_dt(s):
    # ESPN uses "2026-11-09T01:20Z" (sometimes with seconds)
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def local_time(dt):
    if ZoneInfo:
        try:
            dt = dt.astimezone(ZoneInfo(LOCAL_TZ))
        except Exception:
            pass
    return dt.strftime("%a %b %-d, %-I:%M %p %Z")


def find_game(now=None):
    """Return (event_id, kickoff_dt, state) for a team game that is live or starts soon."""
    now = now or datetime.now(timezone.utc)
    sched = espn_json(f"teams/{TEAM.lower()}/schedule")
    best = None
    for ev in sched.get("events", []):
        comp = ev["competitions"][0]
        state = comp["status"]["type"]["state"]
        kick = parse_dt(ev["date"])
        if state == "in":
            return ev["id"], kick, state
        if state == "pre" and kick - timedelta(minutes=START_WINDOW_MIN) <= now <= kick + timedelta(hours=6):
            best = (ev["id"], kick, state)
    return best


class Game:
    def __init__(self, event_id):
        self.event_id = event_id
        self.teams = {}  # id -> abbreviation
        self.home = self.away = None
        self.data = {}

    def refresh(self):
        self.data = espn_json("summary", event=self.event_id)
        comp = self.data["header"]["competitions"][0]
        for c in comp["competitors"]:
            ab = c["team"]["abbreviation"]
            self.teams[c["team"]["id"]] = ab
            if c["homeAway"] == "home":
                self.home = ab
                self.home_score = int(c.get("score") or 0)
            else:
                self.away = ab
                self.away_score = int(c.get("score") or 0)
        self.status = comp["status"]
        return self

    @property
    def state(self):
        return self.status["type"]["state"]

    @property
    def opponent(self):
        return self.home if self.away == TEAM else self.away

    def score_line(self, away=None, home=None):
        a = self.away_score if away is None else away
        h = self.home_score if home is None else home
        mine, theirs = (a, h) if self.away == TEAM else (h, a)
        return f"{TEAM} {mine} - {self.opponent} {theirs}"

    def plays(self):
        """All plays in order, each tagged with its drive."""
        drives = self.data.get("drives") or {}
        out = []
        all_drives = list(drives.get("previous") or [])
        cur = drives.get("current")
        if cur and not any(d.get("id") == cur.get("id") for d in all_drives):
            all_drives.append(cur)
        for d in all_drives:
            for p in d.get("plays") or []:
                out.append((p, d))
        out.sort(key=lambda x: _seq(x[0]))
        return out

    def finished_drives(self):
        return [d for d in (self.data.get("drives") or {}).get("previous") or [] if d.get("result") or d.get("displayResult")]


def _seq(p):
    try:
        return int(p.get("sequenceNumber") or 0)
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

QUIET_TYPES = {"Official Timeout"}
SCORE_WORDS = ("Touchdown", "Field Goal Good", "Safety", "Two-Point", "Extra Point")


def qtr(n):
    return {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}.get(n, "OT" if n == 5 else f"OT{n - 4}")


def format_play(game: Game, p, drive) -> str | None:
    ptype = (p.get("type") or {}).get("text", "")
    if ptype in QUIET_TYPES:
        return None
    text = (p.get("text") or "").strip()
    if not text:
        return None
    period = (p.get("period") or {}).get("number", 0)
    clock = (p.get("clock") or {}).get("displayValue", "")
    score = game.score_line(p.get("awayScore"), p.get("homeScore"))

    start = p.get("start") or {}
    off_id = ((start.get("team") or {}).get("id")) or ((drive.get("team") or {}).get("id"))
    off = game.teams.get(str(off_id), "")
    ours = off == TEAM

    head = []
    if p.get("scoringPlay"):
        scorer = game.teams.get(str((p.get("team") or {}).get("id")), off)
        if scorer == TEAM:
            head.append(f"🚨🐻 {TEAM} SCORE! {ptype.upper()} 🐻🚨")
        else:
            head.append(f"😤 {scorer} score: {ptype}")
    elif p.get("isTurnover") or "Interception" in ptype or "Fumble Recovery (Opponent)" in ptype:
        head.append("🔄 TURNOVER!")
    elif ptype in ("End Period", "End of Half", "End of Game", "Two-minute warning", "Timeout"):
        return f"⏱️ {qtr(period)} {clock} · {text} · {score}"

    situation = start.get("downDistanceText") or ""
    who = "🐻" if ours else "🛡️"
    lines = head + [f"{qtr(period)} {clock} · {score}"]
    if situation and off:
        lines.append(f"{who} {off} {situation}")
    lines.append(text)
    return "\n".join(lines)


def format_drive(game: Game, d) -> str:
    team = (d.get("team") or {}).get("abbreviation", "")
    res = d.get("displayResult") or d.get("result") or ""
    desc = d.get("description") or ""
    icon = "🐻" if team == TEAM else "🛡️"
    return f"📋 {icon} {team} drive: {desc} → {res}"


def game_title(game: Game):
    return f"{game.away} @ {game.home}"


# --------------------------------------------------------------------------
# Main loops
# --------------------------------------------------------------------------

def track(event_id, outbox: Outbox, poll=POLL_SECONDS, announce_catchup=True):
    game = Game(event_id).refresh()
    seen: set[str] = set()
    seen_drives: set[str] = set()
    log(f"Tracking {game_title(game)} event={event_id} state={game.state}")

    if game.state == "post":
        log("Game already final; nothing to do.")
        return

    # Pre-game: announce, then wait for kickoff.
    if game.state == "pre":
        kick = parse_dt(game.data["header"]["competitions"][0]["date"])
        outbox.send(
            f"🏈 Bears play-by-play is ON\n{game_title(game)} · kickoff {local_time(kick)}\n"
            f"You'll get every play here as it happens. Bear Down! 🐻⬇️"
        )
        while game.state == "pre":
            remaining = (kick - datetime.now(timezone.utc)).total_seconds()
            time.sleep(min(60, max(poll, remaining / 2)) if remaining > 120 else poll)
            game = _safe_refresh(game)
        # any plays already there are fresh (kickoff) - send them normally
    else:
        # Started mid-game (late start or restart): catch up briefly, don't flood.
        plays = game.plays()
        for p, _ in plays:
            seen.add(p["id"])
        for d in game.finished_drives():
            seen_drives.add(d.get("id"))
        if announce_catchup:
            recent = [format_play(game, p, d) for p, d in plays[-3:]]
            detail = game.status["type"].get("shortDetail", "")
            outbox.send(
                f"🏈 Bears play-by-play connected ({detail})\n{game_title(game)} · {game.score_line()}\n\n"
                + "Latest:\n\n" + "\n\n".join(r for r in recent if r)
            )

    while True:
        new_msgs = []
        for p, d in game.plays():
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            msg = format_play(game, p, d)
            if msg:
                new_msgs.append(msg)
        for d in game.finished_drives():
            did = d.get("id")
            if did and did not in seen_drives:
                seen_drives.add(did)
                new_msgs.append(format_drive(game, d))

        if new_msgs:
            outbox.send("\n\n".join(new_msgs))

        if game.state == "post":
            detail = game.status["type"].get("detail", "Final")
            mine = game.away_score if game.away == TEAM else game.home_score
            theirs = game.home_score if game.away == TEAM else game.away_score
            verdict = "BEARS WIN! 🐻🎉" if mine > theirs else ("Tie game." if mine == theirs else "Bears lose. 😞")
            outbox.send(f"🏁 {detail}: {game.score_line()}\n{verdict}")
            log("Game over.")
            return

        time.sleep(poll)
        game = _safe_refresh(game)


def _safe_refresh(game):
    try:
        return game.refresh()
    except Exception as e:
        log(f"refresh failed, will retry: {e}")
        time.sleep(3)
        return game


def replay(event_id, outbox: Outbox, delay: float, limit: int | None):
    """Send a finished game's plays as if live (for testing formatting/delivery)."""
    game = Game(event_id).refresh()
    outbox.send(f"🧪 TEST replay: {game_title(game)}")
    seen_drives = set()
    n = 0
    plays = game.plays()
    drive_end = {}
    for d in game.finished_drives():
        if d.get("plays"):
            drive_end[d["plays"][-1]["id"]] = d
    for p, d in plays:
        msg = format_play(game, p, d)
        extra = drive_end.get(p["id"])
        if extra and extra.get("id") not in seen_drives:
            seen_drives.add(extra.get("id"))
            msg = (msg + "\n\n" if msg else "") + format_drive(game, extra)
        if msg:
            outbox.send(msg)
            n += 1
            if limit and n >= limit:
                break
            time.sleep(delay)
    outbox.send(f"🧪 TEST replay done. Final was {game.score_line()}")


def daemon(outbox: Outbox):
    """Run forever (used by the Mac background service): wake up for every
    Bears game, track it, go back to waiting."""
    done = set()
    try:
        nxt = next_game_text()
        outbox.send(f"🐻 Bears play-by-play bot is running on {socket.gethostname()}.{nxt}\n"
                    f"Keep this Mac on, plugged in and awake on game days.")
    except Exception as e:
        log(f"startup message failed: {e}")
    while True:
        try:
            found = find_game()
            if found and found[0] not in done:
                event_id, kick, state = found
                log(f"Found game {event_id} kickoff {kick.isoformat()} state={state}")
                track(event_id, outbox)
                done.add(event_id)
                continue
            time.sleep(120 if found else 300)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"daemon loop error, retrying in 30s: {e!r}")
            time.sleep(30)


def next_game_text():
    sched = espn_json(f"teams/{TEAM.lower()}/schedule")
    now = datetime.now(timezone.utc)
    for ev in sched.get("events", []):
        if ev["competitions"][0]["status"]["type"]["state"] != "post" and parse_dt(ev["date"]) > now - timedelta(hours=5):
            return f"\nNext up: {ev['shortName']} · {local_time(parse_dt(ev['date']))}"
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", help="ESPN event id to track (default: auto-detect next Bears game)")
    ap.add_argument("--test", action="store_true", help="send a test message and exit")
    ap.add_argument("--daemon", action="store_true", help="run forever, tracking every game (Mac background service)")
    ap.add_argument("--replay", metavar="EVENT", help="replay a finished game's plays")
    ap.add_argument("--replay-limit", type=int, default=None)
    ap.add_argument("--replay-delay", type=float, default=1.0)
    ap.add_argument("--senders", help="comma list: whatsapp,twilio,imessage,console (default: all configured)")
    args = ap.parse_args()

    outbox = Outbox(build_senders(args.senders))

    if args.test:
        nxt = ""
        try:
            nxt = next_game_text()
        except Exception as e:
            log(f"schedule lookup failed: {e}")
        outbox.send(f"✅ Bears play-by-play test message. If you can read this, you're all set.{nxt}")
        return

    if args.daemon:
        daemon(outbox)
        return

    if args.replay:
        replay(args.replay, outbox, args.replay_delay, args.replay_limit)
        return

    if args.event:
        track(args.event, outbox)
        return

    found = find_game()
    if not found:
        log(f"No {TEAM} game live or within {START_WINDOW_MIN:.0f} min of kickoff. Exiting.")
        return
    event_id, kick, state = found
    log(f"Found game {event_id} kickoff {kick.isoformat()} state={state}")
    track(event_id, outbox)


if __name__ == "__main__":
    main()
