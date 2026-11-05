# Today's Sports setup

Set `ODDS_API_KEY` in Render. The key must be authorized for the PrizePicks bookmaker.
Set `PIKBOT_LIVE_HISTORY_DIR` to a directory of regularly refreshed, authorized player-game CSVs, one per Odds API sport key. Use columns `sport,player,stat,date,value`. Player names and stat codes must match today.py. Keep history current and include at least 12 qualifying observations for each player and stat.

Today's slate uses Eastern Time. The first version covers NBA, WNBA, college basketball, NFL, college football, MLB and NHL when the provider lists them. To limit starter quota use, it queries one upcoming event per league and caches each event's odds per process for an hour. This is not an all-game or in-play feed. The free trial analyzes at most five matching lines per request.

Verify actual PrizePicks entitlement, provider freshness fields, and each league's history matches before deploying.

## Automatic history

NBA uses BALLDONTLIE when a local history CSV is absent. Set `BALLDONTLIE_API_KEY` in Render after enabling NBA Game Player Stats access. The adapter checks up to eight unique players per request.
NFL uses free weekly player stats and completed schedules from [nflverse](https://github.com/nflverse/nflverse-data), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). PikBot joins games to player stats and converts those stats into projection inputs. NFL history refreshes every six hours per running process.

## College stats

Both leagues use the shared `COLLEGE_DATA_API_KEY`. The first request with current lines starts a background import of up to 365 days of completed games. Once loaded, requests can trigger a seven-day incremental refresh once per day per app process. A failed refresh retries after six hours. The default cache is ephemeral, so restarts can consume additional provider quota. A persistent `PIKBOT_COLLEGE_CACHE_DIR` avoids reimporting on restart. Use one app worker with the free key.
