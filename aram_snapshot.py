#!/usr/bin/env python3
"""
ARAM group-ranking snapshot generator.

Pulls every group member's ARAM (queue 450) matches from the Riot API, scores
each game with the locked "hybrid" standard, aggregates per player, and writes
`snapshot.js` (a `const SNAPSHOT = {...}` object) for the dashboard HTML.

Scoring (per game, per player):
    archetype  = enchanter (explicit set) -> tank (Data Dragon "Tank" tag) -> carry
    primary    = carry:      challenges.teamDamagePercentage
                 tank:       challenges.damageTakenOnTeamPercentage
                 enchanter:  effectiveHealAndShielding / team total
    contribPct = percentile rank of `primary` among ALL snapshot games of that archetype
    gameScore  = 0.40*win + 0.45*contribPct + 0.15*killParticipation

Per player:
    rankScore  = 100 * mean(gameScore)
               = 40*winRate + 45*meanContribPct + 15*meanKP   (components, sum == rankScore)
    provisional if games < MIN_GAMES

Usage:
    export RIOT_API_KEY="RGAPI-...."
    pip install requests
    # edit MEMBERS and REGION below
    python aram_snapshot.py

Notes:
  - Both ACCOUNT-V1 and MATCH-V5 use *regional* routing. KR/JP -> "asia",
    NA/BR/LAN/LAS/OCE -> "americas", EUW/EUNE/TR/RU -> "europe".
  - A dev key works fine for a one-off snapshot (finish inside its 24h window).
  - Raw match JSON is cached under CACHE_DIR; delete it to force a fresh pull,
    or keep it to re-tweak weights offline without touching the API.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

API_KEY = os.environ.get("RIOT_API_KEY", "")

REGION = "asia"          # regional routing cluster: asia | americas | europe
GROUP_NAME = "The Howling Abyss Boys"

# Members as Riot IDs ("gameName#tagLine").
MEMBERS = [
    "하짜르트#KR1",
    "커피도안되고#KR1",
    "함마분쇄기#신도림역",
    "복낙아#다리떨지마",
    "h1zzy#KR1",
    "야주려#KR1",
    "에프킬라#9059",
    "김규평#중앙정보부",
    # add your group here...
]

QUEUE_ARAM = 450
MAX_MATCHES_PER_PLAYER = 200     # cap the pull per member
START_TIME: Optional[int] = 1767193200  # 2026-01-01 00:00 KST; only games on/after this
END_TIME: Optional[int] = None    # epoch seconds; None = now
PATCH_LABEL = ""                  # optional, shown in the header (e.g. "16.11")

# Scoring weights (must match the dashboard's expectation).
W_WIN, W_CONTRIB, W_KP = 0.40, 0.45, 0.15
MIN_GAMES = 10
GAMELOG_SIZE = 10                # most-recent games kept per player for the detail view
MIN_GAME_DURATION = 300          # seconds; skip remakes / very short games

# Champions scored as enchanters (matched against Data Dragon display names).
ENCHANTERS = {
    "Soraka", "Sona", "Janna", "Lulu", "Nami", "Karma", "Yuumi",
    "Renata Glasc", "Milio", "Seraphine", "Taric", "Ivern",
}

CACHE_DIR = Path("match_cache")
OUT_JS = Path("snapshot.js")
OUT_JSON = Path("snapshot.json")

BASE = f"https://{REGION}.api.riotgames.com"
DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json"


# --------------------------------------------------------------------------- #
# Rate-limited HTTP                                                           #
# --------------------------------------------------------------------------- #

class RiotClient:
    """Minimal Riot API client respecting dev-key limits (20/s, 100/120s)."""

    def __init__(self, api_key: str, per_sec: int = 18, per_two_min: int = 95):
        if not api_key:
            sys.exit("RIOT_API_KEY is not set. Export it before running.")
        self.session = requests.Session()
        self.session.headers["X-Riot-Token"] = api_key
        self.per_sec = per_sec
        self.per_two_min = per_two_min
        self._sec_window: deque[float] = deque()
        self._min_window: deque[float] = deque()

    def _throttle(self) -> None:
        now = time.monotonic()
        for window, span, limit in (
            (self._sec_window, 1.0, self.per_sec),
            (self._min_window, 120.0, self.per_two_min),
        ):
            while window and now - window[0] > span:
                window.popleft()
            if len(window) >= limit:
                time.sleep(span - (now - window[0]) + 0.01)
        stamp = time.monotonic()
        self._sec_window.append(stamp)
        self._min_window.append(stamp)

    def get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        for attempt in range(6):
            self._throttle()
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2"))
                print(f"  429 rate limited; sleeping {wait}s", file=sys.stderr)
                time.sleep(wait + 1)
                continue
            if resp.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
        print(f"  giving up on {url}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# Data Dragon (champion -> archetype)                                         #
# --------------------------------------------------------------------------- #

def load_champion_tags() -> tuple[dict[int, set[str]], set[int]]:
    """Return (championId -> tag set, set of enchanter championIds)."""
    ver = requests.get(DDRAGON_VERSIONS, timeout=15).json()[0]
    data = requests.get(DDRAGON_CHAMPS.format(ver=ver), timeout=15).json()["data"]
    tags_by_id: dict[int, set[str]] = {}
    enchanter_ids: set[int] = set()
    for champ in data.values():
        cid = int(champ["key"])
        tags_by_id[cid] = set(champ.get("tags", []))
        if champ["name"] in ENCHANTERS:
            enchanter_ids.add(cid)
    missing = ENCHANTERS - {data[c]["name"] for c in data}
    if missing:
        print(f"  warning: enchanters not found in Data Dragon: {sorted(missing)}",
              file=sys.stderr)
    return tags_by_id, enchanter_ids


def classify(champion_id: int, tags_by_id: dict[int, set[str]],
             enchanter_ids: set[int]) -> str:
    if champion_id in enchanter_ids:
        return "enchanter"
    if "Tank" in tags_by_id.get(champion_id, set()):
        return "tank"
    return "carry"


# --------------------------------------------------------------------------- #
# Match collection                                                            #
# --------------------------------------------------------------------------- #

def resolve_puuid(client: RiotClient, riot_id: str) -> Optional[str]:
    if "#" not in riot_id:
        print(f"  bad Riot ID (need name#tag): {riot_id}", file=sys.stderr)
        return None
    name, tag = riot_id.rsplit("#", 1)
    data = client.get(f"{BASE}/riot/account/v1/accounts/by-riot-id/{name}/{tag}")
    return data["puuid"] if data else None


def match_ids_for(client: RiotClient, puuid: str) -> list[str]:
    ids: list[str] = []
    start = 0
    while len(ids) < MAX_MATCHES_PER_PLAYER:
        params = {"queue": QUEUE_ARAM, "start": start, "count": 100}
        if START_TIME is not None:
            params["startTime"] = START_TIME
        if END_TIME is not None:
            params["endTime"] = END_TIME
        page = client.get(
            f"{BASE}/lol/match/v5/matches/by-puuid/{puuid}/ids", params=params
        )
        if not page:
            break
        ids.extend(page)
        if len(page) < 100:
            break
        start += 100
    return ids[:MAX_MATCHES_PER_PLAYER]


def fetch_match(client: RiotClient, match_id: str) -> Optional[dict]:
    cached = CACHE_DIR / f"{match_id}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    data = client.get(f"{BASE}/lol/match/v5/matches/{match_id}")
    if data:
        CACHE_DIR.mkdir(exist_ok=True)
        cached.write_text(json.dumps(data))
    return data


# --------------------------------------------------------------------------- #
# Scoring                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class GameRecord:
    puuid: str
    date: str
    champion: str
    archetype: str
    win: bool
    kda: float
    kill_participation: float
    team_damage_pct: float
    damage_taken_pct: float
    heal_shield_pct: float
    primary: float                 # the archetype's primary metric value
    contrib_pct: float = 0.0       # filled in pass 2
    game_score: float = 0.0        # filled in pass 2


def in_window(info: dict) -> bool:
    """True if the match falls within [START_TIME, END_TIME].

    Applied to every match in BOTH the API and offline paths, so stale cached
    matches outside the window (e.g. last year's games) are never scored even
    though their JSON still sits in match_cache/.
    """
    ts = (info.get("gameEndTimestamp") or info.get("gameCreation", 0)) / 1000
    if START_TIME is not None and ts < START_TIME:
        return False
    if END_TIME is not None and ts > END_TIME:
        return False
    return True


def build_game_record(part: dict, info: dict, archetype: str,
                      heal_shield_pct: float) -> Optional[GameRecord]:
    ch = part.get("challenges")
    if not ch or "teamDamagePercentage" not in ch:
        return None  # remake / pre-challenges data; can't score fairly
    k, d, a = part["kills"], part["deaths"], part["assists"]
    primary = {
        "carry": ch.get("teamDamagePercentage", 0.0),
        "tank": ch.get("damageTakenOnTeamPercentage", 0.0),
        "enchanter": heal_shield_pct,
    }[archetype]
    ts = info.get("gameEndTimestamp") or info.get("gameCreation", 0)
    return GameRecord(
        puuid=part["puuid"],
        date=datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        champion=part.get("championName", "Unknown"),
        archetype=archetype,
        win=bool(part["win"]),
        kda=round((k + a) / max(d, 1), 2),
        kill_participation=round(ch.get("killParticipation", 0.0), 4),
        team_damage_pct=round(ch.get("teamDamagePercentage", 0.0), 4),
        damage_taken_pct=round(ch.get("damageTakenOnTeamPercentage", 0.0), 4),
        heal_shield_pct=round(heal_shield_pct, 4),
        primary=primary,
    )


def heal_shield_share(part: dict, participants: list[dict]) -> float:
    team_id = part["teamId"]
    mine = part.get("challenges", {}).get("effectiveHealAndShielding", 0.0)
    total = sum(
        p.get("challenges", {}).get("effectiveHealAndShielding", 0.0)
        for p in participants if p["teamId"] == team_id
    )
    return mine / total if total > 0 else 0.0


def percentile_ranks(values: list[float]) -> list[float]:
    """Fraction of the population each value beats (ties share credit)."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    out = []
    for v in values:
        below = sum(1 for x in values if x < v)
        equal = sum(1 for x in values if x == v)
        out.append((below + 0.5 * equal) / n)
    return out


def score_games(records: list[GameRecord]) -> None:
    """Pass 2: contribution percentile within archetype, then game score."""
    by_arch: dict[str, list[GameRecord]] = defaultdict(list)
    for r in records:
        by_arch[r.archetype].append(r)
    for group in by_arch.values():
        pcts = percentile_ranks([r.primary for r in group])
        for r, p in zip(group, pcts):
            r.contrib_pct = round(p, 4)
            r.game_score = round(
                W_WIN * (1.0 if r.win else 0.0)
                + W_CONTRIB * r.contrib_pct
                + W_KP * r.kill_participation,
                4,
            )


# --------------------------------------------------------------------------- #
# Aggregation -> snapshot                                                      #
# --------------------------------------------------------------------------- #

def aggregate(records: list[GameRecord], riot_ids: dict[str, str],
              icons: dict[str, int]) -> list[dict]:
    by_player: dict[str, list[GameRecord]] = defaultdict(list)
    for r in records:
        by_player[r.puuid].append(r)

    players = []
    for puuid, games in by_player.items():
        n = len(games)
        wins = sum(1 for g in games if g.win)
        win_rate = wins / n
        mean_contrib = sum(g.contrib_pct for g in games) / n
        mean_kp = sum(g.kill_participation for g in games) / n
        win_pts = round(100 * W_WIN * win_rate, 1)
        contrib_pts = round(100 * W_CONTRIB * mean_contrib, 1)
        kp_pts = round(100 * W_KP * mean_kp, 1)

        arch_groups: dict[str, list[GameRecord]] = defaultdict(list)
        for g in games:
            arch_groups[g.archetype].append(g)
        archetypes = {
            arch: {
                "games": len(gs),
                "meanContribPct": round(sum(x.contrib_pct for x in gs) / len(gs), 3),
            }
            for arch, gs in arch_groups.items()
        }
        most_played = max(arch_groups, key=lambda k: len(arch_groups[k]))

        recent = sorted(games, key=lambda g: g.date, reverse=True)[:GAMELOG_SIZE]
        game_log = [{
            "date": g.date,
            "champion": g.champion,
            "archetype": g.archetype,
            "win": g.win,
            "gameScore": g.game_score,
            "teamDamagePct": g.team_damage_pct,
            "damageTakenPct": g.damage_taken_pct,
            "healShieldPct": g.heal_shield_pct,
            "kda": g.kda,
            "killParticipation": round(g.kill_participation, 3),
        } for g in recent]

        players.append({
            "riotId": riot_ids.get(puuid, puuid),
            "profileIconId": icons.get(puuid, 0),
            "games": n,
            "wins": wins,
            "losses": n - wins,
            "winRate": round(win_rate, 3),
            "rankScore": round(win_pts + contrib_pts + kp_pts, 1),
            "components": {
                "winPoints": win_pts,
                "contribPoints": contrib_pts,
                "kpPoints": kp_pts,
            },
            "avgKda": round(sum(g.kda for g in games) / n, 2),
            "avgKillParticipation": round(mean_kp, 3),
            "mostPlayedArchetype": most_played,
            "provisional": n < MIN_GAMES,
            "archetypes": archetypes,
            "gameLog": game_log,
        })

    # Ranked first (by score), provisional players after.
    players.sort(key=lambda p: (p["provisional"], -p["rankScore"]))
    return players


def build_games_array(records: list[GameRecord], riot_ids: dict[str, str]) -> list[dict]:
    """Flat per-game series for the dashboard's distribution charts.

    One row per scorable player-game (the full `records` list, NOT the
    truncated per-player gameLog). Only fields that form meaningful
    distributions are emitted: contribPct (uniform percentile) and win
    (binary) are intentionally excluded.
    """
    return [{
        "riotId": riot_ids.get(r.puuid, r.puuid),
        "archetype": r.archetype,
        "kp": round(r.kill_participation, 4),
        "kda": r.kda,
        "gameScore": r.game_score,
        "teamDamagePct": r.team_damage_pct,
        "damageTakenPct": r.damage_taken_pct,
        "healShieldPct": r.heal_shield_pct,
    } for r in records]


def write_snapshot(players: list[dict], total_games: int, games: list[dict],
                   records: list[GameRecord]) -> None:
    # Actual played-date range from the games themselves; fall back to the
    # explicit START_TIME/END_TIME bounds when set. Without this, an unset
    # START_TIME left window.from = "" and the dashboard rendered NaN.
    game_dates = [r.date for r in records if r.date]
    date_from = (datetime.fromtimestamp(START_TIME).strftime("%Y-%m-%d")
                 if START_TIME else (min(game_dates) if game_dates else ""))
    date_to = (datetime.fromtimestamp(END_TIME).strftime("%Y-%m-%d")
               if END_TIME else (max(game_dates) if game_dates
                                  else datetime.now().strftime("%Y-%m-%d")))
    snapshot = {
        "group": GROUP_NAME,
        "generatedAt": datetime.now().strftime("%Y-%m-%d"),
        "window": {
            "from": date_from,
            "to": date_to,
            "patch": PATCH_LABEL,
            "queue": "ARAM",
        },
        "totalGames": total_games,
        "weights": {
            "win": W_WIN, "contribution": W_CONTRIB, "killParticipation": W_KP,
        },
        "minGames": MIN_GAMES,
        "players": players,
        "games": games,
    }
    OUT_JSON.write_text(json.dumps(snapshot, indent=2))
    OUT_JS.write_text("const SNAPSHOT = " + json.dumps(snapshot, indent=2) + ";\n")
    print(f"\nWrote {OUT_JS} and {OUT_JSON} "
          f"({len(players)} players, {total_games} unique games).")


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def run_offline() -> None:
    """Rebuild the snapshot entirely from match_cache/ with no API calls.

    Member PUUIDs and Riot IDs are read from the cached match participants
    (riotIdGameName/riotIdTagline), so neither ACCOUNT-V1 nor MATCH-V5 listing
    is needed. Only Data Dragon (public, no key) is contacted, for archetypes.
    """
    print("OFFLINE mode: rebuilding from match_cache/ (no API calls)...")
    print("Loading champion archetypes from Data Dragon...")
    tags_by_id, enchanter_ids = load_champion_tags()

    files = sorted(CACHE_DIR.glob("*.json"))
    if not files:
        sys.exit(f"No cached matches in {CACHE_DIR}; offline rebuild needs the cache.")

    members_wanted = set(MEMBERS)
    # First pass: map riot_id -> puuid by scanning cached participants.
    member_puuids: dict[str, str] = {}   # puuid -> riot_id
    for f in files:
        match = json.loads(f.read_text())
        for part in match["info"]["participants"]:
            rid = f'{part.get("riotIdGameName","")}#{part.get("riotIdTagline","")}'
            if rid in members_wanted and part.get("puuid"):
                member_puuids[part["puuid"]] = rid
    missing = members_wanted - set(member_puuids.values())
    if missing:
        print(f"  warning: not found in cache: {sorted(missing)}", file=sys.stderr)
    if not member_puuids:
        sys.exit("No members found in cached matches; check MEMBERS / cache.")
    for puuid, rid in member_puuids.items():
        print(f"  {rid} -> {puuid[:12]}...")

    member_set = set(member_puuids)
    records: list[GameRecord] = []
    icons: dict[str, int] = {}
    for f in files:
        match = json.loads(f.read_text())
        info = match["info"]
        if info.get("gameDuration", 0) < MIN_GAME_DURATION:
            continue
        if not in_window(info):
            continue
        participants = info["participants"]
        for part in participants:
            if part["puuid"] not in member_set:
                continue
            icons[part["puuid"]] = part.get("profileIcon", 0)
            archetype = classify(part.get("championId", 0), tags_by_id, enchanter_ids)
            hs = heal_shield_share(part, participants)
            rec = build_game_record(part, info, archetype, hs)
            if rec:
                records.append(rec)

    if not records:
        sys.exit("No scorable games found in cache.")

    print(f"Scoring {len(records)} player-games...")
    score_games(records)
    players = aggregate(records, member_puuids, icons)
    games = build_games_array(records, member_puuids)
    write_snapshot(players, len(files), games, records)


def main() -> None:
    client = RiotClient(API_KEY)

    print("Loading champion archetypes from Data Dragon...")
    tags_by_id, enchanter_ids = load_champion_tags()

    print("Resolving members...")
    member_puuids: dict[str, str] = {}   # puuid -> riot_id
    for riot_id in MEMBERS:
        puuid = resolve_puuid(client, riot_id)
        if puuid:
            member_puuids[puuid] = riot_id
            print(f"  {riot_id} -> {puuid[:12]}...")
        else:
            print(f"  could not resolve {riot_id}", file=sys.stderr)
    if not member_puuids:
        sys.exit("No members resolved; check Riot IDs and REGION.")

    print("Collecting match IDs...")
    all_ids: set[str] = set()
    for puuid, riot_id in member_puuids.items():
        ids = match_ids_for(client, puuid)
        all_ids.update(ids)
        print(f"  {riot_id}: {len(ids)} ARAM games")
    print(f"  {len(all_ids)} unique matches to fetch")

    print("Fetching match detail (cached on disk)...")
    records: list[GameRecord] = []
    icons: dict[str, int] = {}
    member_set = set(member_puuids)
    fetched = 0
    for match_id in all_ids:
        match = fetch_match(client, match_id)
        fetched += 1
        if fetched % 25 == 0:
            print(f"  {fetched}/{len(all_ids)}")
        if not match:
            continue
        info = match["info"]
        if info.get("gameDuration", 0) < MIN_GAME_DURATION:
            continue
        if not in_window(info):
            continue
        participants = info["participants"]
        for part in participants:
            if part["puuid"] not in member_set:
                continue
            icons[part["puuid"]] = part.get("profileIcon", 0)
            archetype = classify(part.get("championId", 0), tags_by_id, enchanter_ids)
            hs = heal_shield_share(part, participants)
            rec = build_game_record(part, info, archetype, hs)
            if rec:
                records.append(rec)

    if not records:
        sys.exit("No scorable games found.")

    print(f"Scoring {len(records)} player-games...")
    score_games(records)
    players = aggregate(records, member_puuids, icons)
    games = build_games_array(records, member_puuids)
    write_snapshot(players, len(all_ids), games, records)


if __name__ == "__main__":
    if "--offline" in sys.argv or os.environ.get("ARAM_OFFLINE"):
        run_offline()
    else:
        main()
