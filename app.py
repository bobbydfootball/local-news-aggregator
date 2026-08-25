"""
Streamlit frontend for the local news aggregator.

Layout: News Type sections, flat article lists within each (no region
sub-grouping -- see notes in main() for why that was removed).
Run: streamlit run app.py
"""

import streamlit as st
from datetime import datetime, timedelta, timezone
from src.db import get_conn, init_db

MAX_ARTICLE_AGE_DAYS = 2

st.set_page_config(page_title="Bo6's News Aggregator", page_icon="📰", layout="wide")

# ---------- Styling: white background, colorful accents ----------
st.markdown(
    """
    <style>
    .stApp { background-color: #FFFFFF; }
    .section-header {
        padding: 10px 16px;
        border-radius: 8px;
        color: white;
        font-weight: 700;
        font-size: 1.4rem;
        margin-top: 1.5rem;
        margin-bottom: 0.75rem;
    }
    .article-card {
        border: 1px solid #E5E7EB;
        border-radius: 10px;
        padding: 14px;
        margin-bottom: 12px;
        background-color: #FAFAFA;
    }
    .article-title {
        font-weight: 700;
        font-size: 1.02rem;
        margin-bottom: 4px;
    }
    .article-title a { text-decoration: none; color: #111827; }
    .article-title a:hover { text-decoration: underline; }
    .article-source {
        font-size: 0.8rem;
        color: #6B7280;
        margin-bottom: 6px;
    }
    .article-summary {
        font-size: 0.92rem;
        color: #374151;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=300)
def load_news_types():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT slug, name, color FROM news_types ORDER BY sort_order"
        ).fetchall()
        # Convert sqlite3.Row -> plain dict. st.cache_data pickles whatever
        # a cached function returns, and sqlite3.Row doesn't pickle reliably
        # (this is what caused UnserializableReturnValueError on deploy).
        return [dict(r) for r in rows]


@st.cache_data(ttl=300)
def load_articles(news_type_slug: str):
    # Only show articles published within the last MAX_ARTICLE_AGE_DAYS.
    # Cutoff is computed in Python (not SQLite's datetime()) and compared
    # as an ISO8601 string -- this format sorts/compares correctly as plain
    # text as long as both sides use the same representation, which avoids
    # relying on SQLite's own date-parsing quirks. Articles with no
    # published_at (a feed that didn't provide one) are naturally excluded,
    # since a NULL comparison against >= never evaluates true in SQL.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.url, a.summary, a.image_url, a.published_at,
                   s.name AS source_name, ar.region_slug
            FROM articles a
            JOIN article_news_types ant ON ant.article_id = a.id
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_regions ar ON ar.article_id = a.id
            WHERE ant.news_type_slug = ? AND a.published_at >= ?
            ORDER BY a.published_at DESC
            LIMIT 200
            """,
            (news_type_slug, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def render_article_card(article):
    cols = st.columns([1, 4]) if article["image_url"] else [st.container()]
    if article["image_url"]:
        with cols[0]:
            st.image(article["image_url"], use_container_width=True)
        body_col = cols[1]
    else:
        body_col = cols[0]

    with body_col:
        st.markdown(
            f"""
            <div class="article-card">
                <div class="article-title"><a href="{article['url']}" target="_blank">{article['title']}</a></div>
                <div class="article-source">{article['source_name'] or ''}</div>
                <div class="article-summary">{article['summary'] or ''}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def main():
    init_db()
    st.title("📰 Bo6's News Aggregator")
    st.caption("Local news for Waukesha, Waukesha County, Milwaukee County & Wisconsin")

    news_types = load_news_types()

    if not news_types:
        st.warning(
            "No data yet. Run `python -m src.load_config` then `python -m src.ingest` "
            "to populate the database before launching the app."
        )
        return

    for nt in news_types:
        st.markdown(
            f'<div class="section-header" style="background-color:{nt["color"]}">{nt["name"]}</div>',
            unsafe_allow_html=True,
        )
        articles = load_articles(nt["slug"])
        if not articles:
            if nt["slug"] == "events":
                st.caption("Coming soon — this section is a placeholder until concerts/festivals support is built.")
            else:
                st.caption("No articles yet for this section.")
            continue

        # Flat list, most recent first -- no region sub-grouping. The
        # region/county labels on articles weren't reliably matching their
        # actual content (see project history), so rather than keep fixing
        # per-source region tagging, the display was simplified to avoid
        # showing a county label that might not be accurate. Region data
        # is still stored (article_regions table) in case a more reliable
        # grouping approach is worth revisiting later.
        for article in articles[:20]:
            render_article_card(article)


if __name__ == "__main__":
    main()
