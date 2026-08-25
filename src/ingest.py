"""
Polls all active sources' RSS feeds and stores new articles.

Design notes:
- Only headline, short summary/excerpt, thumbnail image, and link are
  stored -- never full article text. This keeps the app well within
  standard aggregator fair use (same pattern as Google News / Apple News)
  and avoids copyright exposure. See README for more on this.
- Dedup is by article URL (unique constraint) -- re-running ingest is safe.
- Each article inherits its source's default news types and region. If a
  feed covers multiple topics, split it into separate source entries in
  sources.yaml rather than trying to auto-classify content here.

Usage: python -m src.ingest
"""

import feedparser
import requests
import time
from datetime import datetime, timezone
from time import mktime
from src.db import get_conn, init_db

USER_AGENT = "LocalNewsAggregator/0.1 (+contact: you@example.com)"
MAX_SUMMARY_CHARS = 400
DELAY_BETWEEN_REQUESTS_SECONDS = 2


def run_ingest():
    init_db()
    with get_conn() as conn:
        sources = conn.execute(
            "SELECT id, name, feed_url, default_region, sports_scope FROM sources WHERE status = 'active'"
        ).fetchall()

    total_new = 0
    for source in sources:
        try:
            new_count = ingest_source(source)
            total_new += new_count
            print(f"OK   {source['name']:<40} +{new_count} new")
        except Exception as e:
            print(f"FAIL {source['name']:<40} {e}")
            with get_conn() as conn:
                conn.execute(
                    "UPDATE sources SET last_error = ?, last_fetched_at = ? WHERE id = ?",
                    (str(e), datetime.now(timezone.utc).isoformat(), source["id"]),
                )
        time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)

    print(f"\nDone. {total_new} new article(s) across {len(sources)} source(s).")


def ingest_source(source) -> int:
    resp = requests.get(source["feed_url"], headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    with get_conn() as conn:
        news_types = [
            r["news_type_slug"]
            for r in conn.execute(
                "SELECT news_type_slug FROM source_news_types WHERE source_id = ?",
                (source["id"],),
            ).fetchall()
        ]

    new_count = 0
    with get_conn() as conn:
        for entry in parsed.entries:
            url = entry.get("link")
            title = entry.get("title")
            if not url or not title:
                continue

            existing = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
            if existing:
                continue

            summary = clean_summary(entry.get("summary", ""))
            image_url = extract_image(entry)
            published_at = extract_published(entry)

            cur = conn.execute(
                """INSERT INTO articles
                   (source_id, title, url, summary, image_url, published_at, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    source["id"], title, url, summary, image_url, published_at,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            article_id = cur.lastrowid

            for nt_slug in news_types:
                conn.execute(
                    "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                    (article_id, nt_slug),
                )
            conn.execute(
                "INSERT OR IGNORE INTO article_regions (article_id, region_slug) VALUES (?, ?)",
                (article_id, source["default_region"]),
            )
            new_count += 1

        conn.execute(
            "UPDATE sources SET last_fetched_at = ?, last_error = NULL WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), source["id"]),
        )

    return new_count


def clean_summary(raw_html: str) -> str:
    """Strip HTML tags from the feed summary and truncate. Feeds already
    give short excerpts, not full text, so this is just cleanup, not
    the copyright safeguard -- that's the fact that we never fetch/store
    full article bodies at all."""
    import re
    text = re.sub("<[^<]+?>", "", raw_html or "").strip()
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "..."
    return text


def extract_image(entry) -> str | None:
    if "media_content" in entry and entry.media_content:
        return entry.media_content[0].get("url")
    if "media_thumbnail" in entry and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url")
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image/"):
            return link.get("href")
    return None


def extract_published(entry) -> str | None:
    if entry.get("published_parsed"):
        dt = datetime.fromtimestamp(mktime(entry.published_parsed), tz=timezone.utc)
        return dt.isoformat()
    return None


if __name__ == "__main__":
    run_ingest()
