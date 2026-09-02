"""
One-off cleanup: merges pre-existing duplicate articles that were
inserted under two different URLs for the same underlying BLOX article
(see compute_content_hash in src/ingest.py for the full explanation --
GMToday/Capital Times allow an article's SEO slug to change after
publication while its "article_<uuid>.html" identifier stays fixed;
dedup used to be keyed on the full URL, so a slug change produced a
second, duplicate row for the same story).

This only needs to run ONCE, after deploying the content_hash-based
dedup fix in ingest.py -- that fix stops NEW duplicates from being
created, but does nothing about ones already sitting in the database.
Safe to run more than once regardless: it's a no-op once no duplicates
remain.

For every existing article, this also backfills the content_hash
column (previously unused) from its current url, so future ingest runs
can rely on it being populated for every row, not just ones touched
since the fix went live.

Usage: python -m src.dedupe_existing
"""
from src.db import get_conn, init_db
from src.ingest import compute_content_hash


def run_dedupe():
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT id, url, fetched_at FROM articles").fetchall()

        groups = {}
        for row in rows:
            content_hash = compute_content_hash(row["url"])
            conn.execute(
                "UPDATE articles SET content_hash = ? WHERE id = ?",
                (content_hash, row["id"]),
            )
            groups.setdefault(content_hash, []).append(row)

        merged = 0
        for content_hash, group_rows in groups.items():
            if len(group_rows) < 2:
                continue
            # Keep whichever row was fetched most recently -- most
            # likely to reflect the article's current title/slug --
            # and delete the rest, along with their tag/region rows.
            group_rows.sort(key=lambda r: r["fetched_at"] or "", reverse=True)
            keep = group_rows[0]
            for dupe in group_rows[1:]:
                conn.execute("DELETE FROM article_news_types WHERE article_id = ?", (dupe["id"],))
                conn.execute("DELETE FROM article_regions WHERE article_id = ?", (dupe["id"],))
                conn.execute("DELETE FROM articles WHERE id = ?", (dupe["id"],))
                merged += 1
            print(f"Merged {len(group_rows) - 1} duplicate(s), kept: {keep['url']}")

    print(f"\nDone. Removed {merged} duplicate row(s).")


if __name__ == "__main__":
    run_dedupe()
