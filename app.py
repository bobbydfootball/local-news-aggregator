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
    </style>
    """,
    unsafe_allow_html=True,
)

# Focus the page body on load so arrow keys / Tab / Page Down work
# immediately without the user needing to click into the page first.
# st.markdown's <script> tags don't reliably execute in the browser, so
# this uses components.html instead, which renders in a same-origin iframe
# and can reach the actual page via window.parent.
components.html(
    """
    <script>
    window.parent.document.body.setAttribute('tabindex', '-1');
    window.parent.document.body.focus();
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
    # "recently published" rule as news would make
