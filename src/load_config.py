"""
Loads config/taxonomy.yaml and config/sources.yaml into the database.

Run this after editing either config file (e.g. after adding a new region,
news type, or source) to sync the DB. Safe to re-run -- uses upserts.
"""

import yaml
from pathlib import Path
from src.db import get_conn, init_db

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_taxonomy():
    with open(CONFIG_DIR / "taxonomy.yaml") as f:
        taxonomy = yaml.safe_load(f)

    with get_conn() as conn:
        for nt in taxonomy["news_types"]:
            conn.execute(
                """INSERT INTO news_types (slug, name, color, sort_order)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                     name=excluded.name, color=excluded.color, sort_order=excluded.sort_order""",
                (nt["slug"], nt["name"], nt["color"], nt["sort_order"]),
            )

        # Insert regions in dependency order (parents before children) --
        # simplest approach: insert twice so parent_slug FK always resolves.
        for _ in range(2):
            for r in taxonomy["regions"]:
                conn.execute(
                    """INSERT INTO regions (slug, name, level, parent_slug, sort_order)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(slug) DO UPDATE SET
                         name=excluded.name, level=excluded.level,
                         parent_slug=excluded.parent_slug, sort_order=excluded.sort_order""",
                    (r["slug"], r["name"], r["level"], r.get("parent_slug"), r["sort_order"]),
                )

    return taxonomy


def load_sources():
    with open(CONFIG_DIR / "sources.yaml") as f:
        sources = yaml.safe_load(f)["sources"]

    with get_conn() as conn:
        for s in sources:
            cur = conn.execute(
                """INSERT INTO sources (name, base_url, feed_url, default_region, sports_scope, status)
                   VALUES (?, ?, ?, ?, ?, 'active')
                   ON CONFLICT(feed_url) DO UPDATE SET
                     name=excluded.name, base_url=excluded.base_url,
                     default_region=excluded.default_region, sports_scope=excluded.sports_scope
                   """,
                (s["name"], s.get("base_url"), s["feed_url"], s["default_region"], s.get("sports_scope")),
            )
            source_id = conn.execute(
                "SELECT id FROM sources WHERE feed_url = ?", (s["feed_url"],)
            ).fetchone()["id"]

            conn.execute("DELETE FROM source_news_types WHERE source_id = ?", (source_id,))
            for nt_slug in s["default_news_types"]:
                conn.execute(
                    "INSERT INTO source_news_types (source_id, news_type_slug) VALUES (?, ?)",
                    (source_id, nt_slug),
                )

    return sources


if __name__ == "__main__":
    init_db()
    taxonomy = load_taxonomy()
    sources = load_sources()
    print(f"Loaded {len(taxonomy['news_types'])} news types, {len(taxonomy['regions'])} regions, "
          f"{len(sources)} sources.")
