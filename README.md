# PikBot — NBA research app

PikBot is a Flask research application. The model is **not calibrated**, and the project does not place wagers or guarantee results.

## Live NBA integration status

The `feature/nba-live-props-feed` branch adds `GET /api/nba/live-predictions` and a dashboard button. It intentionally returns HTTP 503 instead of substituting demo or historical lines whenever the provider, entitlement, game list, or historical dataset is unavailable.

To activate the integration, the hosting environment must have:
- `SPORTSDATAIO_API_KEY` (or `SPORTSDATA_API_KEY`): a valid provider credential. **Never commit it to GitHub.**
- `SPORTSDATAIO_SPORTSBOOK_GROUP`: the numeric sportsbook group specifically authorized by the provider for the player-props endpoint. Guessing a group does not work.
- `SPORTSDATAIO_NBA_GAME_IDS`: comma-separated **current** provider NBA game IDs. This is a temporary integration requirement, not automated discovery.
- `data/nba_history.csv`: licensed, current NBA player-game observations with columns `sport,player,stat,date,value`. Stat labels must match the live offers (e.g. `Points`, `Rebounds`, `Assists`), and dates must be UTC-compatible. The model requires at least 12 past games and rejects data older than 30 days.

**Not yet verified:** The actual SportsDataIO subscription entitlement, provider JSON schema, data licensing, and deployed hosting configuration. No authenticated live API calls were performed while developing this branch. The adapter only accepts an explicitly recognized schema with a fresh `Updated`/`UpdatedAt`/`LastUpdated` timestamp; other responses fail closed.

## Local tests

Install `requirements.txt` and run `python -m unittest discover -s tests`.

## API

- `GET /health`: app liveness
- `GET /api/sportsdata-test`: provider teams endpoint test (does **not** establish player-props entitlement)
- `GET /api/live-props-status`: configuration-only readiness; does not claim an active live feed
- `GET /api/nba/live-predictions`: live offer retrieval and research estimates when all dependencies are present
- `POST /api/analyze`: analyze user-provided history and offer CSV files

**Caution:** Historical backtests are not independently validated. Two-leg probability estimates assume independence, which may be false.
