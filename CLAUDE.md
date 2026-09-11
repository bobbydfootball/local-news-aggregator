# Bo6's News Aggregator

Personal Python/Streamlit/SQLite news aggregator focused on Wisconsin/Waukesha-area
local news. Sole developer and user: Bob.

## Architecture

- **Ingest**: `sources.yaml` (source registry) → `ingest.py` (fetches RSS feeds,
  routes articles into categories) → SQLite
- **Config sync**: `load_config.py` loads `taxonomy.yaml` and `sources.yaml` into
  the DB. Run after editing either config file. Safe to re-run (uses upserts).
- **Feed health**: `verify_feeds.py` checks every active source's `feed_url` and
  writes `active`/`dead`/`needs_review` to `sources.status`.
- **Frontend**: `app.py`, Streamlit Community Cloud, color-coded `st.tabs()` layout.
- **DB helpers**: `db.py`

Production workflow (GitHub Actions, scheduled every 30 minutes):
`load_config` → `verify_feeds` → `ingest` → Ticketmaster concerts ingest → dad joke ingest.

`load_config`'s `load_sources()` resets every source still present in
`sources.yaml` to `status='active'` at the start of each run, before
`verify_feeds` runs — this makes a bad `verify_feeds` result on one run
self-correcting on the next, not a permanent lockout.

cron-job.org runs as a redundant scheduler alongside GitHub Actions. A
`keep-awake.yml` workflow curls the Streamlit app URL every 4 hours to
prevent hibernation (no `-L` flag — that domain has a redirect-loop issue).

## Development environment

**Bob works in Google Colab** for development, testing, and diagnostic work —
not GitHub Actions exclusively. Colab has real network access, unlike some
other environments Claude may have used earlier in a session. All production
deployments flow through GitHub Actions regardless of where testing happened.

Colab runs a fresh environment each session — `feedparser`, `beautifulsoup4`,
etc. need `!pip install` before use.

**Known Colab-specific gotcha**: Colab's shared Google Cloud IP range gets
429'd by some sources (confirmed on GMToday's `f=rss` search endpoint) even
when GitHub Actions' runners fetch the exact same URL successfully. A 429
reproduced from Colab does **not** confirm a production problem — always
verify against the production DB (`sources.last_error`, `sources.last_fetched_at`,
and actual `articles` counts) before concluding a source is broken in
production. This mistake was made once in this project's history; don't
repeat it.

## Core design principles

- **Fair-use approach**: never fetch or store full article text. Only
  headline, short summary/excerpt, thumbnail image, and link are stored —
  same pattern as Google News / Apple News. This is a hard line, not a
  guideline — see "Rejected: expanded Freeman summaries" below for a case
  where this actually came up.
- **Routing rules applied uniformly** across all sources — no per-source
  special cases without a documented reason.
- **Catch-all vs. specific-identity categories**:
  - "Catch-all" categories (State, World) get the full routing toolkit:
    `team_routing`, `local_keyword`, `exclude_keywords` — any of these
    signals means the article belongs somewhere more specific.
  - "Specific-identity" categories (Waukesha, Politics, Events, each
    individual team) do NOT get `team_routing`/`local_keyword` — a stray
    team mention shouldn't hijack an article out of its source's own
    editorial focus.
  - `exclude_keywords` (wire-content filtering) is the one exception that
    cuts across both kinds — it's a relevance filter, not a "find a more
    specific home" mechanism.
- **Changes are batched** until Bob explicitly signals deploy time.

## `sources.yaml` field reference

- `feed_url`: the actual RSS/Atom endpoint.
- `default_news_types`: single-item list (every article resolves to exactly
  one category, no cross-listing).
- `sports_keyword` (optional): `|`-separated clauses. Bare clause matches
  the phrase alone; `primary&qual1,qual2,...` only matches if the primary
  phrase AND at least one qualifier are both present. Exists because bare
  "high school" false-positived on non-sports stories that simply name a
  school. Supplemented in `ingest.py` by `PREP_SCHOOLS_TIER1`/`TIER2` —
  known Milwaukee/Waukesha prep school names, split by collision risk with
  ordinary local news (Tier 2 names like "Marquette"/"Oak Creek" are real
  town names too and only count alongside a qualifier or a Tier 1 name).
- `team_routing` (optional, bool): routes a named WI team mention
  (Packers/Brewers/Bucks/Badgers) straight to that team's category.
- `local_keyword` (optional): `|`-separated place names; routes to
  `waukesha` category.
- `exclude_keywords` (optional): `|`-separated wire-service markers.
  Matched content is checked for WI relevance (place/team mention); if
  relevant, reclassified to `state`; if not, dropped entirely.

Priority order when multiple rules could match (see `ingest_source()` in
`ingest.py`): `sports_keyword` → `local_keyword` → `team_routing` →
wire-fallback → source's normal `default_news_types`.

## Known platform quirks

- **BLOX/TownNews sites** (GMToday/Freeman, Capital Times, Wisconsin
  State Journal's Capital Times section): article URLs can change SEO
  slug post-publication while the underlying `article_<uuid>.html`
  identifier stays stable. `compute_content_hash()` extracts that UUID
  for dedup instead of using the raw URL — this fixed a real duplicate-
  article bug. Also: their `f=rss&t=article&...` search-style RSS
  endpoints intermittently 429 bare/retry-less requests; `fetch_with_retry()`
  in `ingest.py` (headers, `Referer`, backoff, jitter, `Retry-After`
  awareness) handles this in production. `verify_feeds.py`'s `check_feed()`
  should use the same retry logic — see Known Issues below.
- **GMToday/Freeman paywall**: metered paywall via TownNews, client-side
  (not server-side) — full article HTML (all `<p>` tags in the
  `asset-content` container) is server-rendered even though a real browser
  hits a subscription wall. This was tested and confirmed directly. See
  "Rejected: expanded Freeman summaries" below for why this isn't exploited
  despite being technically easy.
- **Sidearm Sports sites** (uwbadgers.com): per-sport RSS via
  `uwbadgers.com/rss?path=X`, which internally redirects to
  `uwbadgers.com/api/v2/rss/xml?path=X`. An unrecognized `path` value
  does NOT error — it silently falls back to some other feed's content
  (observed: bogus text fell back to `mbball`; a bogus numeric value fell
  back to the unfiltered all-sports feed). This makes casual testing
  misleading — always check the `destination_url`/`final_url` of a
  response, not just whether it returned 200. The unfiltered combined feed
  (`uwbadgers.com/rss`, no `path` param) is a real, stable, independently-
  reachable endpoint, not just a fallback artifact — confirmed by hitting
  it directly. It's hard-capped at ~10 items; `count`/`max`/`limit`/`n`
  query params were all tested and none increased this.

## Current source list (as of last update)

- **Waukesha**: Freeman (GMToday) News + Sports sections; Lake Country
  Tribune Local + Sports category feeds (site-wide `/feed/` deliberately
  excluded — 100% Center Square wire content; category feeds tested clean,
  0% wire across 30 entries each).
- **State**: CBS58 Milwaukee, Milwaukee Independent, Spectrum News 1 WI
  Local Headlines, Capital Times News + Sports.
- **Politics**: Wisconsin Examiner, Spectrum News 1 WI Politics, Wisconsin
  Right Now, Badger Institute, The Heartland Post.
- **World**: Fox News Latest Headlines (deliberately no `exclude_keywords`
  — that would gut a category meant to hold non-WI content).
- **Packers**: Green Bay Packers official feed.
- **Brewers**: Milwaukee Brewers official feed.
- **Bucks**: Brew Hoop (SB Nation).
- **Badgers**: single combined `uwbadgers.com/rss` feed (see "Badgers RSS
  consolidation" below) — deliberately unfiltered, covers every UW sport,
  not just a curated subset.
- **Events**: Shepherd Express Brew City Buzz + Madcap Milwaukee Calendar,
  Ticketmaster Wisconsin concerts (separate dedicated ingest script, 7-day
  retention cutoff, accepted trade-off that `published_at` reflects
  feed-add date not event date). Still missing: Waukesha County-specific
  events/festivals, Wisconsin State Fair/county fairs — no programmatic
  source found despite research.

### Badgers RSS consolidation (recent change)

Originally 6 separate per-sport feeds (football, men's/women's basketball,
men's/women's hockey, volleyball), all mapping to the same `badgers`
category with no differentiated routing. Football's dedicated feed
(`path=football`) started 500ing in early Sept 2026 while the actual
football content kept publishing normally on the site and while the other
5 sport-specific feeds kept working fine individually — confirmed this was
narrow (football's feed-generation specifically, not a platform-wide or
URL-pattern issue) before touching anything.

Considered and rejected: HTML-scraping the football archives page as a
fallback (technically viable — deck-line summary avoids full-text/
paywall-adjacent copyright concerns — but adds a scraping-fragility
maintenance burden none of the other sources have, and duplicates the
`api_source` pattern already used for Ticketmaster/dad-joke without buying
much).

Actual fix: switched ALL SIX feeds to the single unfiltered
`uwbadgers.com/rss` combined feed. This was a deliberate scope decision,
not just an implementation detail — the combined feed includes every UW
sport (soccer, golf, cross country, wrestling, rowing, tennis, swimming),
not just the original six. Bob chose to widen scope to "everything
Badgers" for simplicity rather than add an include-filter mechanism that
would've kept it to the original six sports. The ~10-item cap was
confirmed to not be a practical coverage risk given the observed
publishing rate (~10 items across all UW sports per ~2 days) and the
30-minute ingest cadence with persistent dedup'd storage — an item would
only be missed if 10+ stories published site-wide within one 30-minute
gap, far outside the observed rate.

## Rejected: expanded Freeman summaries

Investigated whether GMToday/Freeman's RSS `summary` field was leaving
usable text on the table (it wasn't — `feedparser`'s `summary` maps to the
short RSS `<description>`, and there's no separate `content:encoded` block
in this feed). Follow-up investigation found the full article body IS
server-rendered in the page HTML even behind the paywall (client-side
paywall, not server-side restriction) — technically trivial to scrape 3-4
full body paragraphs per article.

**Decision: did not implement this.** Pulling real body paragraphs
(covering the actual substance of a paywalled article, not just a teaser)
conflicts with the project's own "never fetch full article text" fair-use
principle documented at the top of `ingest.py`. A longer-than-current
excerpt might be revisited later, but capped much closer to what a normal
RSS teaser would give (roughly 1 paragraph), not the 4-paragraph
substance-of-the-story version that was prototyped and set aside.

## Known issues / accepted limitations

- **BLOX 429s** (GMToday, Capital Times search-style RSS endpoints):
  handled by `fetch_with_retry()` in production `ingest.py`. Confirmed via
  direct DB query (`last_error IS NULL`, recent `last_fetched_at`, healthy
  article counts) that this is NOT currently a production problem.
- **`verify_feeds.py` has no retry logic** (plain `requests.get()`, single
  attempt) unlike `ingest.py`'s `fetch_with_retry()`. Since `verify_feeds`
  writes directly to `sources.status` and `ingest.py` only processes
  `status='active'` sources, a single transient 429 during a `verify_feeds`
  run can cause that one 30-minute ingest cycle to skip an otherwise-healthy
  source. Confirmed this happening to Capital Times (marked `dead` while
  `ingest.py` was fetching it successfully). **Not fixed** — given the
  `load_config → verify_feeds → ingest` run order, `load_config` resets
  status to `active` at the start of every run, so the actual impact is
  bounded to "that one cycle's articles arrive ~30 min late," not a
  standing outage. Assessed as low-priority; a real fix (share
  `fetch_with_retry` between `ingest.py` and `verify_feeds.py`) is drafted
  but not applied — revisit if Capital Times articles start showing up
  consistently missing/delayed rather than as an occasional blip.
- **Waukesha Freeman paywall**: no clean fix exists that doesn't cross the
  fair-use line (see "Rejected: expanded Freeman summaries" above). Kept
  as a source for its unique local/prep-sports coverage; users hitting the
  paywall on click-through is an accepted trade-off.
- **Node.js 20 deprecation warning** in GitHub Actions runs
  (`actions/checkout@v4`, `actions/setup-python@v5` targeting Node 20,
  auto-forced onto Node 24): cosmetic for now, GitHub is silently patching
  it. Fix whenever convenient: bump to `actions/checkout@v5`.

## Key working patterns

- **Test against actual feed/API data, not rendered HTML pages.** A
  category archive page and its corresponding RSS feed can serve genuinely
  different content (confirmed on Lake Country Tribune — the rendered
  `/category/local/` page looked 100% wire-content-polluted, but the real
  `/category/local/feed/` RSS was 0% wire across a 30-entry sample). Always
  verify the actual mechanism being integrated, not a visually similar proxy.
- **Production DB queries are the reliable verification method** — more
  reliable than reproducing a request from a diagnostic environment (Colab,
  local scripts, etc.), which can have different IP-range/rate-limit
  behavior than GitHub Actions runners. Check `sources.last_error`,
  `sources.last_fetched_at`, and actual `articles` counts/timestamps
  before concluding something is or isn't working in production.
- **Do the math on actual publishing velocity before assuming a coverage
  risk.** A feed's item cap only matters if new-item velocity can plausibly
  exceed it within one polling interval — check pubDate spacing in a real
  sample rather than reasoning from "some slots are used by other content."
- **Verify unfamiliar API/query parameter behavior directly** rather than
  assuming a plausible-looking parameter works — e.g. Sidearm's `path=`
  silently falls back instead of erroring on bad input, and `count`/`max`/
  `limit`/`n` were all tested and confirmed not to raise the ~10-item cap
  on uwbadgers.com's combined feed.
- Verify deploy-ready files with `python3 -m py_compile` (Python) or
  `yaml.safe_load` (YAML) before delivery.
- All deploy-ready files delivered via numbered step lists, not fenced
  code blocks pasted inline — Bob copies full files back in for review.
- Bob pushes back (correctly) when asked to re-confirm information already
  established in this file or earlier in a conversation — use what's
  already known rather than re-asking.
- Fabricated or inapplicable suggestions (e.g., assuming a paywall
  circumvention technique will work without testing it, or asserting a
  feed is "broken" from a single ambiguous/possibly-cached test result)
  have caused real rework in this project's history. Test claims directly
  before presenting them as fact, and flag explicitly when a result is
  ambiguous rather than building a conclusion on top of it.
