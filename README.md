# Local News Aggregator — Waukesha / Milwaukee / Wisconsin

Aggregates headlines, short summaries, thumbnails, and source links from free
local news RSS feeds, grouped by news type (Politics, Community, Public
Safety, Sports, State/General) and then by geography (City of Waukesha →
Waukesha County → Milwaukee County → Wisconsin).

## Architecture

```
config/
  taxonomy.yaml     # news types + regions + display order/colors
  sources.yaml       # RSS feed registry, tagged by default region/news type
src/
  db.py               # SQLite schema + connection helper
  load_config.py      # loads the YAML config into the DB
  verify_feeds.py     # checks all feed URLs are live before you rely on them
  ingest.py           # polls feeds, dedups, stores headline+summary+image+link
app.py                # Streamlit frontend
data/news.db           # SQLite database (created on first run)
```

**Why this shape:** regions and news types live in YAML/DB tables, not in
app code. Adding a new city or state later means adding rows to
`taxonomy.yaml` and `sources.yaml` — the Streamlit UI and ingestion logic
don't change.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Load config into the DB
python -m src.load_config

# Confirm feed URLs are actually live before trusting them
python -m src.verify_feeds

# Pull articles
python -m src.ingest

# Launch the app
streamlit run app.py
```

## On the source list

`config/sources.yaml` has several feed URLs marked `# VERIFY` — these were
identified through research but not yet confirmed as live endpoints (RSS
paths on newspaper/TV sites drift more often than you'd expect). Run
`verify_feeds.py` first; it'll mark each source `active`, `dead`, or
`needs_review` in the database and print a report. Fix or remove anything
that comes back `dead` before your first real ingest.

The `ESPN` sports feeds are general-topic (all MLB / all NFL news) rather
than team-specific — you'll likely want to add a keyword filter step in
`ingest.py` to keep only entries mentioning the Brewers/Packers, or swap in
team-specific feeds if you find better ones.

## Copyright / fair-use note

Ingestion stores only headline, a short excerpt (truncated to ~400 chars),
thumbnail URL, and a link back to the original article — never full article
text. This mirrors how Google News / Apple News operate and is the
lower-risk pattern for an aggregator. Don't extend `ingest.py` to scrape
and store full article bodies without separately considering licensing.

## Known limitations / next steps

- **SQLite + GitHub Actions committing the DB file** is fine for a
  prototype but is not a great long-term pattern (binary diffs, no
  concurrent writes). If this grows, move to a hosted Postgres (e.g.
  Supabase/Neon free tier) and point `db.py` at it instead.
- **Streamlit Community Cloud has a read-only filesystem at runtime** for
  anything not committed to the repo — the GitHub Action approach (ingest
  runs in CI, commits `data/news.db`, app reads the committed file) works
  around this, but a hosted DB avoids the workaround entirely.
- **Sports team filtering** is currently just "whatever the feed contains"
  — add keyword matching against `config/taxonomy.yaml`'s `state_teams`
  list if you want cleaner team-specific sections.
- **Dedup across outlets** isn't implemented yet — if five outlets cover
  the same county board vote, you'll currently see five cards. A
  title-similarity clustering pass would be the next enhancement.
- **Adding a new region/city later**: add entries to `taxonomy.yaml`
  (region) and `sources.yaml` (its feeds), re-run `load_config.py`. The nav
  and grouping in `app.py` will pick it up automatically.
