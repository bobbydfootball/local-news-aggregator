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
import random
from datetime import datetime, timezone
from time import mktime
from src.db import get_conn, init_db

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
MAX_SUMMARY_CHARS = 400
DELAY_BETWEEN_REQUESTS_SECONDS = 6
MAX_RETRIES_ON_429 = 3
RETRY_BACKOFF_SECONDS = 25
RETRY_JITTER_SECONDS = 10           # random +/- added to each backoff so
                                     # retry timing isn't perfectly regular
                                     # -- some bot-detection systems flag
                                     # suspiciously consistent retry
                                     # intervals as evidence of automation
MAX_RETRY_AFTER_WAIT_SECONDS = 60  # cap how long we'll actually wait even if
                                    # the server's real Retry-After value is
                                    # much longer -- no point holding up the
                                    # whole workflow for a 30-60 min window;
                                    # better to just let the next scheduled
                                    # run handle it

# When a source's exclude_keywords match (e.g. "(AP)" marking wire-service
# content, or "Daily News"/"News Graphic" marking a different GMToday paper),
# the article is reclassified rather than using the source's normal
# default_news_types. Priority order (see ingest_source below):
#   1. sports_keyword match (e.g. Freeman's "PREP") -> local_sports
#   2. A specific WI team is named (if team_routing is enabled for this
#      source) -> that team's own category, e.g. "packers"
#   3. Wire-service marker matched, and the article mentions Wisconsin/
#      Milwaukee/a WI team -> State (a team mention here still routes to
#      the team category via #2 first if team_routing is on; #3 is the
#      fallback for WI-relevant wire content that doesn't name a team)
#   4. Wire-service marker matched, but nothing Wisconsin-relevant found
#      -> dropped entirely (generic national/world wire content doesn't
#      belong in a Wisconsin-focused aggregator just because it happened
#      to run on a WI site)
#   5. Otherwise -> the source's normal default_news_types
WISCONSIN_TEAMS = ["packers", "brewers", "bucks", "badgers"]
WISCONSIN_KEYWORDS = [
    "wisconsin", "milwaukee", "waukesha", "madison", "green bay",
    "racine", "kenosha", "appleton", "eau claire", "la crosse",
    "oshkosh", "wausau", "governor evers", "gov. evers",
]
WIRE_FALLBACK_NEWS_TYPE = "state"


def run_ingest():
    init_db()
    with get_conn() as conn:
        sources = conn.execute(
            "SELECT id, name, feed_url, default_region, sports_scope, sports_keyword, sports_keyword_scope, exclude_keywords, team_routing FROM sources WHERE status = 'active'"
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
    no point retrying a genuinely broken URL.

    If the server includes a Retry-After header, that real value is used
    instead of our fixed guess -- the server is telling us exactly how long
    to wait, which is more reliable than assuming."""
    resp = None
    for attempt in range(MAX_RETRIES_ON_429 + 1):
        resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 429 and attempt < MAX_RETRIES_ON_429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait_seconds = min(int(retry_after), MAX_RETRY_AFTER_WAIT_SECONDS)
            else:
                # No real data from the server (confirmed via direct test --
                # GMToday sends no Retry-After header), so this is still a
                # guess. Jitter is added so our retry timing isn't perfectly
                # regular, which is the one evidence-based improvement
                # available without real server data to work from.
                base_wait = RETRY_BACKOFF_SECONDS * (attempt + 1)
                wait_seconds = base_wait + random.uniform(-RETRY_JITTER_SECONDS, RETRY_JITTER_SECONDS)
                wait_seconds = max(5, wait_seconds)  # never wait less than 5s
            time.sleep(wait_seconds)
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

            # combined_text includes the author byline (not just title +
            # summary) since wire-service markers like "Associated Press"
            # often only appear there, not in the visible text (e.g.
            # Spectrum News tags AP-sourced national stories this way).
            raw_summary = entry.get("summary", "")
            author = entry.get("author", "") or ""
            combined_text = f"{title} {raw_summary} {author}".lower()
            is_wire = any(kw.lower() in combined_text for kw in exclude_keywords)
            wire_is_wi_relevant = is_wire and (
                any(kw in combined_text for kw in WISCONSIN_KEYWORDS)
                or any(kw in combined_text for kw in WISCONSIN_TEAMS)
            )

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
            # when they were first inserted. (This is what caused sports
            # articles to "sprinkle" into non-sports sections, and region
            # sub-groupings to look broken, after several rounds of
            # taxonomy/source changes -- old tags never got refreshed.)
            conn.execute("DELETE FROM article_news_types WHERE article_id = ?", (article_id,))
            conn.execute("DELETE FROM article_regions WHERE article_id = ?", (article_id,))

            title_matches_sports_keyword = source["sports_keyword"] and any(
                kw.lower() in title.lower() for kw in source["sports_keyword"].split("|")
            )
            matched_team = None
            if source["team_routing"]:
                for team in WISCONSIN_TEAMS:
                    if team in combined_text:
                        matched_team = team
                        break

            if title_matches_sports_keyword:
                # Local prep sports (e.g. GMToday's "PREP" prefix) takes
                # priority over everything else, and goes to the dedicated
                # Local Sports category rather than a specific team --
                # keeps local prep coverage from competing with Packers/
                # Brewers/Badgers/etc. for the same display slot budget.
                conn.execute(
                    "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                    (article_id, "local_sports"),
                )
            elif matched_team:
                # A specific WI team is named and this source opted into
                # team routing -- goes straight to that team's category,
                # taking priority over wire-fallback logic below (a
                # Packers story co-bylined "Associated Press" should still
                # land under Packers, not get stuck in the generic State
                # wire-fallback).
                conn.execute(
                    "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                    (article_id, matched_team),
                )
            elif is_wire:
                conn.execute(
                    "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                    (article_id, WIRE_FALLBACK_NEWS_TYPE),
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
