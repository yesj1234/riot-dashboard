# ARAM Power Rankings

A one-shot snapshot dashboard that ranks the members of a friend group by their
League of Legends **ARAM** (queue 450) performance, using a custom role-fair
scoring standard.

The project has three parts:

1. **`aram_snapshot.py`** — pulls each member's ARAM matches from the Riot API,
   scores every game, aggregates per player, and writes `snapshot.js`.
2. **`snapshot.js`** *(generated)* — a single `const SNAPSHOT = {…}` object
   holding the scored, ranked data.
3. **`ARAM_Leaderboard.html`** — a self-contained dashboard that renders
   `SNAPSHOT`. No build step, no runtime API calls, no key.

Only the Python step ever touches the Riot API. The HTML is fully static.

---

## Why a custom ranking

ARAM has no roles — everyone is mid on one lane with a random champion — so a
per-role ranking has nothing to key off, and raw stats (damage, healing) just
rank players by the champion they happened to get rather than how well they
played it. The scoring standard below fixes that by judging each game on the
metric that fits the champion's job, then comparing players fairly across the
whole group.

## The ranking standard

For each **game**, each group member is scored:

1. **Archetype** is resolved from the champion played, in priority order:
   - **Enchanter** — champion is in the hand-maintained `ENCHANTERS` set.
   - **Tank** — champion has the `Tank` tag in Data Dragon.
   - **Carry** — everything else.

2. A **primary metric** is read for that archetype:

   | Archetype | Primary metric | Source |
   |-----------|----------------|--------|
   | Carry     | team damage share        | `challenges.teamDamagePercentage` |
   | Tank      | team damage-taken share  | `challenges.damageTakenOnTeamPercentage` |
   | Enchanter | team heal+shield share   | `effectiveHealAndShielding` ÷ team total |

3. **`contribPct`** is the percentile rank of that primary metric among **all
   snapshot games of the same archetype**. This makes the three archetypes
   comparable: a 70th-percentile carry game and a 70th-percentile enchanter game
   both score 0.70.

4. **Game score** (0–1):

   ```
   gameScore = 0.40·win + 0.45·contribPct + 0.15·killParticipation
   ```

For each **player**, `rankScore` is the mean of their game scores ×100, which
decomposes exactly into the three component bars the dashboard shows:

```
rankScore = 40·winRate + 45·meanContribPct + 15·meanKP
          = winPoints  + contribPoints     + kpPoints
```

Players with fewer than `MIN_GAMES` (default 10) are marked **provisional** and
listed separately rather than ranked.

---

## Setup

Requires Python 3.10+ and a Riot API key.

```bash
pip install requests
```

Get a key by signing in at <https://developer.riotgames.com> with your Riot
account. A free **development key** is enough for a one-off snapshot (it expires
every 24 hours, so just have it live while the script runs). Apply for a
**personal key** if you plan to regenerate snapshots regularly.

```bash
export RIOT_API_KEY="RGAPI-..."
```

The key is read from the environment only — it never lands in `snapshot.js` or
the HTML.

## Configuration

Edit the constants at the top of `aram_snapshot.py`:

- **`REGION`** — regional routing cluster: `asia` (KR/JP), `americas`
  (NA/BR/LAN/LAS/OCE), or `europe` (EUW/EUNE/TR/RU).
- **`MEMBERS`** — the group, as Riot IDs (`"gameName#tagLine"`).
- **`GROUP_NAME`** — shown in the dashboard header.
- **`START_TIME` / `END_TIME`** — optional epoch-second bounds for the window.
- **`MAX_MATCHES_PER_PLAYER`** — cap per member (default 200).
- **`W_WIN` / `W_CONTRIB` / `W_KP`** — scoring weights (default 0.40 / 0.45 / 0.15).
- **`MIN_GAMES`** — games required to be ranked (default 10).
- **`ENCHANTERS`** — champions scored as enchanters. Data Dragon's `Support` tag
  doesn't separate enchanters from engage supports, so this set is curated by
  hand; adjust it to your group's champion pool.

## Usage

```bash
python aram_snapshot.py
```

This resolves PUUIDs, pulls ARAM match IDs, dedupes them across members, fetches
each unique match once, scores everything, and writes `snapshot.js` (plus
`snapshot.json`).

Then load the data into the dashboard. Either:

- **Keep it a single file:** open `snapshot.js` and paste its contents over the
  inline `const SNAPSHOT = {…};` block near the top of the `<script>` in
  `ARAM_Leaderboard.html`, or
- **Easier refreshes:** delete that inline block and add
  `<script src="snapshot.js"></script>` just before the page's main script tag,
  so future re-runs only touch `snapshot.js`.

Open the HTML in any browser — no server needed.

## Caching

Raw match JSON is cached under `match_cache/`, so the slow part runs once. To
retune weights or the enchanter list, just rerun: the API calls are skipped and
scoring rebuilds from cache in seconds. Delete the folder to force a fresh pull.

---

## Data contract

`snapshot.js` defines `const SNAPSHOT` with this shape (the HTML reads only from
it):

```js
{
  group, generatedAt,
  window: { from, to, patch, queue },
  totalGames,
  weights: { win, contribution, killParticipation },
  minGames,
  players: [{
    riotId, profileIconId, games, wins, losses, winRate,
    rankScore,
    components: { winPoints, contribPoints, kpPoints },
    avgKda, avgKillParticipation, mostPlayedArchetype, provisional,
    archetypes: {           // only buckets the player actually played
      carry:     { games, meanContribPct },
      tank:      { games, meanContribPct },
      enchanter: { games, meanContribPct }
    },
    gameLog: [{             // most-recent games for the detail view
      date, champion, archetype, win, gameScore,
      teamDamagePct, damageTakenPct, healShieldPct, kda, killParticipation
    }]
  }]
}
```

## Files

| File | Description |
|------|-------------|
| `aram_snapshot.py`     | Collector + scorer (the only Riot API caller). |
| `ARAM_Leaderboard.html`| Static dashboard. |
| `snapshot.js`          | Generated: `const SNAPSHOT = {…};` for the dashboard. |
| `snapshot.json`        | Generated: same data as plain JSON. |
| `match_cache/`         | Generated: cached raw match JSON. |

## Caveats

- Games shorter than 5 minutes (remakes) and games missing the `challenges`
  block are skipped, since they can't be scored fairly.
- Archetype is inferred from champion identity, which is a good proxy in ARAM
  (no build choice) but imperfect for hybrid champions.
- The percentile baseline is per-archetype, pooled across the whole group, so it
  is most reliable when each archetype has a healthy number of games in the
  window.

Data: Riot Games API (ACCOUNT-V1, MATCH-V5) and Data Dragon. This project isn't
endorsed by Riot Games.
