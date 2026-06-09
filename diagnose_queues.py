#!/usr/bin/env python3
"""Diagnostic: how many ARAM-family games per member per queue, including the
augment-ARAM queue 2400 ("ARAM: Mayhem"), with year breakdown.

Queue 2400 = ARAM: Mayhem (augment Howling Abyss). 450 = classic ARAM.
720 = ARAM Clash. This counts each, by year, per member — no scoring, no writes.
"""
from __future__ import annotations
import sys
from collections import Counter
from datetime import datetime, timezone
from aram_snapshot import RiotClient, MEMBERS, BASE, API_KEY, resolve_puuid

ARAM_QUEUES = {450: "classic ARAM", 2400: "ARAM: Mayhem (augment)", 720: "ARAM Clash"}
CAP = 200  # per member per queue


def ids_for_queue(client: RiotClient, puuid: str, queue: int, cap: int) -> list[str]:
    ids, start = [], 0
    while len(ids) < cap:
        page = client.get(f"{BASE}/lol/match/v5/matches/by-puuid/{puuid}/ids",
                          params={"queue": queue, "start": start, "count": 100})
        if not page:
            break
        ids.extend(page)
        if len(page) < 100:
            break
        start += 100
    return ids[:cap]


def main() -> None:
    client = RiotClient(API_KEY)
    grand = Counter()
    grand2026 = Counter()
    for riot_id in MEMBERS:
        puuid = resolve_puuid(client, riot_id)
        if not puuid:
            print(f"  could not resolve {riot_id}", file=sys.stderr)
            continue
        line = [f"{riot_id}:"]
        for q, label in ARAM_QUEUES.items():
            ids = ids_for_queue(client, puuid, q, CAP)
            n = len(ids)
            grand[q] += n
            # year breakdown needs match detail; sample only if there are games
            n2026 = 0
            for mid in ids:
                m = client.get(f"{BASE}/lol/match/v5/matches/{mid}")
                if not m:
                    continue
                info = m["info"]
                ts = (info.get("gameEndTimestamp") or info.get("gameCreation", 0)) / 1000
                if ts and datetime.fromtimestamp(ts, tz=timezone.utc).year >= 2026:
                    n2026 += 1
            grand2026[q] += n2026
            line.append(f"q{q}={n}(2026:{n2026})")
        print("  " + "  ".join(line))

    print("\n=== TOTAL by ARAM queue ===")
    for q, label in ARAM_QUEUES.items():
        print(f"  queue {q} [{label}]: {grand[q]} total, {grand2026[q]} in 2026")
