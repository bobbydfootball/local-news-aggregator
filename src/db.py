"""
Database layer for the local news aggregator.

Uses SQLite for simplicity (no external server needed to run the prototype).
If/when this needs to scale to more regions or concurrent writers, swap the
connection logic here for Postgres (psycopg2 / SQLAlchemy) -- the schema
below is written in plain, portable SQL to make that migration easy.
"""

import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "news.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS regions (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    level       TEXT NOT NULL,          -- city / county / state
    parent_slug TEXT,
    sort_order  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS news_types (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    color       TEXT,
    sort_order  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    base_url            TEXT,
    feed_url            TEXT NOT NULL UNIQUE,
    default_region      TEXT REFERENCES regions(slug),
    sports_scope        TEXT,           -- local_teams / state_teams, nullable
    sports_keyword      TEXT,           -- optional: title keyword that reclassifies an article as sports instead of this source's default_news_types
    sports_keyword_scope TEXT,          -- local_teams / state_teams, used only for keyword-matched articles
    exclude_keywords    TEXT,           -- optional: "|"-separated keywords; if title+summary contains any, the article is treated as wire-service content and reclassified (see WIRE_SPORTS_KEYWORDS in ingest.py) instead of using this source's default_news_types
    status              TEXT DEFAULT 'active',   -- active / dead / needs_review
    last_fetched_at     TEXT,
    last_error          TEXT
);

CREATE TABLE IF NOT EXISTS source_news_types (
    source_id       INTEGER REFERENCES sources(id),
    news_type_slug  TEXT REFERENCES news_types(slug),
    PRIMARY KEY (source_id, news_type_slug)
);

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER REFERENCES sources(id),
    title           TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    summary         TEXT,
    image_url       TEXT,
    published_at    TEXT,
    fetched_at      TEXT NOT NULL,
    content_hash    TEXT
);

CREATE TABLE IF NOT EXISTS article_news_types (
    article_id      INTEGER REFERENCES articles(id),
    news_type_slug  TEXT REFERENCES news_types(slug),
    PRIMARY KEY (article_id, news_type_slug)
);

CREATE TABLE IF NOT EXISTS article_regions (
    article_id  INTEGER REFERENCES articles(id),
    region_slug TEXT REFERENCES regions(slug),
    PRIMARY KEY (article_id, region_slug)
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);
"""


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
