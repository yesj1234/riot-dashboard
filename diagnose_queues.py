#!/usr/bin/env python3
"""Diagnostic: does MATCH-V5 actually return queue 2400 (ARAM: Mayhem / augment)
matches at all?

queues.json lists 2400 as a live queue (notes=None), but that doesn't prove the
match-history endpoint serves it. This probes, for each member, the raw ID list
for queue 2400 vs 450 (and a no-filter recent sample whose queueIds we tally),
so we can tell "nobody in this group played 2400" apart from "the API doesn't
expose 2400 at all." Read-only; writes nothing.
"""
from __future__ import annotations
import sys
from collections import Counter
from aram_snapshot import RiotClient, MEMBERS, BASE, API_KEY, resolve_puuid


def ids(client: RiotClient, puuid: str, queue: int | None, count: int) -> list[str]:
    params = {"start": 0, "count": count}
    if queue is not None:
        params["queue"] = queue
    page = client.get(f"{BASE}/lol/match/v5/matches/by-puuid/{puuid}/ids", params=params)
    return page or []


def main() -> None:
    client = RiotClient(API_KEY)
    seen_queues = Counter()  # across a no-filter recent sample, all members

    for riot_id in MEMBERS:
        puuid = resolve_puuid(client, riot_id)
        if not puuid:
            print(f"  could not resolve {riot_id}", file=sys.stderr)
            continue
        n2400 = len(ids(client, puuid, 2400, 100))
        n450 = len(ids(client, puuid, 450, 100))
        # No-filter recent 30: tally their queueIds so we SEE what they actually play
        recent = ids(client, puuid, None, 30)
        for mid in recent[:30]:
            m = client.get(f"{BASE}/lol/match/v5/matches/{mid}")
            if m:
                seen_queues[m["info"].get("queueId")] += 1
        print(f"  {riot_id}: q2400_ids={n2400}  q450_ids={n450}")

    print("\n=== queueIds actually seen in members' recent no-filter sample ===")
    for q, n in seen_queues.most_common():
        print(f"  queue {q}: {n}")
    print("\nReading: if q2400_ids is 0 for everyone AND 2400 never appears in the "
          "no-filter sample, this group simply hasn't played ARAM: Mayhem. The "
          "endpoint itself accepts queue=2400 (no error = supported), so absence "
          "means no games, not 'API doesn't provide it'.")


if __name__ == "__main__":
    main()
