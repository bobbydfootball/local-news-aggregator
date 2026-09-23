"""
Streamlit frontend for the local news aggregator.
Layout: one tab per news category (Waukesha, Sports, State, etc.), each
color-coded to match the category's color from news_types. Each tab shows
a flat list of that category's articles, most recent first -- no region
sub-grouping (removed earlier; county/region tagging wasn't reliable
enough to justify the extra visual layer -- see project history).

Theme: follows each viewer's own device light/dark setting automatically
-- no in-app selector. Streamlit's native widgets switch via the
[theme.light] / [theme.dark] tables in .streamlit/config.toml; this app's
own custom elements (cards, concert rows, joke, tab label colors) switch
via CSS variables and a prefers-color-scheme media query -- see
THEME_PALETTES and build_theme_css() below. Keep the two palettes in sync
with config.toml if either is changed.
Run: streamlit run app.py
"""
import streamlit as st
import streamlit.components.v1 as components
from datetime import datetime, timedelta, timezone
from src.db import get_conn, init_db
MAX_ARTICLE_AGE_DAYS = 2
SPORTS_MAX_ARTICLE_AGE_DAYS = 6   # Packers/Brewers/Bucks/Badgers/Local Sports
                                  # get a longer window -- dedicated official
                                  # feeds publish far less often than general
                                  # news sources (confirmed: an off-season UW
                                  # Badgers feed can go 3+ weeks between
                                  # posts), so a strict 2-day cutoff was
                                  # starving out genuinely relevant, correctly
                                  # -fetched content from those feeds.
EVENTS_MAX_ARTICLE_AGE_DAYS = 7   # Events previously had NO freshness cutoff
                                  # at all (see load_articles below for why:
                                  # published_at on an event reflects when
                                  # the SOURCE added it to their calendar
                                  # feed, not the event date itself, so
                                  # applying a cutoff always risked hiding a
                                  # genuinely upcoming event whose feed-add
                                  # date is old). A 7-day cutoff was added
                                  # anyway, as an explicit, accepted
                                  # trade-off -- events display was
                                  # otherwise accumulating stale entries
                                  # indefinitely whenever a source's feed
                                  # didn't promptly drop a past event on its
                                  # own. If real upcoming events start
                                  # disappearing under this cutoff, that's
                                  # this trade-off surfacing, not a bug --
                                  # revisit the cutoff length or exempt
                                  # specific sources rather than reverting
                                  # to no cutoff at all.
SPORTS_CATEGORIES = {"packers", "brewers", "bucks", "badgers", "local_sports"}

# ---------- Theme (follows device setting) ----------
#
# No manual selector, deliberately: Streamlit has no API to change its
# native theme at runtime, so a manual "Dark" choice on a light-mode
# device would darken our custom cards while Streamlit's own widgets
# stayed light. Following the device setting on both layers (config.toml
# for native widgets, prefers-color-scheme here for custom elements)
# keeps them in agreement, since both read the same OS/browser preference.
#
# These values mirror backgroundColor / secondaryBackgroundColor /
# textColor in config.toml's [theme.light] and [theme.dark] tables.
THEME_PALETTES = {
    "light": {
        "bg": "#FFFFFF",
        "card-bg": "#FAFAFA",
        "border": "#E5E7EB",
        "text": "#111827",
        "text-body": "#374151",
        "text-muted": "#6B7280",
    },
    "dark": {
        "bg": "#0F1117",
        "card-bg": "#1A1D24",
        "border": "#2D3139",
        "text": "#F3F4F6",
        "text-body": "#D1D5DB",
        "text-muted": "#9CA3AF",
    },
}
DARK_TAB_LIGHTEN_AMOUNT = 0.45   # several category colors (Brewers navy,
                                 # World dark teal) are unreadable as text
                                 # on a dark background as-is, so dark mode
                                 # mixes each one toward white by this much.
                                 # Computed from news_types, not hardcoded,
                                 # so it stays in sync with taxonomy.yaml.
st.set_page_config(page_title="Bo6's News Aggregator", page_icon="📰", layout="wide")
# ---------- Styling: colors come from CSS variables set per theme ----------
st.markdown(
    """
    <style>
    .stApp { background-color: var(--bg); color: var(--text); }
    .stApp h1, .stApp h2, .stApp h3 { color: var(--text); }
    .stApp [data-testid="stMarkdownContainer"] { color: var(--text); }
    [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] p { color: var(--text-muted) !important; }
    .stButton button {
        background-color: var(--card-bg);
        color: var(--text);
        border-color: var(--border);
    }
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
        color: var(--text-body);
    }
    .article-card {
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 14px;
        margin-bottom: 12px;
        background-color: var(--card-bg);
    }
    .article-title {
        font-weight: 700;
        font-size: 1.02rem;
        margin-bottom: 4px;
    }
    .article-title a { text-decoration: none; color: var(--text); }
    .article-title a:hover { text-decoration: underline; }
    .article-source {
        font-size: 0.8rem;
        color: var(--text-muted);
        margin-bottom: 6px;
    }
    .article-summary {
        font-size: 0.92rem;
        color: var(--text-body);
    }
    .muted-text { color: var(--text-muted); }
    /* Hide the default Streamlit header bar (deploy button, menu, etc.) */
    header[data-testid="stHeader"] {
        display: none;
    }
    /* Bigger, bold expander title (currently only used for the Ticketmaster
       concerts dropdown) -- Streamlit doesn't expose font-size on expander
       labels directly, so this targets the underlying summary element.
       Covers a couple of selector variants for version compatibility. */
    [data-testid="stExpander"] details {
        background-color: var(--card-bg);
        border-color: var(--border);
    }
    [data-testid="stExpander"] summary,
    [data-testid="stExpander"] summary p {
        font-size: 1.3rem !important;
        font-weight: 700 !important;
        color: var(--text) !important;
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
# Inject the iOS "Add to Home Screen" icon AND a custom home screen name.
# st.set_page_config's page_icon/page_title only control the browser tab
# favicon/title -- iOS Safari ignores both for the home screen and instead
# looks for a <link rel="apple-touch-icon"> tag (icon) and a
# <meta name="apple-mobile-web-app-title"> tag (name).
#
# This uses plain st.markdown(unsafe_allow_html=True) rather than
# components.html() -- an earlier attempt via components.html() (a
# sandboxed iframe reaching into window.parent.document) failed, matching
# the same failure we already saw with the keyboard-focus fix using that
# same technique. st.markdown's HTML injection is what's actually worked
# reliably elsewhere in this app (hiding the Streamlit header, coloring
# the tabs) since it inserts real DOM nodes into the actual page rather
# than being trapped in an isolated iframe. These tags aren't strictly
# inside <head> this way, but browsers generally still honor link/meta
# tags wherever they land in the DOM.
st.markdown(
    '<link rel="apple-touch-icon" sizes="180x180" href="app/static/apple-touch-icon.png">'
    '<meta name="apple-mobile-web-app-title" content="Bo6 News">',
    unsafe_allow_html=True,
)


def lighten(hex_color: str, amount: float) -> str:
    """Mix a #RRGGBB color toward white by `amount` (0..1). Returns the
    input unchanged if it isn't a parseable 6-digit hex value."""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, AttributeError):
        return hex_color
    r, g, b = (round(c + (255 - c) * amount) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


def build_theme_css(news_types) -> str:
    """Return a <style> block setting the CSS variables: light values by
    default, dark values under a prefers-color-scheme: dark media query.
    Includes one --tab-N variable per category so tab label colors can be
    lightened in dark mode."""
    def vars_block(palette_name):
        palette = THEME_PALETTES[palette_name]
        lines = [f"--{k}: {v};" for k, v in palette.items()]
        for i, nt in enumerate(news_types):
            color = nt["color"]
            if palette_name == "dark":
                color = lighten(color, DARK_TAB_LIGHTEN_AMOUNT)
            lines.append(f"--tab-{i + 1}: {color};")
        return " ".join(lines)

    css = (
        f":root {{ {vars_block('light')} }} "
        f"@media (prefers-color-scheme: dark) {{ :root {{ {vars_block('dark')} }} }}"
    )
    return f"<style>{css}</style>"


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
    # EXCEPT that "events" uses its own EVENTS_MAX_ARTICLE_AGE_DAYS window
    # instead. Event calendar feeds (e.g. Shepherd Express) set their RSS
    # pubDate to when the entry was added to their system, which can be
    # weeks before the event itself happens, so a strict news-style cutoff
    # can hide a genuinely upcoming event. A 7-day cutoff is applied anyway
    # as an accepted trade-off against stale entries accumulating
    # indefinitely -- see EVENTS_MAX_ARTICLE_AGE_DAYS above for the full
    # reasoning and what to do if this starts hiding real upcoming events.
    with get_conn() as conn:
        if news_type_slug == "events":
            # Exclude Ticketmaster concerts here -- they're rendered
            # separately in a compact expander (load_concerts below), not
            # as full article cards.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=EVENTS_MAX_ARTICLE_AGE_DAYS)).isoformat()
            rows = conn.execute(
                """
                SELECT a.id, a.title, a.url, a.summary, a.image_url, a.published_at,
                       s.name AS source_name, ar.region_slug
                FROM articles a
                JOIN article_news_types ant ON ant.article_id = a.id
                JOIN sources s ON s.id = a.source_id
                LEFT JOIN article_regions ar ON ar.article_id = a.id
                WHERE ant.news_type_slug = ? AND s.name != 'Ticketmaster - Wisconsin Concerts'
                      AND a.published_at >= ?
                ORDER BY a.published_at DESC
                LIMIT 200
                """,
                (news_type_slug, cutoff),
            ).fetchall()
        else:
            max_age = SPORTS_MAX_ARTICLE_AGE_DAYS if news_type_slug in SPORTS_CATEGORIES else MAX_ARTICLE_AGE_DAYS
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()
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
        <div style="padding:6px 0; border-bottom:1px solid var(--border); font-size:0.92rem;">
            <strong>{date_display}</strong> ·
            <a href="{concert['url']}" target="_blank" style="color:var(--text); text-decoration:none; font-weight:600;">{concert['title']}</a>
            <span class="muted-text"> — {concert['summary'] or ''}</span>
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
    news_types = load_news_types()

    title_col, joke_col = st.columns([3, 2])
    with title_col:
        st.title("📰 Bo6's News Aggregator")
    with joke_col:
        joke = load_joke()
        if joke:
            st.markdown(
                f"<div class='muted-text' style='padding-top:1.9rem; font-style:italic; font-size:0.95rem;'>😄 {joke}</div>",
                unsafe_allow_html=True,
            )

    st.caption("Local news for Waukesha, Waukesha County, Milwaukee County & Wisconsin")
    st.markdown(build_theme_css(news_types), unsafe_allow_html=True)

    if not news_types:
        st.warning(
            "No data yet. Run `python -m src.load_config` then `python -m src.ingest` "
            "to populate the database before launching the app."
        )
        return
    # Color-code each tab to match its category's color, pulled dynamically
    # from news_types (not hardcoded) so tab colors stay in sync if a
    # category's color is ever changed. The actual color value comes from
    # the --tab-N CSS variable set by build_theme_css(), so dark mode can
    # swap in a lightened version of each category color.
    #
    # Confirmed via direct browser inspection (right-click a tab -> Inspect)
    # that the current Streamlit version renders each tab as:
    #   <div data-testid="stTab" aria-selected="false" ...>
    #     <div data-testid="stMarkdownContainer"><p>Label text</p></div>
    #   </div>
    # Two earlier attempts based on Streamlit community docs targeted
    # button[data-baseweb="tab"] and .stTabs [data-baseweb="tab"] -- both
    # wrong for this version, since the element is a <div> with a
    # Streamlit-native data-testid, not a <button> with a BaseWeb attribute.
    # The inner <p> is targeted explicitly (not just the outer div) since
    # the markdown container may otherwise override inherited text color.
    tab_css_rules = "\n".join(
        f'[data-testid="stTab"]:nth-child({i + 1}) [data-testid="stMarkdownContainer"] p {{ '
        f'color: var(--tab-{i + 1}) !important; }}\n'
        f'[data-testid="stTab"]:nth-child({i + 1})[aria-selected="true"] {{ '
        f'border-bottom-color: var(--tab-{i + 1}) !important; }}'
        for i in range(len(news_types))
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
                                f"<div class='muted-text' style='text-align:center; padding-top:6px;'>Page {page + 1} of {total_pages}</div>",
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
            for article in articles[:45]:
                render_article_card(article)
if __name__ == "__main__":
    main()
