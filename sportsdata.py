import os
import requests

BASE_URLS = {
    "NBA": "https://api.sportsdata.io/v3/nba",
    "WNBA": "https://api.sportsdata.io/v3/wnba",
    "NFL": "https://api.sportsdata.io/v3/nfl",
    "MLB": "https://api.sportsdata.io/v3/mlb",
    "NHL": "https://api.sportsdata.io/v3/nhl",
    "SOCCER": "https://api.sportsdata.io/v3/soccer",
    "NCAAF": "https://api.sportsdata.io/v3/cfb",
    "NCAAB": "https://api.sportsdata.io/v3/cbb",
}


def get_sports_data(sport, endpoint):
    sport = sport.upper()

    if sport not in BASE_URLS:
        raise ValueError("Unsupported sport")

    if not endpoint or endpoint.startswith("/"):
        raise ValueError("Invalid endpoint")

    api_key = os.environ.get("SPORTSDATA_API_KEY")

    if not api_key:
        raise ValueError("SportsDataIO API key missing")

    url = BASE_URLS[sport] + "/" + endpoint

    response = requests.get(
        url,
        headers={"Ocp-Apim-Subscription-Key": api_key},
        timeout=20
    )

    response.raise_for_status()
    return response.json()


def test_connection():
    try:
        data = get_sports_data("NBA", "scores/json/Teams")

        return {
            "connected": True,
            "sport": "NBA",
            "records": len(data)
        }

    except requests.exceptions.HTTPError as error:
        return {
            "connected": False,
            "error": "SportsDataIO rejected the request",
            "status": error.response.status_code
        }

    except Exception as error:
        return {
            "connected": False,
            "error": str(error)
        }
