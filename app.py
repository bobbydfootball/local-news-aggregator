"""
Streamlit frontend for the local news aggregator.

Layout: News Type (top level) -> Geography (second level) -> article cards.
Run: streamlit run app.py
"""

import streamlit as st
from src.db import get_conn, init_db

st.set_page_config(page_title="Waukesha Area News", page_icon="📰", layout="wide")

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
    .region-header {
        font-weight: 600;
        font-size: 1.05rem;
        margin-top: 0.75rem;
        margin-bottom: 0.5rem;
        color: #374151;
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
def load_regions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT slug, name, level, sort_order FROM regions ORDER BY sort_order"
        ).fetchall()
        return [dict(r) for r in rows]


@st.cache_data(ttl=300)
def load_articles(news_type_slug: str):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.url, a.summary, a.image_url, a.published_at,
                   s.name AS source_name, ar.region_slug
            FROM articles a
            JOIN article_news_types ant ON ant.article_id = a.id
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_regions ar ON ar.article_id = a.id
            WHERE ant.news_type_slug = ?
            ORDER BY a.published_at DESC
            LIMIT 200
            """,
            (news_type_slug,),
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
    st.title("📰 Waukesha Area News")
    st.caption("Local news for Waukesha, Waukesha County, Milwaukee County & Wisconsin")

    news_types = load_news_types()
    regions = load_regions()
    region_lookup = {r["slug"]: r for r in regions}

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
            st.caption("No articles yet for this section.")
            continue

        # Group by region, in region sort order, then by recency within region
        by_region = {}
        for a in articles:
            by_region.setdefault(a["region_slug"], []).append(a)

        ordered_region_slugs = sorted(
            by_region.keys(),
            key=lambda s: region_lookup[s]["sort_order"] if s in region_lookup else 999,
        )

        for region_slug in ordered_region_slugs:
            region_name = region_lookup.get(region_slug, {}).get("name", "Other")
            st.markdown(f'<div class="region-header">{region_name}</div>', unsafe_allow_html=True)
            for article in by_region[region_slug][:10]:
                render_article_card(article)


if __name__ == "__main__":
    main()
