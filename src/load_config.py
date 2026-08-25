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

    current_news_type_slugs = [nt["slug"] for nt in taxonomy["news_types"]]

    with get_conn() as conn:
        for nt in taxonomy["news_types"]:
            conn.execute(
                """INSERT INTO news_types (slug, name, color, sort_order)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                     name=excluded.name, color=excluded.color, sort_order=excluded.sort_order""",
                (nt["slug"], nt["name"], nt["color"], nt["sort_order"]),
            )

        # Remove news_types that were deleted/renamed out of taxonomy.yaml.
        # Without this, old category rows (e.g. a category you renamed two
        # versions ago) linger in the DB forever and the app shows a mix of
        # every taxonomy version you've ever used -- this was a real bug
        # that caused old section names to keep showing up after several
        # rounds of category edits. article_news_types rows pointing at a
        # removed category are deleted first (their articles just lose that
        # tag -- if that was an article's only tag, it stops appearing
        # anywhere, which is correct: it was tagged under a category that
        # no longer exists).
        if current_news_type_slugs:
            placeholders = ",".join("?" for _ in current_news_type_slugs)
            conn.execute(
                f"DELETE FROM article_news_types WHERE news_type_slug NOT IN ({placeholders})",
                current_news_type_slugs,
            )
            conn.execute(
                f"DELETE FROM source_news_types WHERE news_type_slug NOT IN ({placeholders})",
                current_news_type_slugs,
            )
            conn.execute(
                f"DELETE FROM news_types WHERE slug NOT IN ({placeholders})",
                current_news_type_slugs,
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

    current_feed_urls = [s["feed_url"] for s in sources]

    with get_conn() as conn:
        for s in sources:
            # Reset status to 'active' here so a source that comes back into
            # sources.yaml after being removed gets re-checked fresh by
            # verify_feeds, rather than staying stuck on whatever status it
            # had before it was removed.
            cur = conn.execute(
                """INSERT INTO sources (name, base_url, feed_url, default_region, sports_scope, sports_keyword, sports_keyword_scope, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                   ON CONFLICT(feed_url) DO UPDATE SET
                     name=excluded.name, base_url=excluded.base_url,
                     default_region=excluded.default_region, sports_scope=excluded.sports_scope,
                     sports_keyword=excluded.sports_keyword, sports_keyword_scope=excluded.sports_keyword_scope,
                     status='active'
                   """,
                (s["name"], s.get("base_url"), s["feed_url"], s["default_region"], s.get("sports_scope"),
                 s.get("sports_keyword"), s.get("sports_keyword_scope")),
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

        # Anything in the DB whose feed_url is no longer in sources.yaml was
        # deliberately removed by editing the config -- mark it 'removed' so
        # verify_feeds and the app stop surfacing it. We don't hard-delete
        # the row because articles.source_id may still reference it.
        if current_feed_urls:
            placeholders = ",".join("?" for _ in current_feed_urls)
            conn.execute(
                f"UPDATE sources SET status = 'removed' WHERE feed_url NOT IN ({placeholders})",
                current_feed_urls,
            )

    return sources


if __name__ == "__main__":
    init_db()
    taxonomy = load_taxonomy()
    sources = load_sources()
    print(f"Loaded {len(taxonomy['news_types'])} news types, {len(taxonomy['regions'])} regions, "
          f"{len(sources)} sources.")
