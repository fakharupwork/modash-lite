
import io
import json
import os
import re
import unicodedata
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="AMANA Modash Lite",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(APP_DIR, "amana_config.json"), "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

KEYWORDS = pd.DataFrame(CONFIG["keywords"])
COUNTRIES = pd.DataFrame(CONFIG["countries"])
DELIVERY_HEADERS = CONFIG["delivery_headers"]

st.markdown("""
<style>
    .block-container {padding-top: 1.1rem; padding-bottom: 2rem;}
    [data-testid="stSidebar"] {background: #111827;}
    [data-testid="stSidebar"] * {color: #f9fafb;}
    [data-testid="stSidebar"] label {font-weight: 600;}
    .am-card {
        background:#ffffff; border:1px solid #e5e7eb; border-radius:14px;
        padding:16px 18px; box-shadow:0 1px 2px rgba(0,0,0,.04); margin-bottom:12px;
    }
    .am-muted {color:#6b7280; font-size:.9rem;}
    .am-title {font-size:1.7rem; font-weight:750; margin-bottom:.15rem;}
    .am-sub {color:#6b7280; margin-bottom:1rem;}
    .pill {display:inline-block; padding:3px 9px; border-radius:999px;
           background:#eef2ff; color:#3730a3; font-size:.78rem; margin-right:5px;}
    div[data-testid="metric-container"] {
        background:#ffffff; border:1px solid #e5e7eb; padding:11px 14px; border-radius:12px;
    }
    .stDownloadButton button, .stButton button {border-radius:10px;}
</style>
""", unsafe_allow_html=True)

def normalize_header(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")

ALIASES = {
    "platform": ["platform", "network", "social_platform"],
    "handle": ["account_handle", "handle", "username", "user_name", "account", "profile_handle"],
    "name": ["full_name", "name", "creator_name", "display_name"],
    "followers": ["followers", "follower_count", "followers_count", "audience", "fans"],
    "bio": ["bio", "biography", "description", "profile_bio"],
    "captions": ["captions", "caption", "recent_captions", "posts_text", "content", "post_captions"],
    "email": ["email", "public_email", "business_email", "contact_email", "emails"],
    "country": ["country", "creator_country", "location_country"],
    "language": ["language", "language_code", "lang", "content_language"],
    "website": ["website", "site", "url_website"],
    "profile_url": ["profile_url", "url", "profile", "social_url", "account_url"],
    "last_post_date": ["last_post_date", "latest_post_date", "last_posted", "last_post", "recent_post_date"],
    "email_source_url": ["email_source_url", "email_source", "source_url"],
    "country_evidence_url": ["country_evidence_url", "country_source_url", "location_evidence_url"],
    "notes": ["notes", "note", "comments"],
}

def autodetect_mapping(columns):
    norm_to_orig = {normalize_header(c): c for c in columns}
    result = {}
    for role, aliases in ALIASES.items():
        result[role] = None
        for a in aliases:
            if a in norm_to_orig:
                result[role] = norm_to_orig[a]
                break
    return result

def parse_followers(v):
    if pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(",", "").replace(" ", "")
    if not s:
        return None
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    try:
        return float(s) * mult
    except Exception:
        m = re.search(r"[-+]?\d*\.?\d+", s)
        return float(m.group()) * mult if m else None

def stext(v):
    return "" if pd.isna(v) else str(v)

def contains_keyword(text, keyword, mode="Exact phrase"):
    t = stext(text).casefold()
    k = stext(keyword).casefold().strip()
    if not k:
        return False
    if mode == "Exact phrase":
        return k in t
    words = [w for w in re.split(r"\s+", k) if w]
    if not words:
        return False
    if mode == "All words":
        return all(w in t for w in words)
    return any(w in t for w in words)

def normalize_handle(v):
    s = stext(v).strip().casefold()
    s = s.split("?")[0].rstrip("/")
    s = re.sub(r"^https?://(www\.)?(instagram\.com|tiktok\.com/@|youtube\.com/@)", "", s)
    return s.lstrip("@").strip("/")

def email_validish(v):
    s = stext(v).strip()
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s))

def load_upload(uploaded):
    name = uploaded.name.lower()
    raw = uploaded.getvalue()
    if name.endswith(".csv"):
        # Try common encodings/delimiters.
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return pd.read_csv(io.BytesIO(raw), encoding=enc, sep=None, engine="python")
            except Exception:
                pass
        raise ValueError("Could not read CSV.")
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return pd.read_excel(io.BytesIO(raw))
    raise ValueError("Please upload CSV, XLSX, or XLS.")

def as_csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")

def language_codes_for_country(country_name):
    row = COUNTRIES[COUNTRIES["Country"] == country_name]
    if row.empty:
        return sorted(KEYWORDS["Language code"].dropna().astype(str).unique().tolist())
    codes = [x.strip() for x in str(row.iloc[0]["Search language codes"]).split(",") if x.strip()]
    return codes

def keyword_subset(country, language_codes, fields, themes):
    sub = KEYWORDS.copy()
    if language_codes:
        sub = sub[sub["Language code"].isin(language_codes)]
    if fields:
        sub = sub[sub["Modash field"].isin(fields)]
    if themes:
        sub = sub[sub["Theme"].isin(themes)]
    return sub.reset_index(drop=True)

def base_filter(df, mapping, follower_min, follower_max, platforms, country, selected_langs,
                require_email, last_post_days):
    work = df.copy()
    mask = pd.Series(True, index=work.index)

    if mapping.get("followers"):
        follower_num = work[mapping["followers"]].map(parse_followers)
        mask &= follower_num.between(follower_min, follower_max, inclusive="both").fillna(False)
        work["_followers_num"] = follower_num

    if platforms and mapping.get("platform"):
        allowed = {p.casefold() for p in platforms}
        mask &= work[mapping["platform"]].astype(str).str.casefold().isin(allowed)

    if country and country != "Any" and mapping.get("country"):
        target = country.casefold()
        mask &= work[mapping["country"]].astype(str).str.casefold().str.contains(re.escape(target), na=False)

    if selected_langs and mapping.get("language"):
        allowed_langs = {x.casefold() for x in selected_langs}
        mask &= work[mapping["language"]].astype(str).str.casefold().isin(allowed_langs)

    if require_email and mapping.get("email"):
        mask &= work[mapping["email"]].map(email_validish)

    if last_post_days and mapping.get("last_post_date"):
        dates = pd.to_datetime(work[mapping["last_post_date"]], errors="coerce", utc=True)
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=last_post_days)
        mask &= dates.ge(cutoff).fillna(False)

    return work[mask].copy()

def run_searches(base_df, mapping, search_rows, match_mode, dedupe=True):
    pieces = []
    stats = []
    bio_col = mapping.get("bio")
    cap_col = mapping.get("captions")

    for _, sr in search_rows.iterrows():
        field = sr["Modash field"]
        keyword = sr["Keyword"]
        col = bio_col if field == "Bio" else cap_col

        if not col:
            stats.append({
                "language_code": sr["Language code"], "language": sr["Language"],
                "search_field": field, "keyword": keyword, "theme": sr["Theme"],
                "matches": 0, "status": f"Skipped: no {field} column mapped"
            })
            continue

        m = base_df[col].map(lambda x: contains_keyword(x, keyword, match_mode))
        hit = base_df[m].copy()
        stats.append({
            "language_code": sr["Language code"], "language": sr["Language"],
            "search_field": field, "keyword": keyword, "theme": sr["Theme"],
            "matches": int(len(hit)), "status": "OK"
        })
        if hit.empty:
            continue
        hit["_matched_language_code"] = sr["Language code"]
        hit["_matched_language"] = sr["Language"]
        hit["_matched_search_field"] = field
        hit["_matched_keyword"] = keyword
        hit["_matched_theme"] = sr["Theme"]
        pieces.append(hit)

    if not pieces:
        return pd.DataFrame(columns=list(base_df.columns) + [
            "_matched_language_code","_matched_language","_matched_search_field",
            "_matched_keyword","_matched_theme"
        ]), pd.DataFrame(stats)

    combined = pd.concat(pieces, ignore_index=True)

    if dedupe:
        handle_col = mapping.get("handle")
        platform_col = mapping.get("platform")
        email_col = mapping.get("email")
        profile_col = mapping.get("profile_url")
        agg_cols = ["_matched_keyword", "_matched_theme", "_matched_search_field", "_matched_language_code"]

        def join_unique(s):
            vals = []
            seen = set()
            for x in s:
                x = stext(x)
                if x and x not in seen:
                    seen.add(x)
                    vals.append(x)
            return " | ".join(vals)

        def collapse_groups(frame, key_cols):
            grouped = frame.groupby(key_cols, dropna=False, sort=False)
            first = grouped.first().reset_index()
            for c in agg_cols:
                vals = grouped[c].apply(join_unique).reset_index(name=c)
                first = first.drop(columns=[c], errors="ignore").merge(vals, on=key_cols, how="left")
            return first

        # Profile dedupe: platform+handle first, then profile URL/email fallback for missing handles.
        if handle_col:
            combined["_dedupe_handle"] = combined[handle_col].map(normalize_handle)
        else:
            combined["_dedupe_handle"] = ""

        combined["_dedupe_platform"] = (
            combined[platform_col].astype(str).str.casefold().str.strip()
            if platform_col else ""
        )
        combined["_dedupe_profile_url"] = (
            combined[profile_col].astype(str).str.casefold().str.strip().str.rstrip("/")
            if profile_col else ""
        )
        combined["_dedupe_email"] = (
            combined[email_col].astype(str).str.casefold().str.strip()
            if email_col else ""
        )

        combined["_profile_key"] = [
            (f"h:{plat}:{handle}" if handle
             else f"u:{url}" if url
             else f"e:{email}" if email
             else f"r:{i}")
            for i, (plat, handle, url, email) in enumerate(zip(
                combined["_dedupe_platform"],
                combined["_dedupe_handle"],
                combined["_dedupe_profile_url"],
                combined["_dedupe_email"],
            ))
        ]
        combined = collapse_groups(combined, ["_profile_key"])

        # AMANA also requires duplicate emails to be removed across profiles.
        if email_col:
            has_email = combined["_dedupe_email"].astype(str).str.len().gt(0)
            with_email = combined[has_email].copy()
            without_email = combined[~has_email].copy()
            if not with_email.empty:
                with_email = collapse_groups(with_email, ["_dedupe_email"])
            combined = pd.concat([with_email, without_email], ignore_index=True, sort=False)

        combined = combined.drop_duplicates()

    return combined.reset_index(drop=True), pd.DataFrame(stats)

def delivery_view(df, mapping, country_default=None):
    if df.empty:
        return df.copy()
    out = pd.DataFrame(index=df.index)
    role_to_delivery = {
        "platform":"platform", "handle":"account_handle", "name":"full_name",
        "followers":"followers", "bio":"bio", "email":"email", "country":"country",
        "language":"language", "website":"website", "profile_url":"profile_url",
        "email_source_url":"email_source_url", "last_post_date":"last_post_date",
        "country_evidence_url":"country_evidence_url", "notes":"notes"
    }
    for role, dst in role_to_delivery.items():
        col = mapping.get(role)
        out[dst] = df[col] if col and col in df.columns else ""

    if country_default and "country" in out.columns:
        out["country"] = out["country"].replace("", country_default)

    out["search_field"] = df.get("_matched_search_field", "")
    out["search_expression"] = df.get("_matched_keyword", "")
    out["theme"] = df.get("_matched_theme", "")

    for c in DELIVERY_HEADERS:
        if c not in out.columns:
            out[c] = ""
    return out[DELIVERY_HEADERS]

def search_queue(country, languages, fields, themes, follower_min, follower_max, platforms):
    sub = keyword_subset(country, languages, fields, themes).copy()
    country_row = COUNTRIES[COUNTRIES["Country"] == country]
    iso = "" if country_row.empty else country_row.iloc[0]["ISO"]
    allocation = "" if country_row.empty else country_row.iloc[0]["Next 35k proposed"]
    note = "" if country_row.empty else country_row.iloc[0]["Platform / allocation note"]
    out = pd.DataFrame({
        "country": country,
        "iso": iso,
        "language_code": sub["Language code"],
        "language": sub["Language"],
        "search_field": sub["Modash field"],
        "keyword": sub["Keyword"],
        "theme": sub["Theme"],
        "follower_min": follower_min,
        "follower_max": follower_max,
        "platforms": ", ".join(platforms) if platforms else "Any",
        "proposed_country_allocation": allocation,
        "platform_note": note,
    })
    return out

# Header
st.markdown('<div class="am-title">AMANA Modash Lite</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="am-sub">Keyword + follower-range search queue, creator filtering, deduplication and export. '
    'Built from the uploaded AMANA workbook.</div>',
    unsafe_allow_html=True,
)
st.markdown(
    f'<span class="pill">{len(KEYWORDS):,} keyword searches</span>'
    f'<span class="pill">{KEYWORDS["Language code"].nunique()} language codes</span>'
    f'<span class="pill">{len(COUNTRIES)} countries</span>',
    unsafe_allow_html=True
)

with st.sidebar:
    st.header("Search setup")

    country_options = ["Any"] + COUNTRIES["Country"].astype(str).tolist()
    country = st.selectbox("Creator country", country_options, index=1 if len(country_options) > 1 else 0)

    default_langs = language_codes_for_country(country) if country != "Any" else sorted(KEYWORDS["Language code"].unique())
    available_langs = sorted(KEYWORDS["Language code"].dropna().astype(str).unique().tolist())
    selected_langs = st.multiselect(
        "Language codes",
        options=available_langs,
        default=[x for x in default_langs if x in available_langs]
    )

    fields = st.multiselect("Search fields", ["Bio", "Captions"], default=["Bio", "Captions"])

    all_themes = sorted(KEYWORDS["Theme"].dropna().astype(str).unique().tolist())
    themes = st.multiselect("Themes", all_themes, default=[])

    follower_preset = st.radio("Follower preset", ["Nano 300–10K", "Micro 10K–100K", "Custom"], horizontal=False)
    if follower_preset == "Nano 300–10K":
        follower_min, follower_max = 300, 10_000
    elif follower_preset == "Micro 10K–100K":
        follower_min, follower_max = 10_000, 100_000
    else:
        c1, c2 = st.columns(2)
        follower_min = c1.number_input("Min", min_value=0, value=300, step=100)
        follower_max = c2.number_input("Max", min_value=1, value=10_000, step=100)

    platforms = st.multiselect(
        "Platforms",
        ["Instagram", "TikTok", "YouTube"],
        default=["Instagram", "TikTok", "YouTube"]
    )

    match_mode = st.selectbox("Keyword match", ["Exact phrase", "All words", "Any word"], index=0)
    require_email = st.checkbox("Require valid-looking email", value=True)
    last_post_days = st.number_input("Last post within days (if column exists)", min_value=0, value=30, step=1)
    dedupe = st.checkbox("Deduplicate matched creators", value=True)

tabs = st.tabs(["🔎 Filter creators", "📋 Search queue", "⚙️ Rules / mapping guide"])

with tabs[1]:
    queue_country = country
    queue = search_queue(
        queue_country, selected_langs, fields, themes, follower_min, follower_max, platforms
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Searches", f"{len(queue):,}")
    c2.metric("Bio searches", int((queue["search_field"] == "Bio").sum()) if not queue.empty else 0)
    c3.metric("Caption searches", int((queue["search_field"] == "Captions").sum()) if not queue.empty else 0)

    st.caption("One row = one separate search, matching the workbook instructions.")
    st.dataframe(queue, use_container_width=True, height=520, hide_index=True)
    st.download_button(
        "Download search queue CSV",
        data=as_csv_bytes(queue),
        file_name=f"AMANA_search_queue_{queue_country.replace(' ','_')}_{follower_min}-{follower_max}.csv",
        mime="text/csv"
    )

with tabs[0]:
    uploaded = st.file_uploader("Upload creator export", type=["csv", "xlsx", "xls"])
    st.caption("Supports common Modash/scraper exports. CSV is fastest for large files.")

    if not uploaded:
        st.info(
            "Upload creator data to filter it. The AMANA workbook you provided contains search rules/configuration, "
            "not creator records. You can still use the Search queue tab immediately."
        )
    else:
        try:
            df = load_upload(uploaded)
        except Exception as e:
            st.error(f"Could not read the file: {e}")
            st.stop()

        df.columns = [str(c).strip() for c in df.columns]
        detected = autodetect_mapping(df.columns)

        st.success(f"Loaded {len(df):,} rows × {len(df.columns)} columns.")

        with st.expander("Column mapping", expanded=False):
            st.caption("Auto-detected where possible. Change any incorrect mapping.")
            opts = ["— Not mapped —"] + list(df.columns)
            mapping = {}
            left, right = st.columns(2)
            roles = list(ALIASES.keys())
            for i, role in enumerate(roles):
                host = left if i % 2 == 0 else right
                default_col = detected.get(role)
                default_idx = opts.index(default_col) if default_col in opts else 0
                choice = host.selectbox(role.replace("_"," ").title(), opts, index=default_idx, key=f"map_{role}")
                mapping[role] = None if choice == "— Not mapped —" else choice

        needed = []
        if not mapping.get("followers"):
            needed.append("Followers")
        if "Bio" in fields and not mapping.get("bio"):
            needed.append("Bio")
        if "Captions" in fields and not mapping.get("captions"):
            needed.append("Captions")
        if needed:
            st.warning("Missing mapping for: " + ", ".join(needed) + ". Searches using missing fields will be skipped.")

        search_rows = keyword_subset(country, selected_langs, fields, themes)
        st.write(f"**Active keyword searches:** {len(search_rows):,}")

        # Optional single-keyword mode
        mode = st.radio("Run mode", ["All active keywords", "One keyword"], horizontal=True)
        if mode == "One keyword" and not search_rows.empty:
            labels = (
                search_rows["Language code"].astype(str) + " | " +
                search_rows["Modash field"].astype(str) + " | " +
                search_rows["Keyword"].astype(str) + " | " +
                search_rows["Theme"].astype(str)
            )
            chosen = st.selectbox("Keyword search", labels.tolist())
            idx = labels[labels == chosen].index[0]
            search_rows = search_rows.loc[[idx]]

        if st.button("Run filters", type="primary", use_container_width=True):
            base = base_filter(
                df, mapping, follower_min, follower_max, platforms, country,
                selected_langs, require_email, int(last_post_days) if last_post_days else 0
            )

            results, stats = run_searches(base, mapping, search_rows, match_mode, dedupe=dedupe)

            st.session_state["amana_base_count"] = len(base)
            st.session_state["amana_results"] = results
            st.session_state["amana_stats"] = stats
            st.session_state["amana_mapping"] = mapping
            st.session_state["amana_country"] = country

        if "amana_results" in st.session_state:
            results = st.session_state["amana_results"]
            stats = st.session_state["amana_stats"]
            mapping = st.session_state["amana_mapping"]
            base_count = st.session_state.get("amana_base_count", 0)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Uploaded", f"{len(df):,}")
            c2.metric("After base filters", f"{base_count:,}")
            c3.metric("Unique matches", f"{len(results):,}")
            positive = int((stats["matches"] > 0).sum()) if not stats.empty else 0
            c4.metric("Keywords with matches", f"{positive:,}")

            result_tab, stats_tab, delivery_tab = st.tabs(["Matched creators", "Keyword stats", "Delivery template"])

            with result_tab:
                if results.empty:
                    st.warning("No matches found with the current settings.")
                else:
                    view_cols = [c for c in [
                        mapping.get("platform"), mapping.get("handle"), mapping.get("name"),
                        mapping.get("followers"), mapping.get("bio"), mapping.get("captions"),
                        mapping.get("email"), mapping.get("country"), mapping.get("language"),
                        mapping.get("profile_url"), "_matched_keyword", "_matched_theme",
                        "_matched_search_field", "_matched_language_code"
                    ] if c and c in results.columns]
                    # de-dupe columns while preserving order
                    view_cols = list(dict.fromkeys(view_cols))
                    st.dataframe(results[view_cols], use_container_width=True, height=520, hide_index=True)
                    st.download_button(
                        "Download matched creators CSV",
                        data=as_csv_bytes(results),
                        file_name="AMANA_matched_creators.csv",
                        mime="text/csv"
                    )

            with stats_tab:
                st.dataframe(stats, use_container_width=True, height=520, hide_index=True)
                st.download_button(
                    "Download keyword stats CSV",
                    data=as_csv_bytes(stats),
                    file_name="AMANA_keyword_stats.csv",
                    mime="text/csv"
                )

            with delivery_tab:
                dv = delivery_view(results, mapping, None if country == "Any" else country)
                st.dataframe(dv, use_container_width=True, height=520, hide_index=True)
                st.download_button(
                    "Download AMANA delivery CSV",
                    data=as_csv_bytes(dv),
                    file_name="AMANA_delivery_ready.csv",
                    mime="text/csv"
                )

with tabs[2]:
    st.subheader("What this tool does")
    st.markdown("""
- Uses the exact AMANA country, language, keyword, search-field and theme configuration from your uploaded workbook.
- Applies follower range, platform, creator country, language, public email and recent-post filters when those columns exist.
- Runs each keyword separately against the correct **Bio** or **Captions** column.
- Shows per-keyword match counts and skipped searches.
- Deduplicates creators and retains the matched keyword/theme evidence.
- Exports the final rows in the AMANA delivery-template column order.
""")

    st.subheader("Important limitation")
    st.info(
        "This is a Modash-style filtering/search workflow over data you already have or export. "
        "It does not contain Modash's private influencer database and does not automatically crawl Instagram, "
        "TikTok or YouTube by itself."
    )

    st.subheader("Recommended creator-data columns")
    mapping_table = pd.DataFrame({
        "Role": list(ALIASES.keys()),
        "Common accepted names": [", ".join(v) for v in ALIASES.values()]
    })
    st.dataframe(mapping_table, use_container_width=True, hide_index=True)

    st.subheader("AMANA workbook rules loaded")
    st.write(f"Countries: **{len(COUNTRIES)}**")
    st.write(f"Keyword searches: **{len(KEYWORDS)}**")
    st.write(f"Language codes: **{KEYWORDS['Language code'].nunique()}**")
    st.write(f"Themes: **{KEYWORDS['Theme'].nunique()}**")
