#!/usr/bin/env python3
"""One-shot diagnostic: what queues have the members played recently?

The main collector filters to QUEUE_ARAM = 450 (classic Howling Abyss). If the
group has been playing the 2026 "augment ARAM" mode, it likely sits under a
different queueId and is invisible to a 450-only pull. This script fetches each
member's most recent matches WITHOUT a queue filter and prints the queueId
distribution (overall and for 2026), so we can identify the right id to add.

Run with a valid RIOT_API_KEY. Reuses RiotClient/MEMBERS/REGION from
aram_snapshot.py — read-only, writes nothing.
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone

from aram_snapshot import (
    RiotClient, MEMBERS, BASE, API_KEY, resolve_puuid,
)

RECENT_PER_MEMBER = 60   # most-recent matches to sample per member (no queue filter)


def recent_match_ids(client: RiotClient, puuid: str, count: int) -> list[str]:
    ids: list[str] = []
    start = 0
    while len(ids) < count:
        page = client.get(
            f"{BASE}/lol/match/v5/matches/by-puuid/{puuid}/ids",
            params={"start": start, "count": min(100, count - len(ids))},
        )
        if not page:
            break
        ids.extend(page)
        if len(page) < 100:
            break
        start += len(page)
    return ids[:count]


def main() -> None:
    client = RiotClient(API_KEY)
    overall = Counter()
    y2026 = Counter()
    # queueId -> a sample gameMode/gameType label for context
    labels: dict[int, str] = {}

    print(f"Sampling up to {RECENT_PER_MEMBER} recent matches per member "
          f"(no queue filter)...\n")
    for riot_id in MEMBERS:
        puuid = resolve_puuid(client, riot_id)
        if not puuid:
            print(f"  could not resolve {riot_id}", file=sys.stderr)
            continue
        ids = recent_match_ids(client, puuid, RECENT_PER_MEMBER)
        per_member = Counter()
        for mid in ids:
            m = client.get(f"{BASE}/lol/match/v5/matches/{mid}")
            if not m:
                continue
            info = m["info"]
            qid = info.get("queueId")
            per_member[qid] += 1
            overall[qid] += 1
            labels.setdefault(qid, f'{info.get("gameMode","?")}/{info.get("gameType","?")}')
            ts = (info.get("gameEndTimestamp") or info.get("gameCreation", 0)) / 1000
            if ts and datetime.fromtimestamp(ts, tz=timezone.utc).year >= 2026:
                y2026[qid] += 1
        print(f"  {riot_id}: {dict(per_member)}")

    print("\n=== queueId distribution (all sampled) ===")
    for qid, n in overall.most_common():
        print(f"  queue {qid}: {n}  ({labels.get(qid,'')})")
    print("\n=== queueId distribution (2026 only) ===")
    if y2026:
        for qid, n in y2026.most_common():
            print(f"  queue {qid}: {n}  ({labels.get(qid,'')})")
    else:
        print("  (no 2026 matches in the sample)")
    print("\nNote: queue 450 = classic ARAM. Any OTHER id with ARAM-like "
          "gameMode in 2026 is the augment-ARAM queue to add.")


if __name__ == "__main__":
    main()
