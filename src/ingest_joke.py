"""
Fetches a random dad joke from icanhazdadjoke.com and stores it for display
next to the app's title. Free, no API key required -- their only ask is a
descriptive User-Agent header identifying the calling project.

If the fetch fails, the previously stored joke (if any) is left in place
rather than cleared, so the app always has something to show as long as at
least one fetch has ever succeeded. The workflow step calling this uses
continue-on-error, so a joke-API hiccup never blocks the rest of ingestion.

Usage: python -m src.ingest_joke
"""

import requests
from datetime import datetime, timezone
from src.db import get_conn, init_db

JOKE_API_URL = "https://icanhazdadjoke.com/"
USER_AGENT = "Bo6's News Aggregator (local news aggregator project)"


def run_ingest_joke():
    init_db()
    try:
        resp = requests.get(
            JOKE_API_URL,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        joke = resp.json().get("joke")
        if not joke:
            print("FAIL Dad joke - no joke in response")
            return
    except requests.RequestException as e:
        print(f"FAIL Dad joke - {e}")
        return

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES ('daily_joke', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (joke, datetime.now(timezone.utc).isoformat()),
        )

    print(f"OK   Dad joke - {joke}")


if __name__ == "__main__":
    run_ingest_joke()
