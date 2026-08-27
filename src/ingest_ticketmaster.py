"""
Pulls Wisconsin concert listings from the Ticketmaster Discovery API and
stores them alongside RSS-sourced articles in the same database.

Unlike ingest.py, this is a JSON API, not RSS -- kept as a separate script
since the data shape and fetching logic are different enough to not share
much code with the feedparser-based pipeline.

Requires the TICKETMASTER_API_KEY environment variable (set as a GitHub
Actions secret, not committed to the repo). If the key is missing or the
API call fails, this script exits cleanly without touching the database --
the workflow step that calls this uses continue-on-error, so a Ticketmaster
outage never blocks the rest of the ingest pipeline (Freeman, Packers, etc.
still get committed normally).

Usage: python -m src.ingest_ticketmaster
"""

import os
import requests
from datetime import datetime, timezone
from src.db import get_conn, init_db

API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
SOURCE_NAME = "Ticketmaster - Wisconsin Concerts"
SOURCE_FEED_URL = "ticketmaster-api://wisconsin-concerts"  # not a real URL -- just a stable unique identifier for the sources table
SOURCE_STATUS = "api_source"  # distinct from 'active' so ingest.py's RSS loop and verify_feeds.py both skip this row


def ensure_source_exists():
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sources (name, base_url, feed_url, default_region, status)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(feed_url) DO UPDATE SET status=excluded.status""",
            (SOURCE_NAME, "https://www.ticketmaster.com/", SOURCE_FEED_URL, "wisconsin", SOURCE_STATUS),
        )
        row = conn.execute("SELECT id FROM sources WHERE feed_url = ?", (SOURCE_FEED_URL,)).fetchone()

        conn.execute("DELETE FROM source_news_types WHERE source_id = ?", (row["id"],))
        conn.execute(
            "INSERT INTO source_news_types (source_id, news_type_slug) VALUES (?, ?)",
            (row["id"], "events"),
        )
        return row["id"]


def format_event_datetime(dates: dict):
    """Return an ISO8601-ish string for the event's start time, or None."""
    start = dates.get("start", {})
    if start.get("dateTime"):
        return start["dateTime"]
    if start.get("localDate"):
        # No exact time given -- use midnight local as a placeholder so it
        # still sorts correctly by date.
        local_time = start.get("localTime", "00:00:00")
        return f"{start['localDate']}T{local_time}"
    return None


def format_display_summary(event: dict) -> str:
    venues = event.get("_embedded", {}).get("venues", [{}])
    venue = venues[0] if venues else {}
    venue_name = venue.get("name", "Venue TBA")
    city = venue.get("city", {}).get("name", "")
    start = event.get("dates", {}).get("start", {})
    date_str = start.get("localDate", "")
    time_str = start.get("localTime", "")
    when = date_str
    if time_str:
        when = f"{date_str} {time_str[:5]}"
    location = f"{venue_name}, {city}" if city else venue_name
    return f"{location} — {when}"


def run_ingest_ticketmaster():
    api_key = os.environ.get("TICKETMASTER_API_KEY")
    if not api_key:
        print("SKIP Ticketmaster - TICKETMASTER_API_KEY not set")
        return

    init_db()
    source_id = ensure_source_exists()

    try:
        resp = requests.get(
            API_URL,
            params={
                "apikey": api_key,
                "stateCode": "WI",
                "classificationName": "Music",
                "size": 100,
                "sort": "date,asc",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"FAIL Ticketmaster - {e}")
        return

    events = data.get("_embedded", {}).get("events", [])
    if not events:
        print("OK   Ticketmaster - Wisconsin Concerts     +0 new (0 events returned)")
        return

    new_count = 0
    with get_conn() as conn:
        for event in events:
            url = event.get("url")
            title = event.get("name")
            if not url or not title:
                continue

            existing = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
            published_at = format_event_datetime(event.get("dates", {}))
            summary = format_display_summary(event)
            images = event.get("images", [])
            image_url = images[0]["url"] if images else None

            if existing:
                article_id = existing["id"]
                conn.execute(
                    "UPDATE articles SET title=?, summary=?, image_url=?, published_at=? WHERE id=?",
                    (title, summary, image_url, published_at, article_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO articles
                       (source_id, title, url, summary, image_url, published_at, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, title, url, summary, image_url, published_at,
                     datetime.now(timezone.utc).isoformat()),
                )
                article_id = cur.lastrowid
                new_count += 1

            conn.execute("DELETE FROM article_news_types WHERE article_id = ?", (article_id,))
            conn.execute(
                "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                (article_id, "events"),
            )
            conn.execute("DELETE FROM article_regions WHERE article_id = ?", (article_id,))
            conn.execute(
                "INSERT OR IGNORE INTO article_regions (article_id, region_slug) VALUES (?, ?)",
                (article_id, "wisconsin"),
            )

        conn.execute(
            "UPDATE sources SET last_fetched_at = ?, last_error = NULL WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), source_id),
        )

    print(f"OK   Ticketmaster - Wisconsin Concerts     +{new_count} new ({len(events)} total returned)")


if __name__ == "__main__":
    run_ingest_ticketmaster()
