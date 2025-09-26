"""
Exhaust Leaderboard — PUUID→Spectator only
- Hardcoded config (API key, region, names)
- Flow:
    RiotID (gameName#tagLine) -> account-v1 -> PUUID
    Spectator-v5 using *PUUID as path param* to get the active game
    For each participant: fetch last N ARAM matches (queue=450) via Match-V5
    Count how often they took Exhaust (ID=3), rank by count then %
- Unicode-safe table so non-English names align
- Global rate limiter: 20 req / 1s and 100 req / 120s
"""

import time
import sys
import unicodedata
from collections import deque
from typing import List, Optional, Tuple

import requests

# =========================
# ======== CONFIG =========
# =========================

DEBUG = True  # set False for quiet output

API_KEY   = "YOUR-API-KEY-HERE"
PLATFORM  = "na1"       # spectator/summoner platform (na1, euw1, kr, etc.)
REGIONAL  = "americas"  # match-v5 regional router (americas, europe, asia, sea)

RIOT_NAME = "YOUR-IGN-HERE"   # gameName (case/space-sensitive; use exactly as shown in client/Lobby)
RIOT_TAG  = "YOUR-TAG-HERE"              # tagLine (e.g. NA1, EUW, 777, etc.)

MAX_MATCHES_PER_PLAYER = 20    # how many recent ARAM matches to scan per player
EXHAUST_ID = 3
ARAM_QUEUE_ID = 450

SLEEP_BETWEEN_PLAYERS = 0.05

# =========================
# ===== RATE LIMITING =====
# =========================
# Dev portal "small key" limits (application-tier):
# - 20 requests every 1 second
# - 100 requests every 120 seconds
ONE_SEC_LIMIT     = 20
ONE_SEC_WINDOW    = 1.0
TWO_MIN_LIMIT     = 100
TWO_MIN_WINDOW    = 120.0


def dprint(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


class RateLimiter:
    """Simple sliding-window limiter for two windows: 1s/20 and 120s/100."""
    def __init__(self):
        self.w1 = deque()   # timestamps in last 1s
        self.w2 = deque()   # timestamps in last 120s

    def _prune(self, now: float):
        while self.w1 and now - self.w1[0] > ONE_SEC_WINDOW:
            self.w1.popleft()
        while self.w2 and now - self.w2[0] > TWO_MIN_WINDOW:
            self.w2.popleft()

    def wait_for_slot(self):
        while True:
            now = time.time()
            self._prune(now)
            if len(self.w1) < ONE_SEC_LIMIT and len(self.w2) < TWO_MIN_LIMIT:
                # take a slot
                self.w1.append(now)
                self.w2.append(now)
                return
            # compute minimal sleep needed
            sleep1 = (self.w1[0] + ONE_SEC_WINDOW - now) if self.w1 else 0.0
            sleep2 = (self.w2[0] + TWO_MIN_WINDOW - now) if self.w2 else 0.0
            sleep_for = max(sleep1, sleep2, 0.01)
            if DEBUG:
                dprint(f"[ratelimit] sleeping {sleep_for:.3f}s (w1={len(self.w1)}/20, w2={len(self.w2)}/100)")
            time.sleep(sleep_for)


# Global limiter instance used by RiotClient
RATE_LIMITER = RateLimiter()

# =========================
# ===== UNICODE WIDTH =====
# =========================

def _char_width(ch: str) -> int:
    """Return display width of a single char (rough heuristic; no external deps)."""
    if unicodedata.combining(ch):
        return 0
    eaw = unicodedata.east_asian_width(ch)
    # Treat Fullwidth/Wide as width 2, others as 1 (Narrow, Halfwidth, Ambiguous, Neutral).
    return 2 if eaw in ("F", "W") else 1

def display_width(s: str) -> int:
    return sum(_char_width(ch) for ch in s)

def pad_display(s: str, width: int) -> str:
    """Pad string with spaces so its display width equals `width`."""
    w = display_width(s)
    if w >= width:
        return s
    return s + " " * (width - w)

# =========================
# ====== RIOT CLIENT ======
# =========================

class RiotClient:
    def __init__(self, api_key: str, platform: str, regional: str, timeout: float = 8.0):
        self.api_key = api_key
        self.platform = platform
        self.regional = regional
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Riot-Token": api_key})

    def _get(self, url: str, params: Optional[dict] = None, max_retries: int = 3, backoff_sec: float = 1.0):
        for attempt in range(1, max_retries + 1):
            RATE_LIMITER.wait_for_slot()
            if DEBUG:
                q = f"?{requests.compat.urlencode(params)}" if params else ""
                dprint(f"[GET] {url}{q} (attempt {attempt}/{max_retries})")
            resp = self.session.get(url, params=params, timeout=self.timeout)

            # Handle 429 with Retry-After or conservative backoff
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                sleep_for = float(ra) if ra else max(1.0, backoff_sec * attempt * 5)
                dprint(f"[GET] 429 rate limited. Sleeping {sleep_for:.1f}s")
                time.sleep(sleep_for)
                continue

            # Retry transient 5xx
            if 500 <= resp.status_code < 600:
                sleep_for = backoff_sec * attempt
                dprint(f"[GET] {resp.status_code} server error. Sleeping {sleep_for:.2f}s")
                time.sleep(sleep_for)
                continue

            if resp.ok:
                try:
                    return resp.json()
                except Exception:
                    return {}
            # Non-ok: raise with context
            try:
                err = resp.json()
            except Exception:
                err = {"status": {"message": resp.text}}
            raise RuntimeError(f"GET {url} failed [{resp.status_code}]: {err}")
        raise RuntimeError(f"GET {url} exhausted retries")

    # ----- Identity -----
    def account_by_riot_id(self, gameName: str, tagLine: str) -> dict:
        url = f"https://{self.regional}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{requests.utils.quote(gameName)}/{requests.utils.quote(tagLine)}"
        return self._get(url)

    def summoner_by_id(self, encrypted_summoner_id: str) -> dict:
        url = f"https://{self.platform}.api.riotgames.com/lol/summoner/v4/summoners/{encrypted_summoner_id}"
        return self._get(url)

    # ----- Live game -----
    def active_game_by_summoner_id(self, encrypted_or_puuid: str) -> dict:
        # We intentionally pass the PUUID here per the “workaround” practice.
        url = f"https://{self.platform}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{encrypted_or_puuid}"
        return self._get(url)

    # ----- Matches -----
    def match_ids_by_puuid(self, puuid: str, count: int, queue: Optional[int] = None, start: int = 0) -> List[str]:
        params = {"start": start, "count": count}
        if queue is not None:
            params["queue"] = queue
        url = f"https://{self.regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
        return self._get(url, params=params)

    def match_by_id(self, match_id: str) -> dict:
        url = f"https://{self.regional}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        return self._get(url)


# =========================
# ====== CORE LOGIC =======
# =========================

def get_live_participants_via_puuid_only(client: RiotClient, riot_name: str, tag: str) -> List[dict]:
    """
    Barebones “workaround” path:
      RiotID -> account-v1 -> PUUID
      Spectator-v5 with that PUUID (as if it were encryptedSummonerId)
    Assumes PLATFORM is the correct shard for the player.
    """
    dprint(f"[flow] Using Riot ID route (PUUID->Spectator): {riot_name}#{tag} (regional={client.regional}, platform={client.platform})")
    account = client.account_by_riot_id(riot_name, tag)
    dprint(f"[debug] account payload: {account}")
    puuid = account.get("puuid")
    if not puuid:
        raise RuntimeError(f"Account lookup returned no PUUID. Payload: {account}")

    game = client.active_game_by_summoner_id(puuid)  # PUUID used here by design
    participants = game.get("participants", [])
    return participants


def ensure_puuid(client: RiotClient, participant: dict) -> Tuple[str, str]:
    """
    Return (puuid, display_name) for a participant.
    Prefer PUUID directly from spectator payload; else resolve via summonerId.
    """
    puuid = participant.get("puuid")
    display_name = participant.get("riotId") or participant.get("summonerName") or "Unknown"
    if puuid:
        return puuid, display_name

    enc_id = participant.get("summonerId") or participant.get("encryptedSummonerId") or participant.get("id")
    if not enc_id:
        raise RuntimeError("Participant missing both puuid and encryptedSummonerId.")
    summ = client.summoner_by_id(enc_id)
    puuid = summ["puuid"]
    if display_name == "Unknown":
        display_name = summ.get("name", "Unknown")
    return puuid, display_name


def count_exhaust_for_player(client: RiotClient, puuid: str, max_matches: int) -> Tuple[int, int]:
    """
    Return (exhaust_count, total_tracked_matches) for last `max_matches` ARAM games.
    """
    match_ids = client.match_ids_by_puuid(puuid, count=max_matches, queue=ARAM_QUEUE_ID)
    if not match_ids:
        return 0, 0

    exhaust_count = 0
    total = 0

    for mid in match_ids:
        data = client.match_by_id(mid)
        info = data.get("info", {})
        participants = info.get("participants", [])
        me = next((p for p in participants if p.get("puuid") == puuid), None)
        if not me:
            continue

        s1 = me.get("summoner1Id")
        s2 = me.get("summoner2Id")
        if s1 == EXHAUST_ID or s2 == EXHAUST_ID:
            exhaust_count += 1
        total += 1

    return exhaust_count, total


def format_pct(n: int, d: int) -> str:
    if d <= 0:
        return "—"
    return f"{(n / d) * 100:.0f}%"


def print_leaderboard(rows: List[Tuple[str, int, int]]):
    """
    rows: list of (display_name, exhaust_count, total_tracked)
    Unicode-safe alignment using display width.
    Adds 'WINNER: ' as a prefix to the #1 player's full name.
    """
    # Sort: by exhaust count desc, then percentage desc
    sorted_rows = sorted(
        rows,
        key=lambda r: (r[1], (r[1] / r[2]) if r[2] else 0.0),
        reverse=True
    )

    # Prefix "WINNER: " to the top row's name
    display_rows: List[Tuple[str, int, int]] = []
    for i, (name, ex_cnt, total) in enumerate(sorted_rows):
        name_print = f"WINNER: {name}" if i == 0 else name
        display_rows.append((name_print, ex_cnt, total))

    # Compute dynamic widths using the adjusted names
    header_player = "Player"
    name_w = max(display_width(header_player), max((display_width(r[0]) for r in display_rows), default=12))
    exhaust_w = max(len("Exhausts"), 8)
    pct_w = max(len("% Exhaust"), 9)
    games_w = max(len("Games"), 5)

    total_line_w = name_w + 2 + exhaust_w + 2 + pct_w + 2 + games_w

    print("\nExhaust Leaderboard (last ARAM matches)")
    print("-" * total_line_w)
    print(
        f"{pad_display(header_player, name_w)}  "
        f"{'Exhausts'.rjust(exhaust_w)}  "
        f"{'% Exhaust'.rjust(pct_w)}  "
        f"{'Games'.rjust(games_w)}"
    )
    print("-" * total_line_w)
    for name, ex_cnt, total in display_rows:
        pct = format_pct(ex_cnt, total)
        print(
            f"{pad_display(name, name_w)}  "
            f"{str(ex_cnt).rjust(exhaust_w)}  "
            f"{pct.rjust(pct_w)}  "
            f"{str(total).rjust(games_w)}"
        )
    print("-" * total_line_w)

def run():
    if not API_KEY or API_KEY.startswith("PUT_"):
        print("ERROR: Please set API_KEY at the top of this file.", file=sys.stderr)
        sys.exit(1)

    client = RiotClient(api_key=API_KEY, platform=PLATFORM, regional=REGIONAL)

    dprint(f"[boot] platform={PLATFORM}, regional={REGIONAL}")
    dprint(f"[boot] RiotID: {RIOT_NAME}#{RIOT_TAG}")

    try:
        participants = get_live_participants_via_puuid_only(client, RIOT_NAME, RIOT_TAG)
    except Exception as e:
        print(f"Failed to load live game participants: {e}", file=sys.stderr)
        sys.exit(2)

    if not participants:
        print("No participants returned. Is the target in a live match?", file=sys.stderr)
        sys.exit(3)

    print(f"Found {len(participants)} participants. Scanning last {MAX_MATCHES_PER_PLAYER} ARAM matches each...\n")

    rows: List[Tuple[str, int, int]] = []
    for i, p in enumerate(participants, 1):
        try:
            puuid, display_name = ensure_puuid(client, p)
        except Exception as e:
            print(f"[{i}/10] Skipping a participant (couldn't resolve PUUID): {e}", file=sys.stderr)
            continue

        try:
            ex_cnt, total = count_exhaust_for_player(client, puuid, max_matches=MAX_MATCHES_PER_PLAYER)
        except Exception as e:
            print(f"[{i}/10] Error while counting for {display_name}: {e}", file=sys.stderr)
            continue

        print(f"[{i}/10] {display_name}: {ex_cnt} Exhaust in {total} tracked ARAM matches.")
        rows.append((display_name, ex_cnt, total))
        time.sleep(SLEEP_BETWEEN_PLAYERS)

    if not rows:
        print("\nNo data to display.")
        return

    print_leaderboard(rows)


if __name__ == "__main__":
    run()
