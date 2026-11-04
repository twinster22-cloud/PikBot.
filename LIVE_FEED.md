# Today's Sports setup

Set `ODDS_API_KEY` in Render. The key must be authorized for the PrizePicks bookmaker.
Set `PIKBOT_LIVE_HISTORY_DIR` to a directory of regularly refreshed, authorized player-game CSVs, one per Odds API sport key. Use columns `sport,player,stat,date,value`. Player names and stat codes must match today.py. Keep history current and include at least 12 qualifying observations for each player and stat.

Today's slate uses Eastern Time. The first version covers NBA, WNBA, college basketball, NFL, college football, MLB and NHL when the provider lists them. To limit starter quota use, it queries one upcoming event per league and caches each event's odds per process for an hour. This is not an all-game or in-play feed. The free trial analyzes at most five matching lines per request.

Verify actual PrizePicks entitlement, provider freshness fields, and each league's history matches before deploying.
