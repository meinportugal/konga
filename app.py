"""
Konga Selling — Analytics Dashboard
app.py  (entry point — place konga_analytics.db in the same directory)

Requirements:
    pip install streamlit duckdb plotly pandas streamlit-echarts
"""
import math
import streamlit as st
import duckdb
import pandas as pd
import plotly.graph_objects as go
from streamlit_echarts import st_echarts
from datetime import date, timedelta
import boto3
import os

# ══════════════════════════════════════════════════════════════════════
# PAGE CONFIG  — must be the very first Streamlit call
# ══════════════════════════════════════════════════════════════════════
st.set_page_config(
    layout="wide",
    page_title="Konga Selling",
    page_icon="🛍️",
    initial_sidebar_state="expanded",
)

# ══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════
DB_PATH = "/tmp/konga_analytics.db"

# 10-colour palette for top-level categories; "Other" always grey
CAT_PALETTE = [
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
    "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#86BCB6",
]
OTHER_CLR = "#B0B0B0"


# ══════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════
def download_database():

    s3 = boto3.client(
        "s3",
        endpoint_url=st.secrets["R2_ENDPOINT"],
        aws_access_key_id=st.secrets["R2_ACCESS_KEY"],
        aws_secret_access_key=st.secrets["R2_SECRET_KEY"],
        region_name="auto",
    )

    s3.download_file(
        st.secrets["R2_BUCKET"],
        "konga_analytics.db",
        DB_PATH,
    )


@st.cache_resource
def _db():

    if not os.path.exists(DB_PATH):
        print(st.secrets["R2_ENDPOINT"])
        print(st.secrets["R2_BUCKET"])
        print(st.secrets["R2_ACCESS_KEY"])
        print(st.secrets["R2_SECRET_KEY"])
        download_database()

    return duckdb.connect(DB_PATH, read_only=True)


def q(sql: str, params: list = None) -> pd.DataFrame:
    """Execute a query and return a DataFrame; shows error on failure."""
    try:
        return _db().execute(sql, params or []).df()
    except Exception as ex:
        st.error(f"DB error: {ex}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════
# SAFE NUMERIC HELPER
# ══════════════════════════════════════════════════════════════════════
def safe(v, default=0.0):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return default
    return float(v)


# ══════════════════════════════════════════════════════════════════════
# FORMATTING
# ══════════════════════════════════════════════════════════════════════
def fcur(v):
    """Format as Naira currency with k/m/b abbreviation."""
    if v is None:
        return "N/A"
    av = abs(v)
    if av >= 1e9:
        return f"₦{v / 1e9:.1f}b"
    if av >= 1e6:
        return f"₦{v / 1e6:.1f}m"
    if av >= 1e3:
        return f"₦{v / 1e3:.1f}k"
    return f"₦{v:.0f}"


def fnum(v):
    """Format large integers with k/m abbreviation."""
    if v is None:
        return "N/A"
    av = abs(v)
    if av >= 1e6:
        return f"{v / 1e6:.1f}m"
    if av >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{int(v)}"


def trnd(curr, prev, suffix="") -> str:
    """Return coloured HTML trend badge (▲/▼ + %)."""
    p = safe(prev)
    if p == 0:
        return ""
    d = (safe(curr) - p) / abs(p) * 100
    clr = "#22c55e" if d >= 0 else "#ef4444"
    arr = "▲" if d >= 0 else "▼"
    sfx = f" {suffix}" if suffix else ""
    return (
        f'<span style="color:{clr};font-size:11px">'
        f"{arr}&nbsp;{abs(d):.1f}%{sfx}</span>"
    )


# ══════════════════════════════════════════════════════════════════════
# DATE HELPERS
# ══════════════════════════════════════════════════════════════════════
@st.cache_data
def date_bounds():
    df = q(
        "SELECT MIN(date_snapshot)::DATE mn, MAX(date_snapshot)::DATE mx "
        "FROM products_optimized"
    )
    if df.empty:
        today = date.today()
        return today - timedelta(days=90), today
    return df["mn"].iloc[0], df["mx"].iloc[0]


def prev_window(s: date, e: date):
    """Return (prev_start, prev_end) — same length shifted back."""
    n = (e - s).days + 1
    return s - timedelta(days=n), e - timedelta(days=n)


# ══════════════════════════════════════════════════════════════════════
# SESSION STATE
# ══════════════════════════════════════════════════════════════════════
if "donut_path" not in st.session_state:
    st.session_state.donut_path: list = []   # breadcrumb: list of category name strings


# ══════════════════════════════════════════════════════════════════════
# CATEGORY COLORS  — assigned once per session, consistent across widgets
# ══════════════════════════════════════════════════════════════════════
def cat_colors() -> dict:
    if "cat_colors" not in st.session_state:
        df = q(
            "SELECT DISTINCT level2_name n FROM v_daily_revenue_by_category "
            "WHERE level2_name IS NOT NULL AND trim(level2_name) != '' "
            "ORDER BY n"
        )
        cats = df["n"].tolist() if not df.empty else []
        clrs = {c: CAT_PALETTE[i % len(CAT_PALETTE)] for i, c in enumerate(cats)}
        clrs["Other"] = OTHER_CLR
        st.session_state.cat_colors = clrs
    return st.session_state.cat_colors


# ══════════════════════════════════════════════════════════════════════
# DATA LOADERS
# ══════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300)
def load_kpis(s, e, ps, pe, path: tuple):
    """All KPI metrics, optionally scoped to a category drill level."""
    nd = (e - s).days + 1

    # Category filter — mirrors donut drill level
    if len(path) == 0:
        flt, fp = "", []
    elif len(path) == 1:
        flt, fp = "AND level2_name=?", [path[0]]
    else:
        flt, fp = "AND level3_name=?", [path[-1]]

    cat = len(path) > 0   # True when category-scoped

    # ── Revenue ────────────────────────────────────────────────────
    if not cat:
        df_rev = q(
            "SELECT "
            "  SUM(CASE WHEN date_snapshot BETWEEN ? AND ? THEN quantity_sold*safe_price END) cr,"
            "  SUM(CASE WHEN date_snapshot BETWEEN ? AND ? THEN quantity_sold*safe_price END) pr "
            "FROM products_optimized WHERE date_snapshot BETWEEN ? AND ?",
            [s, e, ps, pe, ps, e],
        )
    else:
        df_rev = q(
            f"SELECT "
            f"  SUM(CASE WHEN date_snapshot BETWEEN ? AND ? THEN revenue END) cr,"
            f"  SUM(CASE WHEN date_snapshot BETWEEN ? AND ? THEN revenue END) pr "
            f"FROM v_daily_revenue_by_category "
            f"WHERE date_snapshot BETWEEN ? AND ? {flt}",
            [s, e, ps, pe, ps, e] + fp,
        )
    cr = safe(df_rev["cr"].iloc[0])
    pr = safe(df_rev["pr"].iloc[0])

    # ── Total Shops ────────────────────────────────────────────────
    # Root: official seller registry; scoped: sellers who list in that category
    if not cat:
        sc = safe(q("SELECT COUNT(DISTINCT seller_id) n FROM sellers_optimized "
                    "WHERE date_snapshot BETWEEN ? AND ?", [s, e])["n"].iloc[0])
        sp = safe(q("SELECT COUNT(DISTINCT seller_id) n FROM sellers_optimized "
                    "WHERE date_snapshot BETWEEN ? AND ?", [ps, pe])["n"].iloc[0])
    else:
        sc = safe(q(f"SELECT COUNT(DISTINCT seller_id) n "
                    f"FROM v_daily_revenue_by_category "
                    f"WHERE date_snapshot BETWEEN ? AND ? {flt}",
                    [s, e] + fp)["n"].iloc[0])
        sp = safe(q(f"SELECT COUNT(DISTINCT seller_id) n "
                    f"FROM v_daily_revenue_by_category "
                    f"WHERE date_snapshot BETWEEN ? AND ? {flt}",
                    [ps, pe] + fp)["n"].iloc[0])

    # ── Active Shops % ─────────────────────────────────────────────
    asc = safe(q(f"SELECT COUNT(DISTINCT seller_id) n "
                 f"FROM v_daily_revenue_by_category "
                 f"WHERE date_snapshot BETWEEN ? AND ? AND quantity_sold>0 {flt}",
                 [s, e] + fp)["n"].iloc[0])
    asp = safe(q(f"SELECT COUNT(DISTINCT seller_id) n "
                 f"FROM v_daily_revenue_by_category "
                 f"WHERE date_snapshot BETWEEN ? AND ? AND quantity_sold>0 {flt}",
                 [ps, pe] + fp)["n"].iloc[0])
    apct_c = asc / sc * 100 if sc else 0
    apct_p = asp / sp * 100 if sp else 0

    # ── Total Items / SKU ──────────────────────────────────────────
    ic = safe(q(f"SELECT COUNT(DISTINCT product_id) n "
                f"FROM v_daily_revenue_by_category "
                f"WHERE date_snapshot BETWEEN ? AND ? {flt}",
                [s, e] + fp)["n"].iloc[0])
    ip = safe(q(f"SELECT COUNT(DISTINCT product_id) n "
                f"FROM v_daily_revenue_by_category "
                f"WHERE date_snapshot BETWEEN ? AND ? {flt}",
                [ps, pe] + fp)["n"].iloc[0])

    # ── Active Items % ─────────────────────────────────────────────
    aic = safe(q(f"SELECT COUNT(DISTINCT product_id) n "
                 f"FROM v_daily_revenue_by_category "
                 f"WHERE date_snapshot BETWEEN ? AND ? AND quantity_sold>0 {flt}",
                 [s, e] + fp)["n"].iloc[0])
    aip = safe(q(f"SELECT COUNT(DISTINCT product_id) n "
                 f"FROM v_daily_revenue_by_category "
                 f"WHERE date_snapshot BETWEEN ? AND ? AND quantity_sold>0 {flt}",
                 [ps, pe] + fp)["n"].iloc[0])
    aipct_c = aic / ic * 100 if ic else 0
    aipct_p = aip / ip * 100 if ip else 0

    # ── Turnover current ───────────────────────────────────────────
    # Stock at the latest available snapshot in range (category-scoped)
    df_stk = q(
        f"SELECT SUM(stock) stk FROM v_daily_revenue_by_category "
        f"WHERE date_snapshot = ("
        f"  SELECT MAX(date_snapshot) FROM v_daily_revenue_by_category "
        f"  WHERE date_snapshot BETWEEN ? AND ? {flt}) {flt}",
        [s, e] + fp + fp,
    )
    df_sol = q(
        f"SELECT SUM(quantity_sold) sol FROM v_daily_revenue_by_category "
        f"WHERE date_snapshot BETWEEN ? AND ? {flt}",
        [s, e] + fp,
    )
    stk = safe(df_stk["stk"].iloc[0]) if not df_stk.empty else 0.0
    sol = safe(df_sol["sol"].iloc[0]) if not df_sol.empty else 0.0
    tov = None if sol <= 0 else stk / sol * nd

    # ── Turnover previous ──────────────────────────────────────────
    df_pstk = q(
        f"SELECT SUM(stock) stk FROM v_daily_revenue_by_category "
        f"WHERE date_snapshot = ("
        f"  SELECT MAX(date_snapshot) FROM v_daily_revenue_by_category "
        f"  WHERE date_snapshot BETWEEN ? AND ? {flt}) {flt}",
        [ps, pe] + fp + fp,
    )
    df_psol = q(
        f"SELECT SUM(quantity_sold) sol FROM v_daily_revenue_by_category "
        f"WHERE date_snapshot BETWEEN ? AND ? {flt}",
        [ps, pe] + fp,
    )
    p_stk = safe(df_pstk["stk"].iloc[0]) if not df_pstk.empty else 0.0
    p_sol = safe(df_psol["sol"].iloc[0]) if not df_psol.empty else 0.0
    tov_p = None if p_sol <= 0 else p_stk / p_sol * nd

    return dict(
        rev=(cr, pr),
        sel=(sc, sp),
        apct=(apct_c, apct_p),
        itm=(ic, ip),
        aipct=(aipct_c, aipct_p),
        tov=(tov, tov_p, stk),
        nd=nd,
    )


@st.cache_data(ttl=300)
def load_donut(s, e, path: tuple):
    """Revenue aggregated at the current drill level, top-9 + Other."""
    if len(path) == 0:
        df = q(
            "SELECT level2_name cat, SUM(revenue) rev "
            "FROM v_daily_revenue_by_category "
            "WHERE date_snapshot BETWEEN ? AND ? "
            "  AND level2_name IS NOT NULL AND trim(level2_name)!='' "
            "GROUP BY level2_name ORDER BY rev DESC",
            [s, e],
        )
    elif len(path) == 1:
        df = q(
            "SELECT level3_name cat, SUM(revenue) rev "
            "FROM v_daily_revenue_by_category "
            "WHERE date_snapshot BETWEEN ? AND ? "
            "  AND level2_name=? "
            "  AND level3_name IS NOT NULL AND trim(level3_name)!='' "
            "GROUP BY level3_name ORDER BY rev DESC",
            [s, e, path[0]],
        )
    else:
        df = q(
            "SELECT category_name cat, SUM(revenue) rev "
            "FROM v_daily_revenue_by_category "
            "WHERE date_snapshot BETWEEN ? AND ? "
            "  AND level3_name=? AND category_level=4 "
            "GROUP BY category_name ORDER BY rev DESC",
            [s, e, path[-1]],
        )
    if df.empty:
        return df
    top9 = df.head(9).copy()
    if len(df) > 9:
        top9 = pd.concat(
            [top9, pd.DataFrame({"cat": ["Other"], "rev": [df.iloc[9:]["rev"].sum()]})],
            ignore_index=True,
        )
    return top9


@st.cache_data(ttl=300)
def load_shops(s, e, ps, pe, path: tuple):
    """Top-100 sellers by revenue, optionally filtered to a category."""
    flt, fp = "", []
    if len(path) == 1:
        flt, fp = "AND level2_name=?", [path[0]]
    elif len(path) >= 2:
        flt, fp = "AND level3_name=?", [path[-1]]

    df_c = q(
        f"SELECT seller_id, SUM(revenue) rev "
        f"FROM v_daily_revenue_by_category "
        f"WHERE date_snapshot BETWEEN ? AND ? {flt} "
        f"GROUP BY seller_id ORDER BY rev DESC LIMIT 100",
        [s, e] + fp,
    )
    if df_c.empty:
        return pd.DataFrame()

    # Seller display names (latest snapshot)
    df_n = q(
        "SELECT seller_id, seller_name FROM ("
        "  SELECT seller_id, seller_name, "
        "         ROW_NUMBER() OVER (PARTITION BY seller_id ORDER BY date_snapshot DESC) rn "
        "  FROM sellers_optimized"
        ") t WHERE rn=1"
    )

    # Market-share denominator
    df_tot = q(
        f"SELECT SUM(revenue) tot FROM v_daily_revenue_by_category "
        f"WHERE date_snapshot BETWEEN ? AND ? {flt}",
        [s, e] + fp,
    )
    total = safe(df_tot["tot"].iloc[0], 1.0)

    # Previous-period revenue for same sellers
    sids = df_c["seller_id"].tolist()
    ph = ",".join(["?"] * len(sids))
    df_p = (
        q(
            f"SELECT seller_id, SUM(revenue) prev_rev "
            f"FROM v_daily_revenue_by_category "
            f"WHERE date_snapshot BETWEEN ? AND ? AND seller_id IN ({ph}) {flt} "
            f"GROUP BY seller_id",
            [ps, pe] + sids + fp,
        )
        if sids
        else pd.DataFrame({"seller_id": [], "prev_rev": []})
    )

    df = df_c.merge(df_n, on="seller_id", how="left")
    df = df.merge(df_p, on="seller_id", how="left")
    df["prev_rev"] = df["prev_rev"].fillna(0)
    df["seller_name"] = df.apply(
        lambda r: r["seller_name"]
        if pd.notna(r.get("seller_name"))
        else str(int(r["seller_id"])),
        axis=1,
    )
    df["rank"] = range(1, len(df) + 1)
    df["chg"] = df.apply(
        lambda r: (r.rev - r.prev_rev) / r.prev_rev * 100 if r.prev_rev > 0 else None,
        axis=1,
    )
    df["mkt"] = df["rev"] / total * 100
    return df


@st.cache_data(ttl=300)
def load_bubble(s, e, path: tuple):
    """Sellers vs items bubble chart, scoped to the current drill level."""
    if len(path) == 0:
        # Root: one bubble per top-level (level-2) category
        return q(
            "SELECT level2_name cat, "
            "       COUNT(DISTINCT seller_id) sellers, "
            "       COUNT(DISTINCT product_id) products, "
            "       SUM(revenue) revenue "
            "FROM v_daily_revenue_by_category "
            "WHERE date_snapshot BETWEEN ? AND ? "
            "  AND level2_name IS NOT NULL AND trim(level2_name)!='' "
            "GROUP BY level2_name ORDER BY revenue DESC",
            [s, e],
        )
    elif len(path) == 1:
        # Drilled into level-2: one bubble per level-3 subcategory
        return q(
            "SELECT level3_name cat, "
            "       COUNT(DISTINCT seller_id) sellers, "
            "       COUNT(DISTINCT product_id) products, "
            "       SUM(revenue) revenue "
            "FROM v_daily_revenue_by_category "
            "WHERE date_snapshot BETWEEN ? AND ? "
            "  AND level2_name=? "
            "  AND level3_name IS NOT NULL AND trim(level3_name)!='' "
            "GROUP BY level3_name ORDER BY revenue DESC",
            [s, e, path[0]],
        )
    else:
        # Drilled into level-3: one bubble per level-4 leaf category
        return q(
            "SELECT category_name cat, "
            "       COUNT(DISTINCT seller_id) sellers, "
            "       COUNT(DISTINCT product_id) products, "
            "       SUM(revenue) revenue "
            "FROM v_daily_revenue_by_category "
            "WHERE date_snapshot BETWEEN ? AND ? "
            "  AND level3_name=? AND category_level=4 "
            "GROUP BY category_name ORDER BY revenue DESC",
            [s, e, path[-1]],
        )


@st.cache_data(ttl=300)
def load_trend(s, e, ps, pe, path: tuple):
    """Daily or weekly revenue bars, optionally scoped to a category."""
    nd = (e - s).days + 1
    grp = (
        "date_snapshot::DATE"
        if nd <= 60
        else "DATE_TRUNC('week', date_snapshot)::DATE"
    )
    # Category filter — mirrors the donut drill level
    if len(path) == 0:
        flt, fp = "", []
    elif len(path) == 1:
        flt, fp = "AND level2_name=?", [path[0]]
    else:
        flt, fp = "AND level3_name=?", [path[-1]]
    dc = q(
        f"SELECT {grp} period, SUM(revenue) rev "
        f"FROM v_daily_revenue_by_category "
        f"WHERE date_snapshot BETWEEN ? AND ? {flt} "
        f"GROUP BY {grp} ORDER BY period",
        [s, e] + fp,
    )
    dp = q(
        f"SELECT {grp} period, SUM(revenue) rev "
        f"FROM v_daily_revenue_by_category "
        f"WHERE date_snapshot BETWEEN ? AND ? {flt} "
        f"GROUP BY {grp} ORDER BY period",
        [ps, pe] + fp,
    )
    return dc, dp, nd


# ══════════════════════════════════════════════════════════════════════
# GLOBAL CSS
# ══════════════════════════════════════════════════════════════════════
st.markdown(
    """
<style>
/* ── Hide default Streamlit chrome ── */
#MainMenu, footer { display: none !important; }
header[data-testid="stHeader"] { display: none !important; }
.block-container {
    padding-top: 0 !important;
    padding-left: 1rem !important;
    padding-right: 1rem !important;
    max-width: 100% !important;
}

/* ── Sidebar: icon-only strip ── */
section[data-testid="stSidebar"] {
    min-width: 58px !important;
    max-width: 58px !important;
}
section[data-testid="stSidebar"] > div:first-child {
    padding-top: 0 !important;
    background: white;
    border-right: 1px solid #e5e7eb;
}

/* ── Custom header ── */
.ks-hdr {
    display: flex; align-items: center; justify-content: space-between;
    background: white; border-bottom: 1px solid #e5e7eb;
    padding: 10px 20px; position: sticky; top: 0; z-index: 999;
    margin-bottom: 12px;
}
.ks-logo { font-size: 16px; font-weight: 700; color: #1d4ed8; letter-spacing: -.3px; }
.ks-links a {
    color: #374151; text-decoration: underline;
    margin-left: 20px; font-size: 13px;
}

/* ── KPI card ── */
.kpi {
    background: white; border-radius: 10px; border: 1px solid #e5e7eb;
    padding: 13px 10px 10px; text-align: center; height: 88px;
    box-shadow: 0 1px 3px rgba(0,0,0,.05);
}
.kpi .lbl {
    color: #9ca3af; font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: .05em;
    margin-bottom: 5px; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
}
.kpi .val {
    color: #111827; font-size: 21px; font-weight: 700;
    line-height: 1.15; margin-bottom: 4px;
}
.kpi .tnd { font-size: 11px; min-height: 14px; }

/* ── Widget title ── */
.wtitle { font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 6px; }

/* ── Shop table ── */
.tbl-wrap { overflow-y: auto; max-height: 378px; }
.stbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.stbl th {
    padding: 6px 8px; color: #1e40af; font-weight: 600; font-size: 11px;
    background: #f8fafc; border-bottom: 1px solid #e5e7eb;
    position: sticky; top: 0; z-index: 2; white-space: nowrap;
}
.stbl td { padding: 4px 8px; vertical-align: middle; }
.rbar-wrap {
    position: relative; height: 20px;
    display: flex; align-items: center; min-width: 85px;
}
.rbar {
    position: absolute; left: 0; top: 1px; bottom: 1px;
    background: rgba(59,130,246,.18); border-radius: 2px;
}
.rval { position: relative; font-size: 12px; color: #111827; padding-left: 2px; }

/* ── Grand total row ── */
.gtrow td {
    font-weight: 700; color: #111827;
    border-top: 2px solid #e5e7eb; padding: 5px 8px;
}

/* ── Breadcrumb ── */
.bc-trail { font-size: 11px; color: #6b7280; margin: 2px 0 8px; }
.bc-trail .bc-cur { color: #111827; font-weight: 600; }

/* ── Date input label ── */
div[data-testid="stDateInput"] label { display: none; }
div[data-testid="stDateInput"] div[data-baseweb="input"] {
    background: white; border: 1px solid #d1d5db; border-radius: 8px;
    box-shadow: 0 1px 2px rgba(0,0,0,.05);
}

/* ── Sidebar icons ── */
.sb-nav { display: flex; flex-direction: column; padding: 8px 0; gap: 2px; }
.nav-ico {
    display: flex; align-items: center; justify-content: center;
    width: 42px; height: 42px; border-radius: 8px;
    margin: 0 auto; cursor: pointer;
}
.nav-ico.active {
    border-left: 3px solid #2563eb;
    background: #eff6ff;
    border-radius: 0 8px 8px 0;
    margin-left: -1px;
}

/* ── Collapse chevron ── */
.sb-collapse {
    position: absolute; bottom: 14px; left: 0; right: 0;
    text-align: center; color: #9ca3af;
    cursor: pointer; font-size: 20px; line-height: 1;
}
</style>
""",
    unsafe_allow_html=True,
)


# ══════════════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════════════
st.markdown(
    """
<div class="ks-hdr">
  <div class="ks-logo">🛍️ Konga Selling</div>
  <div class="ks-links">
    <a href="https://www.konga.com" target="_blank">Service Website</a>
    <a href="https://t.me/kongasupport" target="_blank">Telegram Support</a>
  </div>
</div>
""",
    unsafe_allow_html=True,
)


# ══════════════════════════════════════════════════════════════════════
# SIDEBAR  — icon-only navigation strip
# ══════════════════════════════════════════════════════════════════════
NAV = [
    ("grid_view",      "Dashboard",  True),
    ("show_chart",     "Analytics",  False),
    ("payments",       "Finance",    False),
    ("task_alt",       "Tasks",      False),
    ("search",         "Search",     False),
    ("description",    "Documents",  False),
    ("table_chart",    "Tables",     False),
    ("storefront",     "Storefront", False),
    ("account_circle", "Profile",    False),
]

with st.sidebar:
    icons_html = (
        '<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">'
        '<div class="sb-nav">'
    )
    for icon, lbl, active in NAV:
        clr = "#2563eb" if active else "#6b7280"
        cls = "nav-ico active" if active else "nav-ico"
        icons_html += (
            f'<div class="{cls}" title="{lbl}">'
            f'<span class="material-icons" style="font-size:21px;color:{clr}">{icon}</span>'
            f"</div>"
        )
    icons_html += '</div><div class="sb-collapse">›</div>'
    st.markdown(icons_html, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════
# DATE RANGE PICKER  — default: last 30 days from MAX(date_snapshot)
# ══════════════════════════════════════════════════════════════════════
mn_d, mx_d = date_bounds()
def_e = mx_d
def_s = max(mn_d, mx_d - timedelta(days=29))

dp_col, _ = st.columns([3, 9])
with dp_col:
    picked = st.date_input(
        "Date range", value=(def_s, def_e),
        min_value=mn_d, max_value=mx_d, key="dr",
    )

if isinstance(picked, (list, tuple)) and len(picked) == 2:
    S, E = picked[0], picked[1]
else:
    S, E = def_s, def_e

PS, PE = prev_window(S, E)
ND = (E - S).days + 1


# ══════════════════════════════════════════════════════════════════════
# KPI CARDS
# ══════════════════════════════════════════════════════════════════════
kpis = load_kpis(S, E, PS, PE, tuple(st.session_state.donut_path))
cr, pr   = kpis["rev"]
sc, sp   = kpis["sel"]
ac, ap   = kpis["apct"]
ic, ip   = kpis["itm"]
aic, aip = kpis["aipct"]
tv, pvt, stk = kpis["tov"]

# Turnover display logic
if tv is None:
    tv_disp = "&gt; 999 days"
    tv_trnd = ""
elif stk == 0:
    tv_disp = '<span style="color:#ef4444">⚠&nbsp;0 days</span>'
    tv_trnd = ""
else:
    tv_disp = f"{int(tv)} days"
    tv_trnd = trnd(tv, pvt) if pvt is not None else ""

KPI_DEFS = [
    ("Revenue",        fcur(cr),         trnd(cr, pr, f"vs prev {ND}d")),
    ("Total Shops",    fnum(sc),          trnd(sc, sp)),
    ("…Active Shops",  f"{ac:.1f}%",      trnd(ac, ap)),
    ("Total Items",    fnum(ic),          trnd(ic, ip)),
    ("…Active Items",  f"{aic:.1f}%",     trnd(aic, aip)),
    ("SKU",            fnum(ic),          trnd(ic, ip)),   # per spec: same as Total Items in range
    ("Turnover",       tv_disp,           tv_trnd),
]

k_cols = st.columns(7, gap="small")
for col, (lbl, val, tnd) in zip(k_cols, KPI_DEFS):
    with col:
        st.markdown(
            f'<div class="kpi">'
            f'<div class="lbl">{lbl}</div>'
            f'<div class="val">{val}</div>'
            f'<div class="tnd">{tnd}</div>'
            f"</div>",
            unsafe_allow_html=True,
        )

st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════
# CATEGORY COLORS  (load / initialise once)
# ══════════════════════════════════════════════════════════════════════
CC = cat_colors()

# ══════════════════════════════════════════════════════════════════════
# ROW 1 — Widget 1 (Donut) │ Widget 2 (Top Shops)
# ══════════════════════════════════════════════════════════════════════
R1, R2 = st.columns(2, gap="small")

# ─── Widget 1: Donut Chart with drill-down ────────────────────────
with R1:
    with st.container(border=True):
        path = tuple(st.session_state.donut_path)

        # Title row
        st.markdown('<div class="wtitle">Top Categories</div>', unsafe_allow_html=True)

        # Breadcrumb trail
        bc_parts = ["All"] + list(path)
        bc_html = ""
        for i, part in enumerate(bc_parts):
            if i > 0:
                bc_html += ' <span style="color:#d1d5db">/</span> '
            if i == len(bc_parts) - 1:
                bc_html += f'<span class="bc-cur">{part}</span>'
            else:
                bc_html += f'<span>{part}</span>'
        st.markdown(f'<div class="bc-trail">{bc_html}</div>', unsafe_allow_html=True)

        # Breadcrumb navigation buttons (only when drilled in)
        if path:
            bc_btn_cols = st.columns(min(len(bc_parts), 4))
            if bc_btn_cols[0].button("↩ All", key="bc_root"):
                st.session_state.donut_path = []
                st.rerun()
            for i, part in enumerate(path[:-1]):          # skip current (last)
                btn_lbl = (part[:10] + "…") if len(part) > 10 else part
                if bc_btn_cols[i + 1].button(f"↑ {btn_lbl}", key=f"bc_{i}", help=part):
                    st.session_state.donut_path = list(path)[: i + 1]
                    st.rerun()

        # Load donut data
        df_d = load_donut(S, E, path)

        if not df_d.empty:
            # Colour assignment
            if len(path) == 0:
                # Root level: use global category colour map
                d_data = [
                    {"name": r["cat"], "value": round(float(r.rev), 2),
                     "itemStyle": {"color": CC.get(r["cat"], OTHER_CLR)}}
                    for _, r in df_d.iterrows()
                ]
            else:
                # Drill-down: assign distinct palette colours by position
                d_data = [
                    {"name": r["cat"], "value": round(float(r.rev), 2),
                     "itemStyle": {"color": CAT_PALETTE[i % len(CAT_PALETTE)]}}
                    for i, (_, r) in enumerate(df_d.iterrows())
                ]

            chart_key = f"donut_{'|'.join(path) or 'root'}_{S}_{E}"
            ev = st_echarts(
                options={
                    "tooltip": {"trigger": "item", "formatter": "{b}: {d}%"},
                    "legend": {
                        "orient": "vertical",
                        "right": "2%",
                        "top": "center",
                        "icon": "circle",
                        "itemWidth": 8, "itemHeight": 8,
                        "type": "scroll",
                        "textStyle": {"fontSize": 10},
                    },
                    "series": [{
                        "name": "Revenue",
                        "type": "pie",
                        "radius": ["38%", "68%"],
                        "center": ["38%", "50%"],
                        "avoidLabelOverlap": True,
                        "label": {
                            "show": True, "position": "inside",
                            "formatter": "{d}%", "color": "#fff",
                            "fontWeight": "bold", "fontSize": 10, "minAngle": 12,
                        },
                        "emphasis": {"itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,.3)"}},
                        "data": d_data,
                    }],
                },
                height="300px",
                events={"click": "function(p){return {name:p.name,value:p.value};}"},
                key=chart_key,
            )

            # Handle drill-down — st_echarts wraps the JS return value
            # under the "chart_event" key, not at the top level
            chart_event = ev.get("chart_event") if isinstance(ev, dict) else None
            if chart_event and isinstance(chart_event, dict):
                clicked = chart_event.get("name")
                cur = list(st.session_state.donut_path)
                # Allow drill-down max 2 levels deep (level 2 → 3 → 4)
                if clicked and clicked != "Other" and len(cur) < 2 and clicked not in cur:
                    st.session_state.donut_path = cur + [clicked]
                    st.rerun()
        else:
            st.info("No category data for this date range.")


# ─── Widget 2: Top-100 Shops ─────────────────────────────────────
with R2:
    with st.container(border=True):
        st.markdown('<div class="wtitle">🏆 Top-100 Shops</div>', unsafe_allow_html=True)

        df_sh = load_shops(S, E, PS, PE, tuple(st.session_state.donut_path))

        if not df_sh.empty:
            mx_rev = df_sh["rev"].max() or 1.0
            rows_html = []

            for _, r in df_sh.iterrows():
                bw = r.rev / mx_rev * 100
                chg = r.chg
                # Change % badge
                if chg is None or (isinstance(chg, float) and math.isnan(chg)):
                    chg_h = '<span style="color:#9ca3af">—</span>'
                elif chg >= 0:
                    chg_h = f'<span style="color:#22c55e">▲&nbsp;{abs(chg):.1f}%</span>'
                else:
                    chg_h = f'<span style="color:#ef4444">▼&nbsp;{abs(chg):.1f}%</span>'

                # Market share heatmap background
                ms_alpha = min(r.mkt / 25.0, 1.0) * 0.45
                ms_bg = f"rgba(34,197,94,{ms_alpha:.3f})"
                row_bg = "white" if r["rank"] % 2 == 1 else "#f9fafb"
                sname = str(r.seller_name)[:28]

                rows_html.append(
                    f'<tr style="background:{row_bg}">'
                    f'<td style="color:#9ca3af;text-align:right;padding-right:6px">{r["rank"]}</td>'
                    f'<td><a href="https://www.konga.com/merchant/{int(r.seller_id)}" '
                    f'   target="_blank" '
                    f'   style="color:#2563eb;text-decoration:underline;white-space:nowrap">'
                    f'{sname}</a></td>'
                    f"<td>"
                    f'<div class="rbar-wrap">'
                    f'<div class="rbar" style="width:{bw:.1f}%"></div>'
                    f'<span class="rval">{fcur(r.rev)}</span>'
                    f"</div></td>"
                    f'<td style="text-align:center">{chg_h}</td>'
                    f'<td style="background:{ms_bg};text-align:center">{r.mkt:.1f}%</td>'
                    f"</tr>"
                )

            # Grand total row
            gt_rev  = df_sh["rev"].sum()
            gt_mkt  = df_sh["mkt"].sum()
            rows_html.append(
                f'<tr class="gtrow">'
                f'<td colspan="2">Grand Total</td>'
                f'<td>{fcur(gt_rev)}</td>'
                f'<td></td>'
                f'<td style="text-align:center">{gt_mkt:.1f}%</td>'
                f"</tr>"
            )

            st.markdown(
                f'<div class="tbl-wrap"><table class="stbl">'
                f"<thead><tr>"
                f'<th style="width:28px;text-align:right">#</th>'
                f"<th>Shop</th>"
                f"<th>Revenue&nbsp;▾</th>"
                f"<th>Change&nbsp;%</th>"
                f"<th>Market&nbsp;Share</th>"
                f"</tr></thead>"
                f'<tbody>{"".join(rows_html)}</tbody>'
                f"</table></div>"
                f'<div style="text-align:right;color:#9ca3af;font-size:10px;margin-top:4px">'
                f"1&nbsp;–&nbsp;{len(df_sh)}&nbsp;/&nbsp;{len(df_sh)}&emsp;◀&nbsp;▶"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.info("No shop data for this filter and period.")


# ══════════════════════════════════════════════════════════════════════
# ROW 2 — Widget 3 (Bubble) │ Widget 4 (Revenue Trend)
# ══════════════════════════════════════════════════════════════════════
R3, R4 = st.columns(2, gap="small")

# ─── Widget 3: Bubble Chart ──────────────────────────────────────
with R3:
    with st.container(border=True):
        # Dynamic title reflects current drill level
        _path_w3 = tuple(st.session_state.donut_path)
        _scope_w3 = (' – ' + ' / '.join(_path_w3)) if _path_w3 else ''
        st.markdown(
            f'<div class="wtitle">'
            f'Assortment <span style="color:#2563eb">▶</span> Competition{_scope_w3}'
            "</div>",
            unsafe_allow_html=True,
        )

        df_b = load_bubble(S, E, _path_w3)

        if not df_b.empty:
            mx_r = df_b["revenue"].max() or 1
            fig3 = go.Figure()

            for i, (_, r) in enumerate(df_b.iterrows()):
                cat_name = r["cat"]
                # Root level: use session-locked palette; drill levels: positional palette
                c = CC.get(cat_name, OTHER_CLR) if not _path_w3 else CAT_PALETTE[i % len(CAT_PALETTE)]
                sz  = max(8.0, (r.revenue / mx_r) ** 0.45 * 62)
                fig3.add_trace(go.Scatter(
                    x=[r.sellers], y=[r.products],
                    mode="markers",
                    name=cat_name,
                    marker=dict(
                        size=sz, color=c, opacity=0.78,
                        line=dict(width=1, color="white"),
                    ),
                    hovertemplate=(
                        f"<b>{cat_name}</b><br>"
                        "Shops: %{x:,}<br>"
                        "Items: %{y:,}<br>"
                        f"Revenue: {fcur(r.revenue)}"
                        "<extra></extra>"
                    ),
                ))

            fig3.update_layout(
                xaxis=dict(
                    title="Shops – Competition",
                    showgrid=True, gridcolor="#f0f4f8",
                    tickformat=",d",
                ),
                yaxis=dict(
                    title="Items – Assortment",
                    showgrid=True, gridcolor="#f0f4f8",
                    tickformat=",d",
                ),
                legend=dict(
                    orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0,
                    font=dict(size=9), itemsizing="constant",
                ),
                plot_bgcolor="white", paper_bgcolor="white",
                margin=dict(l=50, r=10, t=50, b=50),
                height=330,
            )
            st.plotly_chart(fig3, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("No data for this period.")


# ─── Widget 4: Revenue Trend (grouped bar) ───────────────────────
with R4:
    with st.container(border=True):
        _path_w4 = tuple(st.session_state.donut_path)
        df_ct, df_pt, nd_ = load_trend(S, E, PS, PE, _path_w4)
        prev_lbl = f"Revenue (previous {nd_} days)"

        _scope_w4 = (' – ' + ' / '.join(_path_w4)) if _path_w4 else ''
        st.markdown(
            f'<div class="wtitle">Revenue Trend{_scope_w4}</div>',
            unsafe_allow_html=True,
        )

        if not df_ct.empty:
            cx  = [str(d) for d in df_ct["period"].tolist()]
            cy  = df_ct["rev"].tolist()
            py  = df_pt["rev"].tolist() if not df_pt.empty else []

            # Align previous series to current x-axis positions
            n       = len(cx)
            py_pad  = (py + [None] * n)[:n]

            fig4 = go.Figure()
            fig4.add_trace(go.Bar(
                name="Current Revenue",
                x=cx, y=cy,
                marker_color="#60a5fa",
                offsetgroup="A",
            ))
            fig4.add_trace(go.Bar(
                name=prev_lbl,
                x=cx, y=py_pad,
                marker_color="#94a3b8",
                offsetgroup="B",
            ))
            fig4.update_layout(
                barmode="group",
                xaxis=dict(tickangle=45, showgrid=False, nticks=12),
                yaxis=dict(
                    showgrid=True, gridcolor="#f0f4f8",
                    tickformat=".2s",       # e.g. 100M, 1B
                ),
                legend=dict(
                    orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0, font=dict(size=9),
                ),
                plot_bgcolor="white", paper_bgcolor="white",
                margin=dict(l=55, r=10, t=50, b=70),
                height=330,
            )
            st.plotly_chart(fig4, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("No trend data for this period.")


# ══════════════════════════════════════════════════════════════════════
# FOOTER
# ══════════════════════════════════════════════════════════════════════
st.markdown(
    f'<div style="color:#d1d5db;font-size:10px;margin-top:8px;padding:0 4px">'
    f"Last update: {mx_d}"
    f"</div>",
    unsafe_allow_html=True,
)
