"""
Checks every source's feed_url to confirm it's a live, parseable RSS/Atom
feed before you rely on it in ingest.py. Run this whenever you add a new
source or periodically to catch feeds that moved or were discontinued.

Usage: python -m src.verify_feeds
"""

import feedparser
import requests
from src.db import get_conn, init_db
from src.load_config import load_taxonomy, load_sources

USER_AGENT = "LocalNewsAggregator/0.1 (+contact: you@example.com)"


def verify_all():
    init_db()
    load_taxonomy()
    load_sources()

    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, feed_url FROM sources").fetchall()

    results = []
    for row in rows:
        status, detail = check_feed(row["feed_url"])
        results.append((row["name"], row["feed_url"], status, detail))
        with get_conn() as conn:
            conn.execute(
                "UPDATE sources SET status = ? WHERE id = ?",
                (status, row["id"]),
            )

    print(f"{'STATUS':<12} {'SOURCE':<35} DETAIL")
    print("-" * 90)
    for name, url, status, detail in results:
        print(f"{status:<12} {name:<35} {detail}")

    dead = [r for r in results if r[2] != "active"]
    if dead:
        print(f"\n{len(dead)} feed(s) need attention -- update or remove them in config/sources.yaml.")


def check_feed(feed_url: str):
    try:
        resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if resp.status_code != 200:
            return "dead", f"HTTP {resp.status_code}"
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            return "dead", f"Parse error: {parsed.bozo_exception}"
        if not parsed.entries:
            return "needs_review", "Parsed OK but 0 entries"
        return "active", f"{len(parsed.entries)} entries OK"
    except requests.RequestException as e:
        return "dead", f"Request failed: {e}"


if __name__ == "__main__":
    verify_all()
