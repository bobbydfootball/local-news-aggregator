"""
Streamlit frontend for the local news aggregator.

Layout: News Type (top level) -> Geography (second level) -> article cards.
Run: streamlit run app.py
"""

import streamlit as st
import streamlit.components.v1 as components
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
    /* Hide the default Streamlit header bar (deploy button, menu, etc.) */
    header[data-testid="stHeader"] {
        display: none;
    }
    /* Bigger, bold expander title (currently only used for the Ticketmaster
       concerts dropdown) -- Streamlit doesn't expose font-size on expander
       labels directly, so this targets the underlying summary element.
       Covers a couple of selector variants for version compatibility. */
    [data-testid="stExpander"] summary,
    [data-testid="stExpander"] summary p {
        font-size: 1.3rem !important;
        font-weight: 700 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Focus the page body on load so arrow keys / Tab / Page Down work
# immediately without the user needing to click into the page first.
# st.markdown's <script> tags don't reliably execute in the browser, so
# this uses components.html instead, which renders in a same-origin iframe
# and can reach the actual page via window.parent. Focus is grabbed both
# on the parent window itself and on its body element, and retried a few
# times since Streamlit re-renders shortly after first load and can reset
# focus in between.
components.html(
    """
    <script>
    function grabFocus() {
        try {
            window.parent.focus();
            window.parent.document.body.setAttribute('tabindex', '-1');
            window.parent.document.body.focus();
        } catch (e) {}
    }
    grabFocus();
    setTimeout(grabFocus, 300);
    setTimeout(grabFocus, 1000);
    </script>
    """,
    height=0,
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
    # Only show articles published within the last MAX_ARTICLE_AGE_DAYS --
    # EXCEPT for "events". Event calendar feeds (e.g. Shepherd Express) set
    # their RSS pubDate to when the entry was added to their system, which
    # can be weeks before the event itself happens. Applying the same
    # "recently published" rule as news would make upcoming events vanish
    # shortly after being added, even though they're still relevant. So
    # events skip the freshness filter entirely and show everything
    # currently in the feed; the source's own feed naturally drops events
    # once they're past.
    with get_conn() as conn:
        if news_type_slug == "events":
            # Exclude Ticketmaster concerts here -- they're rendered
            # separately in a compact expander (load_concerts below), not
            # as full article cards.
            rows = conn.execute(
                """
                SELECT a.id, a.title, a.url, a.summary, a.image_url, a.published_at,
                       s.name AS source_name, ar.region_slug
                FROM articles a
                JOIN article_news_types ant ON ant.article_id = a.id
                JOIN sources s ON s.id = a.source_id
                LEFT JOIN article_regions ar ON ar.article_id = a.id
                WHERE ant.news_type_slug = ? AND s.name != 'Ticketmaster - Wisconsin Concerts'
                ORDER BY a.published_at DESC
                LIMIT 200
                """,
                (news_type_slug,),
            ).fetchall()
        else:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)).isoformat()
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


@st.cache_data(ttl=300)
def load_concerts():
    # Soonest-first (ASC), unlike news articles which sort newest-first --
    # a concert next week matters more than one three months out, the
    # opposite of how article recency works.
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.url, a.summary, a.image_url, a.published_at
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE s.name = 'Ticketmaster - Wisconsin Concerts'
            ORDER BY a.published_at ASC
            LIMIT 800
            """
        ).fetchall()
        return [dict(r) for r in rows]


CONCERTS_PER_PAGE = 50


def render_concert_row(concert):
    date_display = concert["published_at"][:10] if concert["published_at"] else "TBA"
    st.markdown(
        f"""
        <div style="padding:6px 0; border-bottom:1px solid #E5E7EB; font-size:0.92rem;">
            <strong>{date_display}</strong> ·
            <a href="{concert['url']}" target="_blank" style="color:#111827; text-decoration:none; font-weight:600;">{concert['title']}</a>
            <span style="color:#6B7280;"> — {concert['summary'] or ''}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=300)
def load_joke():
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'daily_joke'").fetchone()
        return row["value"] if row else None


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

    title_col, joke_col = st.columns([3, 2])
    with title_col:
        st.title("📰 Bo6's News Aggregator")
    with joke_col:
        joke = load_joke()
        if joke:
            st.markdown(
                f"<div style='padding-top:1.9rem; font-style:italic; color:#6B7280; font-size:0.95rem;'>😄 {joke}</div>",
                unsafe_allow_html=True,
            )

    st.caption("Local news for Waukesha, Waukesha County, Milwaukee County & Wisconsin")

    news_types = load_news_types()

    if not news_types:
        st.warning(
            "No data yet. Run `python -m src.load_config` then `python -m src.ingest` "
            "to populate the database before launching the app."
        )
        return

    # Color-code each tab to match its category's color, pulled dynamically
    # from news_types (not hardcoded) so tab colors stay in sync if a
    # category's color is ever changed. Streamlit's tabs are built on the
    # BaseWeb tab component under the hood, hence targeting
    # button[data-baseweb="tab"] rather than a Streamlit-specific class.
    tab_css_rules = "\n".join(
        f'.stTabs [data-baseweb="tab-list"] > [data-baseweb="tab"]:nth-child({i + 1}) {{ '
        f'color: {nt["color"]} !important; }}\n'
        f'.stTabs [data-baseweb="tab-list"] > [data-baseweb="tab"]:nth-child({i + 1})[aria-selected="true"] {{ '
        f'border-bottom-color: {nt["color"]} !important; }}'
        for i, nt in enumerate(news_types)
    )
    st.markdown(f"<style>{tab_css_rules}</style>", unsafe_allow_html=True)
    
    tabs = st.tabs([nt["name"] for nt in news_types])

    for tab, nt in zip(tabs, news_types):
        with tab:
            articles = load_articles(nt["slug"])

            # Concerts render separately as compact rows in a collapsed
            # expander, only under Events -- checked before the "no
            # articles" early-out below, since concerts can have content
            # even when the regular events article list doesn't.
            has_concerts = False
            if nt["slug"] == "events":
                concerts = load_concerts()
                has_concerts = bool(concerts)
                if has_concerts:
                    total_pages = max(1, (len(concerts) - 1) // CONCERTS_PER_PAGE + 1)
                    if "concert_page" not in st.session_state:
                        st.session_state.concert_page = 0
                    st.session_state.concert_page = min(st.session_state.concert_page, total_pages - 1)

                    with st.expander(f"🎵 {len(concerts)} upcoming Wisconsin concerts (Ticketmaster)"):
                        page = st.session_state.concert_page
                        start = page * CONCERTS_PER_PAGE
                        for concert in concerts[start:start + CONCERTS_PER_PAGE]:
                            render_concert_row(concert)

                        nav_prev, nav_label, nav_next = st.columns([1, 2, 1])
                        with nav_prev:
                            if st.button("◀ Previous", disabled=(page == 0), key="concert_prev"):
                                st.session_state.concert_page -= 1
                                st.rerun()
                        with nav_label:
                            st.markdown(
                                f"<div style='text-align:center; padding-top:6px; color:#6B7280;'>Page {page + 1} of {total_pages}</div>",
                                unsafe_allow_html=True,
                            )
                        with nav_next:
                            if st.button("Next ▶", disabled=(page >= total_pages - 1), key="concert_next"):
                                st.session_state.concert_page += 1
                                st.rerun()

            if not articles:
                if nt["slug"] == "events":
                    if not has_concerts:
                        st.caption("Coming soon — this section is a placeholder until concerts/festivals support is built.")
                else:
                    st.caption("No articles yet for this section.")
                continue

            for article in articles[:20]:
                render_article_card(article)


if __name__ == "__main__":
    main()
