# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A one-shot ARAM (League of Legends queue 450) power-ranking dashboard for a friend group. A Python collector hits the Riot API, scores every game with a custom role-fair standard, and emits a `const SNAPSHOT = {…}` object. Two self-contained HTML files render that object. **Only the Python step touches the Riot API; the HTML is fully static** (no build step, no runtime API calls, no key embedded).

## Commands

```bash
source venv/bin/activate          # deps: requests (already in venv/)
export RIOT_API_KEY="RGAPI-..."   # 24h dev key is enough for a one-off run
python aram_snapshot.py           # writes snapshot.js + snapshot.json
open "ARAM Leaderboard.html"      # or the Report Card variant — no server needed
```

There are no tests, linter, or build step. The Python script regenerates from `match_cache/` in seconds (API calls skipped), so re-running after a scoring/weight change is cheap. Delete `match_cache/` to force a fresh API pull.

## Architecture

The whole system is a single data contract — `const SNAPSHOT` — produced by `aram_snapshot.py` and consumed by the HTML. Understanding the pipeline and that contract is the key to working here.

**Pipeline (`aram_snapshot.py`, top to bottom):**
1. `RiotClient` — rate-limited (18/s, 95/120s) Riot API wrapper with 429/5xx retry. ACCOUNT-V1 and MATCH-V5 both use *regional* routing (`REGION` = asia/americas/europe), not platform routing.
2. `load_champion_tags` / `classify` — Data Dragon gives champion tags; archetype is resolved **enchanter → tank → carry** in that priority. Enchanters come from the hand-curated `ENCHANTERS` set (Data Dragon's `Support` tag can't distinguish enchanters from engage supports).
3. Collection — resolve PUUIDs, page match IDs per member, **dedupe across members** so a shared game is fetched once. `fetch_match` caches raw JSON to `match_cache/{matchId}.json`.
4. **Two-pass scoring** — pass 1 (`build_game_record`) reads each player-game's archetype-specific primary metric; pass 2 (`score_games`) computes `contribPct` as the percentile of that primary *within its archetype pool across all snapshot games*, then the game score. The percentile is what makes carries, tanks, and enchanters comparable.
5. `aggregate` — per-player means → `rankScore` and its three component points; sorts ranked players by score, provisional (< `MIN_GAMES`) last.
6. `write_snapshot` — emits `snapshot.js` (`const SNAPSHOT = …;`) and `snapshot.json` (same data, plain JSON).

**The scoring formula is load-bearing and duplicated by design:**

```
gameScore = W_WIN·win + W_CONTRIB·contribPct + W_KP·killParticipation   (0.40 / 0.45 / 0.15)
rankScore = 100·mean(gameScore) = 40·winRate + 45·meanContribPct + 15·meanKP
          = winPoints + contribPoints + kpPoints   (the three bars the dashboard shows)
```

This decomposition is exact: the component points always sum to `rankScore`. If you change `W_WIN/W_CONTRIB/W_KP` in the Python, the HTML reads them back from `SNAPSHOT.weights` for its methodology text — but the **45/40/15 split is what makes the component breakdown sum correctly**, so keep weights and the aggregation math in sync.

**The two HTML files are two themed renderings of the same `SNAPSHOT`:**
- `ARAM Leaderboard.html` — English "Power Rankings".
- `ARAM Leaderboard - Report Card.html` — Korean 성적표 variant; adds letter grades (`gradeOf`, 수/우/미/양/가 by rank percentile) and generated teacher-style remarks (`remarkFor`).

Both `<script src="snapshot.js">` at the bottom and share the same render functions (`renderBody`, `detailHTML`, `donut`, `sparkline`, per-archetype boards) and the `ARCH` archetype color/label map. The detail panel (`detailHTML`) also renders a **Distribution** section: per-metric KDE curves (`kde`, `distCurve`, `distSection`, `DIST_METRICS`) drawn from `SNAPSHOT.games`, marking where the expanded player's mean falls in the group; share metrics are faceted by archetype, and the bounded KDE reflects at both 0 and 1. **Any change to the `SNAPSHOT` shape must be applied to both HTML files.** They read from `SNAPSHOT` only — never re-fetch or recompute scores.

## The SNAPSHOT data contract

`aggregate`/`write_snapshot` produce it; both HTML files consume it. Changing a field on either side without the other breaks rendering silently.

```js
{ group, generatedAt, window:{from,to,patch,queue}, totalGames,
  weights:{win,contribution,killParticipation}, minGames,
  players:[{ riotId, profileIconId, games, wins, losses, winRate, rankScore,
    components:{winPoints,contribPoints,kpPoints},
    avgKda, avgKillParticipation, mostPlayedArchetype, provisional,
    archetypes:{ carry?:{games,meanContribPct}, tank?:…, enchanter?:… },  // only buckets played
    gameLog:[{date,champion,archetype,win,gameScore,teamDamagePct,
              damageTakenPct,healShieldPct,kda,killParticipation}] }],  // most-recent GAMELOG_SIZE
  games:[{ riotId, archetype, kp, kda, gameScore,        // full per-game series (one row per
           teamDamagePct, damageTakenPct, healShieldPct }] }  // player-game) for the Distribution charts
```

`SNAPSHOT.games` is the **full** per-game series (one row per scorable player-game, ~698 today — distinct from `totalGames`, which counts unique matches), built by `build_games_array` from the pre-truncation `records` list. It powers the Distribution section's KDE curves. It intentionally omits `contribPct` (a within-archetype percentile, ~uniform → useless as a distribution) and `win` (binary). Like the rest of `SNAPSHOT`, any shape change must be applied to **both** HTML files.

## Config & conventions

- Group/region/window config lives in the constants block at the top of `aram_snapshot.py` (`MEMBERS` as `name#tag`, `REGION`, `START_TIME/END_TIME`, `MAX_MATCHES_PER_PLAYER`, `ENCHANTERS`, weights, `MIN_GAMES`). Edit `ENCHANTERS` to match the group's champion pool.
- Games < `MIN_GAME_DURATION` (300s, remakes) and games missing the `challenges` block are skipped — they can't be scored fairly.
- The Riot key is read from `RIOT_API_KEY` only and never written into output files.
- `launch.json` in the repo root is an unrelated Django VS Code debug config (references `manage.py`/`app.settings` that don't exist here) — not part of this project; ignore it.

Data: Riot Games API (ACCOUNT-V1, MATCH-V5) and Data Dragon. Not endorsed by Riot Games.
