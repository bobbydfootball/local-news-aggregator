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
DELAY_BETWEEN_REQUESTS_SECONDS = 6
MAX_RETRIES_ON_429 = 2
RETRY_BACKOFF_SECONDS = 20

# When a source's exclude_keywords match (e.g. "(AP)" marking wire-service
# content on a local paper's site), the article is reclassified rather than
# using the source's normal default_news_types:
#   1. If it doesn't mention Wisconsin/Milwaukee/a WI team, it's dropped
#      entirely -- generic national/world wire content doesn't belong in a
#      Wisconsin-focused aggregator, even though it's "good content" in the
#      abstract.
#   2. Otherwise, if it's sports-flavored -> Sports, else -> State.
# These lists are heuristics, not exhaustive -- same trade-off as the PREP
# keyword rule below.
WIRE_SPORTS_KEYWORDS = [
    "baseball", "basketball", "football", "hockey", "soccer", "golf",
    "tennis", "volleyball", "wrestling", "olympic", "nba", "nfl", "mlb",
    "nhl", "ncaa", "world series", "super bowl", "stanley cup", "playoff",
    "championship", "brewers", "packers", "bucks", "badgers",
]
WISCONSIN_KEYWORDS = [
    "wisconsin", "milwaukee", "waukesha", "madison", "green bay",
    "packers", "brewers", "bucks", "badgers", "racine", "kenosha",
    "appleton", "eau claire", "la crosse", "oshkosh", "wausau",
    "governor evers", "gov. evers",
]
WIRE_FALLBACK_NEWS_TYPE = "state"
WIRE_SPORTS_NEWS_TYPE = "sports"


def run_ingest():
    init_db()
    with get_conn() as conn:
        sources = conn.execute(
            "SELECT id, name, feed_url, default_region, sports_scope, sports_keyword, sports_keyword_scope, exclude_keywords FROM sources WHERE status = 'active'"
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


def fetch_with_retry(feed_url: str):
    """Fetch a feed URL, retrying with backoff specifically on HTTP 429
    (rate limited) -- some sources (GMToday) fail intermittently rather than
    consistently, so a couple of retries within the same run meaningfully
    improves the odds of success instead of just waiting for next hour's
    scheduled run to get lucky. Other errors (404, etc.) fail immediately --
    no point retrying a genuinely broken URL."""
    resp = None
    for attempt in range(MAX_RETRIES_ON_429 + 1):
        resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 429 and attempt < MAX_RETRIES_ON_429:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        break
    resp.raise_for_status()
    return resp


def ingest_source(source) -> int:
    resp = fetch_with_retry(source["feed_url"])
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
    exclude_keywords = source["exclude_keywords"].split("|") if source["exclude_keywords"] else []

    with get_conn() as conn:
        for entry in parsed.entries:
            url = entry.get("link")
            title = entry.get("title")
            if not url or not title:
                continue

            # Wire-service content (e.g. AP stories syndicated on a local
            # paper's site alongside their own reporting): if it has no
            # Wisconsin relevance at all, drop it -- a story about Florida
            # golf or national business news doesn't belong in a
            # Wisconsin-focused aggregator just because it happened to run
            # on a WI paper's site. WI-relevant wire content is kept and
            # reclassified: sports-flavored -> Sports, otherwise -> State.
            raw_summary = entry.get("summary", "")
            combined_text = f"{title} {raw_summary}".lower()
            is_wire = any(kw.lower() in combined_text for kw in exclude_keywords)
            wire_is_wi_relevant = is_wire and any(kw in combined_text for kw in WISCONSIN_KEYWORDS)
            wire_is_sports = is_wire and any(kw in combined_text for kw in WIRE_SPORTS_KEYWORDS)

            existing = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()

            if is_wire and not wire_is_wi_relevant:
                if existing:
                    # Was ingested before this WI-relevance check existed --
                    # remove it now rather than leaving it mis-tagged.
                    conn.execute("DELETE FROM article_news_types WHERE article_id = ?", (existing["id"],))
                    conn.execute("DELETE FROM article_regions WHERE article_id = ?", (existing["id"],))
                    conn.execute("DELETE FROM articles WHERE id = ?", (existing["id"],))
                continue

            if existing:
                article_id = existing["id"]
            else:
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
                new_count += 1

            # Always recompute this article's category + region from the
            # CURRENT source config, whether it's brand new or already
            # existed. This makes the system self-healing: if sources.yaml's
            # default_news_types or default_region changes, previously-
            # ingested articles get correctly re-tagged on the next run
            # instead of keeping stale tags from whatever config was active
            # when they were first inserted.
            conn.execute("DELETE FROM article_news_types WHERE article_id = ?", (article_id,))
            conn.execute("DELETE FROM article_regions WHERE article_id = ?", (article_id,))

            title_matches_sports_keyword = source["sports_keyword"] and any(
                kw.lower() in title.lower() for kw in source["sports_keyword"].split("|")
            )
            if title_matches_sports_keyword:
                # Local prep sports (e.g. GMToday's "PREP" prefix) takes
                # priority over wire detection.
                conn.execute(
                    "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                    (article_id, "sports"),
                )
            elif is_wire:
                target_type = WIRE_SPORTS_NEWS_TYPE if wire_is_sports else WIRE_FALLBACK_NEWS_TYPE
                conn.execute(
                    "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                    (article_id, target_type),
                )
            else:
                for nt_slug in news_types:
                    conn.execute(
                        "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                        (article_id, nt_slug),
                    )
            conn.execute(
                "INSERT OR IGNORE INTO article_regions (article_id, region_slug) VALUES (?, ?)",
                (article_id, source["default_region"]),
            )

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
