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
import re
import requests
import time
import random
from datetime import datetime, timezone
from time import mktime
from urllib.parse import urlparse
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
WISCONSIN_TEAM_KEYWORDS = {
    # keyword -> the category slug it should route to. Not a flat list,
    # since "badger"/"packer" (singular) are valid, safe keywords to match
    # on but are NOT themselves valid category slugs -- only the plural
    # forms are. Using a flat list here would have silently tried to
    # insert an invalid slug like "badger" and failed the insert.
    #
    # "brewer" and "buck" deliberately excluded despite being the natural
    # singular forms: "brewer" is an ordinary word for anyone who brews
    # beer (Milwaukee has an active craft brewing scene that gets regular
    # local coverage), and "buck" is common in unrelated senses (money
    # slang, the animal, "buck a trend"). Both would generate real false
    # positives, unlike "badger"/"packer" which don't have a comparably
    # common unrelated everyday usage.
    "packers": "packers",
    "packer": "packers",
    "brewers": "brewers",
    "bucks": "bucks",
    "badgers": "badgers",
    "badger": "badgers",
}
WISCONSIN_KEYWORDS = [
    "wisconsin", "milwaukee", "waukesha", "madison", "green bay",
    "racine", "kenosha", "appleton", "eau claire", "la crosse",
    "oshkosh", "wausau", "governor evers", "gov. evers",
]
WIRE_FALLBACK_NEWS_TYPE = "state"

# --- Prep sports school-name routing (supplements sports_keyword) ---
#
# Background: a bare "high school" keyword match (the original
# sports_keyword mechanism) turned out to false-positive on genuine
# non-sports CBS58 stories that simply name a school, e.g. "Lincoln
# Avenue kids begin school year at Pulaski High School after fire
# destroyed building" and "Nicolet Union High School rolls out the red
# carpet to welcome students back". Neither is a sports story. This is
# why sports_keyword clauses now support an optional "&qualifier1,
# qualifier2,..." suffix requiring a sports-context word alongside the
# primary phrase (see ingest_source below) -- but "high school" alone,
# even qualified, still misses genuine prep recaps that never say the
# literal words "high school" at all, e.g. "Catholic Memorial, Greenfield
# among Thursday night winners as Week 2 kicks off".
#
# The two lists below close that gap using actual school names from the
# WIAA conferences covering CBS58's Milwaukee/Waukesha coverage area
# (Greater Metro, North Shore, Parkland, Milwaukee City), split by
# collision risk with ordinary (non-sports) local news:
#
# TIER 1 -- distinctive names, safe to match on their own (combined with
# a sports qualifier word, same as any other sports_keyword clause).
#
# TIER 2 -- names that are also real town/village names or otherwise
# collide with ordinary local coverage (confirmed in CBS58's own feed:
# "Marquette wins overtime thriller..." is Marquette University
# basketball, not Marquette University High School; "Oak Creek
# dealership sued..." is ordinary local news). These are NOT matched on
# their own, even with a qualifier -- they only count as a signal when
# they appear alongside a Tier 1 name in the same title, since two named
# schools sharing a headline is itself strong evidence of a prep
# matchup. Revisit/expand this list as new collisions are found.
PREP_SCHOOLS_TIER1 = [
    "arrowhead", "nicolet", "cedarburg", "pewaukee", "divine savior holy angels",
    "whitefish bay", "sussex hamilton", "menomonee falls",
    "new berlin eisenhower", "new berlin west", "waukesha north",
    "waukesha south", "wisconsin lutheran", "milwaukee lutheran",
    "catholic memorial", "pius xi", "brookfield central", "brookfield east",
    "nathan hale", "wauwatosa east", "wauwatosa west", "west allis central",
]
PREP_SCHOOLS_TIER2 = [
    "marquette", "oak creek", "franklin", "greendale", "germantown",
    "homestead", "grafton", "slinger", "hartford", "shorewood",
]
SPORTS_QUALIFIERS = [
    "score", "beat", "beats", "win", "wins", "won", "winners", "loses",
    "lost", "vs.", "vs", "game", "tournament", "tops",
]


BLOX_ARTICLE_ID_PATTERN = re.compile(
    r"article_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def compute_content_hash(url: str) -> str:
    """Extract a stable per-article identifier from a URL, used for
    dedup instead of the raw URL.

    Discovered via a real duplicate: GMToday/BLOX (used by both Freeman
    and Capital Times) allows an article's SEO slug to change after
    publication -- e.g. a short working title "A transformative
    experience" edited to the fuller "...Waukesha resident celebrates
    50th birthday with donation to Locks of Love" -- while the
    underlying "article_<uuid>.html" identifier stays the same. The
    feed re-serves the article under the new URL, and since dedup was
    previously keyed on the full URL, the same story got inserted
    twice under two different slugs.

    BLOX article URLs end in a stable "article_<uuid>.html" segment
    regardless of the slug preceding it -- that's what's extracted and
    matched on here instead. Sources not on BLOX (no matching pattern)
    fall back to the raw URL, i.e. unchanged behavior from before this
    fix.
    """
    match = BLOX_ARTICLE_ID_PATTERN.search(url)
    if match:
        return match.group(0)
    return url


def run_ingest():
    init_db()
    with get_conn() as conn:
        sources = conn.execute(
            "SELECT id, name, feed_url, default_region, sports_keyword, exclude_keywords, team_routing, local_keyword FROM sources WHERE status = 'active'"
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
    to wait, which is more reliable than assuming.

    Headers beyond User-Agent (Accept, Referer) are included since some
    bot-detection systems check for these too, not just the UA string.
    Referer is derived from the feed URL's own domain (not hardcoded) since
    sources span many different publisher domains -- it points at that
    specific publisher's homepage, same as a real browser visiting that
    site would send. Worth being honest that this may not fully resolve
    429s if they're actually caused by shared-IP-range rate limiting on
    GitHub Actions' runners (used by many unrelated projects hitting the
    same publisher infrastructure) rather than by request-signature
    detection -- header spoofing wouldn't fix that kind of limiting."""
    parsed_url = urlparse(feed_url)
    referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml",
        "Referer": referer,
    }

    resp = None
    for attempt in range(MAX_RETRIES_ON_429 + 1):
        resp = requests.get(feed_url, headers=headers, timeout=15)
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
                or any(kw in combined_text for kw in WISCONSIN_TEAM_KEYWORDS)
            )

            content_hash = compute_content_hash(url)
            existing = conn.execute(
                "SELECT id FROM articles WHERE content_hash = ?", (content_hash,)
            ).fetchone()

            if is_wire and not wire_is_wi_relevant:
                if existing:
                    # Was ingested before this WI-relevance check existed --
                    # remove it now rather than leaving it mis-tagged.
                    conn.execute("DELETE FROM article_news_types WHERE article_id = ?", (existing["id"],))
                    conn.execute("DELETE FROM article_regions WHERE article_id = ?", (existing["id"],))
                    conn.execute("DELETE FROM articles WHERE id = ?", (existing["id"],))
                continue

            summary = clean_summary(entry.get("summary", ""))
            image_url = extract_image(entry)
            published_at = extract_published(entry)

            if existing:
                article_id = existing["id"]
                # Refresh these fields on every run, same as category/region
                # below -- previously this branch only grabbed the existing
                # ID and never touched published_at/image_url/summary again.
                # That meant if any of them came out wrong (NULL, a bad
                # parse, anything) on an article's very first ingestion, it
                # stayed wrong forever, even though the article kept getting
                # correctly re-processed for everything else on every
                # subsequent run. A stuck bad published_at is especially
                # damaging here since it silently fails the freshness
                # filter with no visible error -- this is what caused
                # correctly-fetched, correctly-categorized official-feed
                # articles to be invisible in the app despite everything
                # upstream reporting success.
                # title and url are refreshed here too, not just
                # summary/image_url/published_at -- a changed slug (the
                # exact thing that caused the Freeman duplicate bug) is
                # a changed title in practice, so this keeps the stored
                # article pointing at the current live URL and headline
                # instead of a stale pre-edit version.
                conn.execute(
                    "UPDATE articles SET title = ?, url = ?, summary = ?, image_url = ?, published_at = ?, content_hash = ? WHERE id = ?",
                    (title, url, summary, image_url, published_at, content_hash, article_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO articles
                       (source_id, title, url, summary, image_url, published_at, fetched_at, content_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source["id"], title, url, summary, image_url, published_at,
                        datetime.now(timezone.utc).isoformat(), content_hash,
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

            title_lower = title.lower()

            # sports_keyword clauses are "|"-separated. Each clause is
            # either a bare phrase (matches on its own, e.g. CBS58's
            # branded "Friday Night Rivals" segment name -- unambiguous,
            # no qualifier needed) or "primary&qual1,qual2,..." meaning
            # the primary phrase only counts as a sports match if at
            # least one qualifier word is ALSO present in the title. This
            # exists because a bare "high school" match was
            # false-positiving on real non-sports stories that simply
            # name a school, e.g. "...Pulaski High School after fire
            # destroyed building" and "Nicolet Union High School rolls
            # out the red carpet to welcome students back" -- neither is
            # a sports story, but both contain the literal phrase.
            title_matches_sports_keyword = False
            if source["sports_keyword"]:
                for clause in source["sports_keyword"].split("|"):
                    if "&" in clause:
                        primary, quals_str = clause.split("&", 1)
                        qualifiers = quals_str.split(",")
                        if primary.lower() in title_lower and any(
                            q.lower() in title_lower for q in qualifiers
                        ):
                            title_matches_sports_keyword = True
                            break
                    else:
                        if clause.lower() in title_lower:
                            title_matches_sports_keyword = True
                            break

            # Supplemental check: known prep school names, for headlines
            # that never say "high school" at all (e.g. "Catholic
            # Memorial, Greenfield among Thursday night winners as Week 2
            # kicks off"). A Tier 1 (distinctive, low collision risk)
            # school name counts as a sports match if EITHER a sports
            # qualifier word is also present, OR a Tier 2 (collision-risk
            # -- also a real town/village name) school name is also
            # present, since two named schools sharing a headline is
            # itself strong evidence of a prep matchup. Tier 2 names are
            # never matched alone, even with a qualifier -- confirmed
            # collisions in CBS58's own feed include "Marquette wins
            # overtime thriller..." (Marquette University basketball, not
            # the high school) and "Oak Creek dealership sued..."
            # (ordinary local news, not sports).
            if not title_matches_sports_keyword:
                tier1_present = any(name in title_lower for name in PREP_SCHOOLS_TIER1)
                tier2_present = any(name in title_lower for name in PREP_SCHOOLS_TIER2)
                has_qualifier = any(q in title_lower for q in SPORTS_QUALIFIERS)
                if tier1_present and (has_qualifier or tier2_present):
                    title_matches_sports_keyword = True

            title_matches_local_keyword = source["local_keyword"] and any(
                kw.lower() in combined_text for kw in source["local_keyword"].split("|")
            )
            matched_team = None
            if source["team_routing"]:
                for keyword, slug in WISCONSIN_TEAM_KEYWORDS.items():
                    if keyword in combined_text:
                        matched_team = slug
                        break

            if title_matches_sports_keyword:
                # Local prep sports (e.g. GMToday's "PREP" prefix, CBS58's
                # "high school") takes priority over everything else, and
                # goes to the dedicated Local Sports category rather than a
                # specific team -- keeps local prep coverage from competing
                # with Packers/Brewers/Badgers/etc. for the same display
                # slot budget. Also takes priority over local_keyword, so a
                # Waukesha-area prep sports story still lands in Local
                # Sports, consistent with how Freeman's own Waukesha sports
                # coverage already works.
                conn.execute(
                    "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                    (article_id, "local_sports"),
                )
            elif title_matches_local_keyword:
                # Same pattern as sports_keyword -> local_sports above, just
                # a different fixed destination: a configured place name
                # (e.g. CBS58 saying "Waukesha") routes straight to the
                # Waukesha category, ahead of team_routing below. A
                # hyperlocal connection ("Waukesha native drafted by the
                # Packers") is judged more valuable to a Waukesha reader
                # than the team categorization would be.
                conn.execute(
                    "INSERT OR IGNORE INTO article_news_types (article_id, news_type_slug) VALUES (?, ?)",
                    (article_id, "waukesha"),
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
