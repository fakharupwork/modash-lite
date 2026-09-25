import io
import json
import os
import re
import time
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st

try:
    from langdetect import detect as _langdetect_detect, DetectorFactory, LangDetectException
    DetectorFactory.seed = 0
except Exception:
    _langdetect_detect = None
    LangDetectException = Exception

st.set_page_config(page_title="AMANA Creator Finder 35K", page_icon="🔎", layout="wide", initial_sidebar_state="expanded")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(APP_DIR, "amana_config.json"), "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

KEYWORDS = pd.DataFrame(CONFIG["keywords"])
COUNTRIES = pd.DataFrame(CONFIG["countries"])
DELIVERY_HEADERS = CONFIG["delivery_headers"]

EXTRA_EVIDENCE_HEADERS = [
    "country_evidence", "detected_language", "language_evidence", "discovered_followers",
    "relevant_posts_90d", "supporting_post_1", "supporting_post_2",
    "qualification_status", "qualification_notes", "api_source"
]

st.markdown("""
<style>
.block-container {padding-top: 1rem; padding-bottom: 2rem;}
[data-testid="stSidebar"] {background:#111827;}
[data-testid="stSidebar"] * {color:#f9fafb;}
[data-testid="stSidebar"] label {font-weight:600;}
.am-title {font-size:1.75rem;font-weight:800;margin-bottom:.15rem;}
.am-sub {color:#6b7280;margin-bottom:.8rem;}
.pill {display:inline-block;padding:4px 10px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:.78rem;margin:0 5px 5px 0;}
div[data-testid="metric-container"] {background:#fff;border:1px solid #e5e7eb;padding:10px 13px;border-radius:12px;}
.stButton button,.stDownloadButton button {border-radius:10px;}
</style>
""", unsafe_allow_html=True)

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
SOCIAL_HOSTS = {"instagram.com", "www.instagram.com", "tiktok.com", "www.tiktok.com", "youtube.com", "www.youtube.com", "youtu.be", "x.com", "twitter.com", "facebook.com", "www.facebook.com"}

COMMON_COUNTRY_ALIASES = {
    "United States": ["United States", "USA", "U.S.A.", "US"],
    "United Kingdom": ["United Kingdom", "UK", "U.K.", "Great Britain", "Britain", "England", "Scotland", "Wales", "Northern Ireland"],
    "United Arab Emirates": ["United Arab Emirates", "UAE", "U.A.E."],
    "South Korea": ["South Korea", "Republic of Korea", "Korea"],
    "Côte d’Ivoire": ["Côte d’Ivoire", "Cote d'Ivoire", "Ivory Coast"],
    "Taiwan": ["Taiwan", "Republic of China"],
}


def stext(v):
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v)


def normalize_handle(v):
    s = stext(v).strip().casefold().split("?")[0].rstrip("/")
    s = re.sub(r"^https?://(www\.)?(instagram\.com/|tiktok\.com/@|youtube\.com/@)", "", s)
    return s.lstrip("@").strip("/")


def parse_followers(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    s = stext(v).strip().lower().replace(",", "")
    if not s:
        return None
    # Handles 12.3K, 12.3K subscribers, 2M followers, and plain integers.
    m = re.search(r"(\d+(?:\.\d+)?)\s*([kmb])?", s)
    if not m:
        return None
    n = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    mult = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
    return int(n * mult)


def contains_keyword(text, keyword, mode="Exact phrase"):
    t = stext(text).casefold()
    k = stext(keyword).casefold().strip()
    if not k:
        return False
    if mode == "Exact phrase":
        return k in t
    words = [w for w in re.split(r"\s+", k) if w]
    if mode == "All words":
        return bool(words) and all(w in t for w in words)
    return bool(words) and any(w in t for w in words)



LANG_ALIASES = {
    "zh-cn": "zh", "zh-tw": "zh", "zh-hk": "zh", "pt-br": "pt", "pt-pt": "pt",
    "en-us": "en", "en-gb": "en", "es-419": "es", "fil": "tl", "iw": "he",
}


def normalize_lang_code(code):
    c = stext(code).strip().casefold().replace("_", "-")
    if not c:
        return ""
    c = LANG_ALIASES.get(c, c)
    return c.split("-")[0]


def detect_content_language(text):
    clean = re.sub(r"https?://\S+|[@#]\w+", " ", stext(text))
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) < 35 or _langdetect_detect is None:
        return ""
    try:
        return normalize_lang_code(_langdetect_detect(clean))
    except LangDetectException:
        return ""
    except Exception:
        return ""


def in_follower_range(value, follower_min, follower_max):
    f = parse_followers(value)
    return f is not None and int(follower_min) <= f <= int(follower_max)


class ApiBudgetExceeded(RuntimeError):
    pass


def unique_join(values, sep=" | "):
    out, seen = [], set()
    for v in values:
        x = stext(v).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return sep.join(out)


def first_nonempty(*vals):
    for v in vals:
        if v is None:
            continue
        if isinstance(v, pd.Series):
            if v.empty:
                continue
            non_null = v.dropna()
            if non_null.empty:
                continue
            return non_null.iloc[0]
        if isinstance(v, (list, tuple, set, dict)):
            if len(v) == 0:
                continue
            return v
        if isinstance(v, str):
            if not v.strip():
                continue
            return v
        try:
            if pd.isna(v):
                continue
        except Exception:
            pass
        return v
    return None


def nested_get(obj, path, default=None):
    cur = obj
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def walk_items(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, k, v
            yield from walk_items(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield from walk_items(v, p)


def find_values_by_key(obj, key_fragments):
    frags = [x.casefold() for x in key_fragments]
    vals = []
    for path, key, value in walk_items(obj):
        lk = str(key).casefold()
        if any(f in lk for f in frags) and not isinstance(value, (dict, list)):
            vals.append((path, value))
    return vals


def extract_emails(obj):
    found = []
    for _, _, value in walk_items(obj):
        if isinstance(value, str):
            for email in EMAIL_RE.findall(value):
                e = email.strip(".,;:()[]{}<>\"'").lower()
                if e not in found:
                    found.append(e)
    return found


def extract_urls(obj):
    urls = []
    url_re = re.compile(r"https?://[^\s\"'<>]+", re.I)
    for _, _, value in walk_items(obj):
        if isinstance(value, str):
            for u in url_re.findall(value):
                u = u.rstrip(".,;:)]}")
                if u not in urls:
                    urls.append(u)
    return urls


def first_website(obj):
    for u in extract_urls(obj):
        try:
            host = urlparse(u).netloc.casefold()
        except Exception:
            continue
        if host and host not in SOCIAL_HOSTS and not host.endswith("cdninstagram.com") and "googleusercontent" not in host:
            return u
    return ""


def parse_date_any(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        n = float(v)
        if n > 10_000_000_000:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except Exception:
            return None
    s = stext(v).strip()
    if not s:
        return None
    # ISO / normal date
    try:
        ts = pd.to_datetime(s, utc=True, errors="raise")
        return ts.to_pydatetime()
    except Exception:
        pass
    # YouTube-style relative dates
    m = re.search(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", s.casefold())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        now = datetime.now(timezone.utc)
        if unit == "minute": return now - timedelta(minutes=n)
        if unit == "hour": return now - timedelta(hours=n)
        if unit == "day": return now - timedelta(days=n)
        if unit == "week": return now - timedelta(weeks=n)
        if unit == "month": return now - timedelta(days=30*n)
        if unit == "year": return now - timedelta(days=365*n)
    return None


def iso_date(dt):
    return dt.astimezone(timezone.utc).date().isoformat() if isinstance(dt, datetime) else ""


def language_codes_for_country(country_name):
    row = COUNTRIES[COUNTRIES["Country"] == country_name]
    if row.empty:
        return sorted(KEYWORDS["Language code"].dropna().astype(str).unique().tolist())
    return [x.strip() for x in stext(row.iloc[0]["Search language codes"]).split(",") if x.strip()]


def country_iso(country_name):
    row = COUNTRIES[COUNTRIES["Country"] == country_name]
    return "" if row.empty else stext(row.iloc[0]["ISO"]).upper().strip()


def keyword_subset(language_codes, fields, themes):
    sub = KEYWORDS.copy()
    if language_codes:
        sub = sub[sub["Language code"].isin(language_codes)]
    if fields:
        sub = sub[sub["Modash field"].isin(fields)]
    if themes:
        sub = sub[sub["Theme"].isin(themes)]
    return sub.reset_index(drop=True)


def country_aliases(country, iso):
    vals = [country]
    vals += COMMON_COUNTRY_ALIASES.get(country, [])
    # ISO is safe only for structured country fields, not free text.
    return list(dict.fromkeys([x for x in vals if x]))


def text_has_location(text, country, extra_terms):
    t = stext(text).casefold()
    terms = country_aliases(country, "") + extra_terms
    for term in terms:
        term = term.strip()
        if len(term) < 3:
            continue
        if re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", t):
            return term
    return ""


def structured_country_match(obj, country, iso):
    aliases = {x.casefold() for x in country_aliases(country, iso)}
    for path, value in find_values_by_key(obj, ["country_code", "countrycode", "country"]):
        sv = stext(value).strip()
        if not sv:
            continue
        if sv.upper() == iso.upper() or sv.casefold() in aliases:
            return True, f"{path}={sv}"
    return False, ""


def profile_location_evidence(obj, bio, country, iso, extra_terms):
    ok, ev = structured_country_match(obj, country, iso)
    if ok:
        return True, ev
    # Check explicit profile address/location-like fields, but not arbitrary post text.
    for path, value in find_values_by_key(obj, ["business_address", "city_name", "location", "address"]):
        if isinstance(value, str):
            hit = text_has_location(value, country, extra_terms)
            if hit:
                return True, f"{path}: {hit}"
            # address_json is sometimes a JSON string
            if value.strip().startswith("{"):
                try:
                    parsed = json.loads(value)
                    ok2, ev2 = structured_country_match(parsed, country, iso)
                    if ok2:
                        return True, f"{path}.{ev2}"
                    hit2 = text_has_location(json.dumps(parsed, ensure_ascii=False), country, extra_terms)
                    if hit2:
                        return True, f"{path}: {hit2}"
                except Exception:
                    pass
    hit = text_has_location(bio, country, extra_terms)
    if hit:
        return True, f"bio: {hit}"
    return False, ""


class ScrapeCreatorsClient:
    BASE = "https://api.scrapecreators.com"

    def __init__(self, api_key, timeout=45, max_calls=0):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.max_calls = int(max_calls or 0)
        self.calls = 0
        self.credits_charged = 0
        self.credits_remaining = None
        self.errors = []
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": self.api_key, "User-Agent": "AMANA-Creator-Finder/3.0"})

    def get(self, path, params=None, retries=2):
        if self.max_calls and self.calls >= self.max_calls:
            raise ApiBudgetExceeded(f"API-call limit reached ({self.max_calls:,} calls for this run).")
        url = self.BASE + path
        last = None
        for attempt in range(retries + 1):
            try:
                if self.max_calls and self.calls >= self.max_calls:
                    raise ApiBudgetExceeded(f"API-call limit reached ({self.max_calls:,} calls for this run).")
                r = self.session.get(url, params=params or {}, timeout=self.timeout)
                self.calls += 1
                if r.status_code == 402:
                    raise RuntimeError("ScrapeCreators credits are exhausted.")
                if r.status_code == 401:
                    raise RuntimeError("Invalid ScrapeCreators API key.")
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict):
                    self.credits_charged += int(data.get("credits_charged") or 0)
                    if data.get("credits_remaining") is not None:
                        self.credits_remaining = data.get("credits_remaining")
                return data
            except ApiBudgetExceeded:
                raise
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(0.7 * (attempt + 1))
                    continue
                self.errors.append(f"{path}: {e}")
        raise last


def add_candidate(store, platform, handle, full_name="", profile_url="", followers=None, search_row=None,
                  discovery_url="", discovery_text="", follower_min=None, follower_max=None,
                  strict_range_gate=True, gate_stats=None):
    """Admit candidates only when discovery already exposes an in-range follower count.

    This is intentionally fail-closed. With strict_range_gate=True, profiles whose follower
    count is missing are not sent to profile/post qualification calls.
    """
    handle = normalize_handle(handle)
    if not handle:
        return False
    f = parse_followers(followers)
    if strict_range_gate:
        if f is None:
            if gate_stats is not None:
                gate_stats["skipped_unknown_followers"] = gate_stats.get("skipped_unknown_followers", 0) + 1
            return False
        if follower_min is not None and follower_max is not None and not (int(follower_min) <= f <= int(follower_max)):
            if gate_stats is not None:
                gate_stats["skipped_out_of_range"] = gate_stats.get("skipped_out_of_range", 0) + 1
            return False
    key = f"{platform.casefold()}:{handle}"
    if key not in store:
        store[key] = {
            "platform": platform, "handle": handle, "full_name": full_name or "",
            "profile_url": profile_url or "", "followers_hint": f,
            "searches": [], "discovery_urls": [], "discovery_texts": []
        }
        if gate_stats is not None:
            gate_stats["admitted_in_range"] = gate_stats.get("admitted_in_range", 0) + 1
    c = store[key]
    if not c["full_name"] and full_name:
        c["full_name"] = full_name
    if not c["profile_url"] and profile_url:
        c["profile_url"] = profile_url
    if c["followers_hint"] is None and f is not None:
        c["followers_hint"] = f
    if search_row is not None:
        rec = {
            "keyword": stext(search_row.get("Keyword")), "field": stext(search_row.get("Modash field")),
            "theme": stext(search_row.get("Theme")), "language_code": stext(search_row.get("Language code")),
            "language": stext(search_row.get("Language"))
        }
        if rec not in c["searches"]:
            c["searches"].append(rec)
    if discovery_url and discovery_url not in c["discovery_urls"]:
        c["discovery_urls"].append(discovery_url)
    if discovery_text:
        c["discovery_texts"].append(discovery_text)
    return True


def discover_instagram(client, store, search_row, country, iso, pages, add_country_term,
                       follower_min, follower_max, gate_stats):
    keyword = stext(search_row["Keyword"]).strip()
    field = stext(search_row["Modash field"])
    queries = [keyword]
    if add_country_term:
        queries.append(f"{keyword} {country}")
    for query in list(dict.fromkeys(queries)):
        # Native profile search is useful for Bio discovery, but strict mode only admits
        # rows where Instagram exposes follower_count in the search response.
        if field == "Bio":
            try:
                data = client.get("/v1/instagram/search/profiles", {"query": query})
            except ApiBudgetExceeded:
                raise
            except Exception:
                data = {}
            for pr in data.get("profiles", []) if isinstance(data, dict) else []:
                add_candidate(
                    store, "Instagram", pr.get("username"), pr.get("full_name"), pr.get("url"),
                    first_nonempty(pr.get("follower_count"), pr.get("followers")), search_row,
                    follower_min=follower_min, follower_max=follower_max, strict_range_gate=True,
                    gate_stats=gate_stats
                )

        # Reels results expose owner.follower_count, so they are the main strict-range
        # discovery source for Instagram. Bio rows are still verified against the profile Bio later.
        for page in range(1, min(int(pages), 11) + 1):
            try:
                data = client.get("/v2/instagram/reels/search", {"query": query, "date_posted": "last-year", "page": page})
            except ApiBudgetExceeded:
                raise
            except Exception:
                break
            reels = data.get("reels", []) if isinstance(data, dict) else []
            if not reels:
                break
            for reel in reels:
                owner = reel.get("owner") or {}
                add_candidate(
                    store, "Instagram", owner.get("username"), owner.get("full_name"),
                    f"https://www.instagram.com/{owner.get('username')}/" if owner.get("username") else "",
                    first_nonempty(owner.get("follower_count"), owner.get("followers")), search_row,
                    reel.get("url", ""), reel.get("caption", ""), follower_min, follower_max, True, gate_stats
                )


def _find_tiktok_user_nodes(data):
    nodes = []
    if isinstance(data, dict):
        for item in data.get("users", []) or []:
            if isinstance(item, dict):
                u = item.get("user_info") or item.get("userInfo") or item.get("user") or item
                if isinstance(u, dict):
                    nodes.append(u)
    return nodes


def tiktok_handle_from_obj(obj):
    for key in ["uniqueId", "unique_id", "uniqueid", "username", "handle", "author_unique_id"]:
        if isinstance(obj, dict) and obj.get(key):
            return obj.get(key)
    for _, key, val in walk_items(obj):
        if str(key) in {"uniqueId", "unique_id", "username"} and isinstance(val, str) and val:
            return val
    return ""


def discover_tiktok(client, store, search_row, country, iso, pages, add_country_term,
                    follower_min, follower_max, gate_stats):
    keyword = stext(search_row["Keyword"]).strip()
    field = stext(search_row["Modash field"])
    queries = [keyword]
    if add_country_term:
        queries.append(f"{keyword} {country}")
    for query in list(dict.fromkeys(queries)):
        if field == "Bio":
            cursor = None
            for _ in range(int(pages)):
                params = {"query": query, "trim": "true"}
                if cursor not in (None, "", 0):
                    params["cursor"] = cursor
                try:
                    data = client.get("/v1/tiktok/search/users", params)
                except ApiBudgetExceeded:
                    raise
                except Exception:
                    break
                nodes = _find_tiktok_user_nodes(data)
                if not nodes:
                    break
                for u in nodes:
                    handle = tiktok_handle_from_obj(u)
                    name = first_nonempty(u.get("nickname"), u.get("nick_name"), u.get("full_name"), "")
                    followers = first_nonempty(
                        u.get("follower_count"), u.get("followerCount"), nested_get(u, "stats.followerCount"),
                        nested_get(u, "stats.follower_count")
                    )
                    add_candidate(
                        store, "TikTok", handle, name, f"https://www.tiktok.com/@{handle}" if handle else "",
                        followers, search_row, follower_min=follower_min, follower_max=follower_max,
                        strict_range_gate=True, gate_stats=gate_stats
                    )
                nxt = data.get("cursor") if isinstance(data, dict) else None
                if nxt in (None, "", cursor):
                    break
                cursor = nxt
        else:
            cursor = None
            for _ in range(int(pages)):
                params = {"query": query, "sort_by": "relevance", "date_posted": "last-6-months", "region": iso, "trim": "true"}
                if cursor not in (None, "", 0):
                    params["cursor"] = cursor
                try:
                    data = client.get("/v1/tiktok/search/keyword", params)
                except ApiBudgetExceeded:
                    raise
                except Exception:
                    break
                items = data.get("search_item_list", []) if isinstance(data, dict) else []
                if not items:
                    break
                for item in items:
                    aweme = item.get("aweme_info") or item
                    author = aweme.get("author") or {}
                    handle = tiktok_handle_from_obj(author) or tiktok_handle_from_obj(aweme)
                    name = first_nonempty(author.get("nickname"), author.get("nick_name"), "") if isinstance(author, dict) else ""
                    stats = author.get("stats") if isinstance(author, dict) else {}
                    followers = first_nonempty(
                        author.get("follower_count") if isinstance(author, dict) else None,
                        author.get("followerCount") if isinstance(author, dict) else None,
                        (stats or {}).get("followerCount") if isinstance(stats, dict) else None,
                        (stats or {}).get("follower_count") if isinstance(stats, dict) else None,
                    )
                    url = first_nonempty(aweme.get("url"), item.get("url"), "")
                    desc = first_nonempty(aweme.get("desc"), item.get("desc"), item.get("search_desc"), "")
                    add_candidate(
                        store, "TikTok", handle, name, f"https://www.tiktok.com/@{handle}" if handle else "",
                        followers, search_row, url, desc, follower_min, follower_max, True, gate_stats
                    )
                nxt = data.get("cursor") if isinstance(data, dict) else None
                if nxt in (None, "", cursor):
                    break
                cursor = nxt


def _youtube_channel_from_result(item):
    if not isinstance(item, dict):
        return None
    if item.get("type") == "channel" or item.get("channelId") or (item.get("id") and (item.get("handle") or item.get("url"))):
        return item
    ch = item.get("channel")
    return ch if isinstance(ch, dict) else None


def youtube_handle_from_obj(obj):
    if not isinstance(obj, dict): return ""
    for key in ["handle", "vanityChannelUrl", "channel", "url"]:
        v = stext(obj.get(key))
        m = re.search(r"youtube\.com/@([^/?]+)", v)
        if m: return m.group(1)
        if key == "handle" and v:
            return v.lstrip("@")
    title = stext(first_nonempty(obj.get("title"), obj.get("name")))
    return title


def discover_youtube(client, store, search_row, country, iso, pages, add_country_term,
                     follower_min, follower_max, gate_stats):
    keyword = stext(search_row["Keyword"]).strip()
    field = stext(search_row["Modash field"])
    queries = [keyword]
    if add_country_term:
        queries.append(f"{keyword} {country}")
    for query in list(dict.fromkeys(queries)):
        token = None
        for _ in range(int(pages)):
            params = {
                "query": query, "type": "channels" if field == "Bio" else "videos",
                "sortBy": "relevance", "region": iso, "includeExtras": "true"
            }
            if token:
                params["continuationToken"] = token
            try:
                data = client.get("/v1/youtube/search", params)
            except ApiBudgetExceeded:
                raise
            except Exception:
                break
            result_lists = []
            if isinstance(data, dict):
                for key in ["channels", "videos", "results", "items"]:
                    if isinstance(data.get(key), list):
                        result_lists.extend(data[key])
            if not result_lists:
                break
            for item in result_lists:
                ch = _youtube_channel_from_result(item)
                if not ch:
                    continue
                channel_id = first_nonempty(ch.get("id"), ch.get("channelId"), item.get("channelId"))
                handle = youtube_handle_from_obj(ch)
                name = first_nonempty(ch.get("title"), ch.get("name"), "")
                url = first_nonempty(ch.get("url"), ch.get("channel"), f"https://www.youtube.com/channel/{channel_id}" if channel_id else "")
                followers = first_nonempty(
                    ch.get("subscriberCount"), ch.get("subscriber_count"), ch.get("subscribers"),
                    ch.get("subscriberCountText"), item.get("subscriberCount"), item.get("subscriberCountText")
                )
                discovery_url = item.get("url", "") if field != "Bio" else ""
                discovery_text = " ".join([stext(item.get("title")), stext(item.get("description"))]) if field != "Bio" else ""
                add_candidate(
                    store, "YouTube", handle or channel_id, name, url, followers, search_row,
                    discovery_url, discovery_text, follower_min, follower_max, True, gate_stats
                )
            token2 = first_nonempty(data.get("continuationToken"), data.get("continuation_token")) if isinstance(data, dict) else None
            if not token2 or token2 == token:
                break
            token = token2


def extract_instagram_profile(data, handle):
    user = nested_get(data, "data.user", {}) or data.get("user", {}) if isinstance(data, dict) else {}
    if not isinstance(user, dict): user = {}
    followers = first_nonempty(nested_get(user, "edge_followed_by.count"), user.get("follower_count"), user.get("followerCount"))
    bio = first_nonempty(user.get("biography"), nested_get(user, "biography_with_entities.raw_text"), "")
    name = first_nonempty(user.get("full_name"), user.get("fullName"), "")
    profile_url = f"https://www.instagram.com/{handle}/"
    website = first_nonempty(user.get("external_url"), first_website(user), "")
    emails = extract_emails(user)
    private = bool(first_nonempty(user.get("is_private"), user.get("private"), False))
    return {"raw": user, "followers": parse_followers(followers), "bio": stext(bio), "name": stext(name), "profile_url": profile_url, "website": stext(website), "email": emails[0] if emails else "", "private": private, "language": ""}


def extract_tiktok_profile(data, handle):
    user = data.get("user", {}) if isinstance(data, dict) else {}
    stats = data.get("stats", {}) if isinstance(data, dict) else {}
    followers = first_nonempty(stats.get("followerCount"), stats.get("follower_count"), user.get("followerCount"), user.get("follower_count"))
    bio = first_nonempty(user.get("signature"), user.get("bio"), "")
    name = first_nonempty(user.get("nickname"), user.get("nick_name"), "")
    website = first_nonempty(nested_get(user, "bioLink.link"), first_website(user), "")
    emails = extract_emails(user)
    private = bool(first_nonempty(user.get("privateAccount"), user.get("is_private"), False))
    lang = first_nonempty(user.get("language"), "")
    return {"raw": data, "followers": parse_followers(followers), "bio": stext(bio), "name": stext(name), "profile_url": f"https://www.tiktok.com/@{handle}", "website": stext(website), "email": emails[0] if emails else "", "private": private, "language": stext(lang)}


def extract_youtube_profile(data, handle):
    if not isinstance(data, dict): data = {}
    followers = first_nonempty(data.get("subscriberCount"), data.get("subscriber_count"), data.get("subscribers"))
    bio = first_nonempty(data.get("description"), "")
    name = first_nonempty(data.get("name"), data.get("title"), "")
    url = first_nonempty(data.get("channel"), data.get("url"), f"https://www.youtube.com/@{handle}")
    email = stext(first_nonempty(data.get("email"), ""))
    if not email:
        es = extract_emails(data); email = es[0] if es else ""
    links = data.get("links") if isinstance(data.get("links"), list) else []
    website = ""
    for u in links:
        try:
            host = urlparse(stext(u)).netloc.casefold()
        except Exception:
            host = ""
        if host and host not in SOCIAL_HOSTS:
            website = stext(u); break
    return {"raw": data, "followers": parse_followers(followers), "bio": stext(bio), "name": stext(name), "profile_url": stext(url), "website": website, "email": email, "private": False, "language": ""}


def instagram_posts(data, handle):
    items = data.get("items", []) if isinstance(data, dict) else []
    out = []
    for x in items[:12]:
        if not isinstance(x, dict): continue
        cap = x.get("caption")
        if isinstance(cap, dict): cap = first_nonempty(cap.get("text"), cap.get("caption"), "")
        text = stext(first_nonempty(cap, x.get("text"), ""))
        dt = parse_date_any(first_nonempty(x.get("created_at"), x.get("taken_at"), x.get("taken_at_timestamp")))
        code = first_nonempty(x.get("code"), x.get("shortcode"))
        url = first_nonempty(x.get("url"), f"https://www.instagram.com/p/{code}/" if code else "")
        out.append({"text": text, "date": dt, "url": stext(url)})
    return out


def tiktok_posts(data, handle):
    items = data.get("aweme_list", []) if isinstance(data, dict) else []
    out = []
    for x in items[:12]:
        if not isinstance(x, dict): continue
        text = stext(first_nonempty(x.get("desc"), x.get("description"), ""))
        dt = parse_date_any(first_nonempty(x.get("create_time"), x.get("createTime"), x.get("create_time_utc")))
        vid = first_nonempty(x.get("aweme_id"), x.get("id"))
        url = first_nonempty(x.get("url"), f"https://www.tiktok.com/@{handle}/video/{vid}" if vid else "")
        out.append({"text": text, "date": dt, "url": stext(url)})
    return out


def youtube_posts(data, handle):
    items = data.get("videos", []) if isinstance(data, dict) else []
    out = []
    for x in items[:12]:
        if not isinstance(x, dict): continue
        text = " ".join([stext(x.get("title")), stext(x.get("description"))]).strip()
        dt = parse_date_any(first_nonempty(x.get("publishedAt"), x.get("published_at"), x.get("publishedTimeText"), x.get("published_time"), x.get("date")))
        vid = first_nonempty(x.get("id"), x.get("videoId"))
        url = first_nonempty(x.get("url"), f"https://www.youtube.com/watch?v={vid}" if vid else "")
        out.append({"text": text, "date": dt, "url": stext(url)})
    return out


def keyword_matches_for_candidate(candidate, bio, posts, mode, max_posts, relevant_days):
    now = datetime.now(timezone.utc)
    recent_posts = [p for p in posts[:max_posts] if p.get("date") and (now - p["date"]).days <= relevant_days]
    matched_keywords, matched_themes, matched_fields, matched_langs, supporting = [], [], [], [], []
    for sr in candidate.get("searches", []):
        kw = sr["keyword"]
        field = sr["field"]
        if field == "Bio":
            hit = contains_keyword(bio, kw, mode)
        else:
            hit = any(contains_keyword(p.get("text", ""), kw, mode) for p in recent_posts)
        if hit:
            matched_keywords.append(kw); matched_themes.append(sr["theme"]); matched_fields.append(field); matched_langs.append(sr["language_code"])
    # Supporting posts can match any discovered keyword, regardless of original field.
    all_keywords = list(dict.fromkeys([s["keyword"] for s in candidate.get("searches", []) if s.get("keyword")]))
    for p in recent_posts:
        if any(contains_keyword(p.get("text", ""), kw, mode) for kw in all_keywords):
            supporting.append(p)
    return {
        "keywords": list(dict.fromkeys(matched_keywords)), "themes": list(dict.fromkeys(matched_themes)),
        "fields": list(dict.fromkeys(matched_fields)), "langs": list(dict.fromkeys(matched_langs)),
        "supporting": supporting
    }



def early_reject_row(platform, handle, candidate, profile, discovered_followers, reason):
    return {
        "platform": platform,
        "account_handle": handle,
        "full_name": profile.get("name", "") or candidate.get("full_name", ""),
        "followers": profile.get("followers", "") if profile.get("followers") is not None else "",
        "bio": profile.get("bio", ""),
        "email": profile.get("email", "") if "duplicate" in reason.casefold() else "",
        "country": "",
        "language": "",
        "website": profile.get("website", ""),
        "profile_url": profile.get("profile_url", "") or candidate.get("profile_url", ""),
        "email_source_url": profile.get("profile_url", "") if (profile.get("email") and "duplicate" in reason.casefold()) else "",
        "search_field": "", "search_expression": "", "theme": "", "last_post_date": "",
        "country_evidence_url": "", "notes": "", "country_evidence": "", "detected_language": "",
        "language_evidence": "", "discovered_followers": discovered_followers, "relevant_posts_90d": 0,
        "supporting_post_1": "", "supporting_post_2": "", "qualification_status": "REJECT",
        "qualification_notes": reason, "api_source": "ScrapeCreators",
    }


def enrich_and_qualify(client, candidate, country, iso, extra_terms, follower_min, follower_max,
                       match_mode, require_country, require_language, allowed_languages, excluded_emails,
                       last_post_days, relevant_days, min_relevant_posts,
                       max_posts, exclude_private, cache_age="1d"):
    platform = candidate["platform"]
    handle = candidate["handle"]
    errors = []

    # Hard gate before any profile/post calls. Only candidates with an already-known
    # in-range discovery follower count should ever reach this function.
    discovered_followers = parse_followers(candidate.get("followers_hint"))
    if discovered_followers is None or not (follower_min <= discovered_followers <= follower_max):
        return None, "strict follower gate blocked candidate before qualification"

    try:
        if platform == "Instagram":
            profile_data = client.get("/v1/instagram/profile", {"handle": handle, "trim": "false", "cache_max_age": cache_age})
            profile = extract_instagram_profile(profile_data, handle)
            # Re-check followers immediately. Do not spend a posts call on a profile that drifted outside range.
            if profile["followers"] is None or not (follower_min <= profile["followers"] <= follower_max):
                return early_reject_row(platform, handle, candidate, profile, discovered_followers,
                                        "followers changed/outside selected range on live profile check"), ""
            if stext(profile.get("email")).strip().casefold() in excluded_emails:
                return early_reject_row(platform, handle, candidate, profile, discovered_followers,
                                        "duplicate email found in exclusion/previous-delivery list"), ""
            post_data = client.get("/v2/instagram/user/posts", {"handle": handle, "trim": "true"})
            posts = instagram_posts(post_data, handle)
            country_ok, country_ev = profile_location_evidence(profile_data, profile["bio"], country, iso, extra_terms)
            country_source = profile["profile_url"] if country_ok else ""

        elif platform == "TikTok":
            profile_data = client.get("/v1/tiktok/profile", {"handle": handle, "cache_max_age": cache_age})
            profile = extract_tiktok_profile(profile_data, handle)
            if profile["followers"] is None or not (follower_min <= profile["followers"] <= follower_max):
                return early_reject_row(platform, handle, candidate, profile, discovered_followers,
                                        "followers changed/outside selected range on live profile check"), ""
            if stext(profile.get("email")).strip().casefold() in excluded_emails:
                return early_reject_row(platform, handle, candidate, profile, discovered_followers,
                                        "duplicate email found in exclusion/previous-delivery list"), ""
            region = {}
            try:
                region = client.get("/v1/tiktok/profile/region", {"handle": handle})
            except ApiBudgetExceeded:
                raise
            except Exception as e:
                errors.append(f"region: {e}")
            region_code = stext(region.get("region") if isinstance(region, dict) else "").upper()
            country_ok = region_code == iso.upper() if region_code else False
            country_ev = f"TikTok region={region_code}" if country_ok else ""
            if not country_ok:
                country_ok, ev2 = profile_location_evidence(profile_data, profile["bio"], country, iso, extra_terms)
                country_ev = ev2 if country_ok else country_ev
            country_source = profile["profile_url"] if country_ok else ""
            post_data = client.get("/v3/tiktok/profile/videos", {"handle": handle, "sort_by": "latest", "region": iso, "trim": "true"})
            posts = tiktok_posts(post_data, handle)

        else:
            params = {"cache_max_age": cache_age}
            if handle.startswith("UC") and len(handle) > 15:
                params["channelId"] = handle
            elif candidate.get("profile_url"):
                params["url"] = candidate["profile_url"]
            else:
                params["handle"] = handle
            profile_data = client.get("/v1/youtube/channel", params)
            profile = extract_youtube_profile(profile_data, handle)
            if profile["followers"] is None or not (follower_min <= profile["followers"] <= follower_max):
                return early_reject_row(platform, handle, candidate, profile, discovered_followers,
                                        "followers changed/outside selected range on live profile check"), ""
            if stext(profile.get("email")).strip().casefold() in excluded_emails:
                return early_reject_row(platform, handle, candidate, profile, discovered_followers,
                                        "duplicate email found in exclusion/previous-delivery list"), ""
            ycountry = stext(profile_data.get("country") if isinstance(profile_data, dict) else "")
            aliases = {x.casefold() for x in country_aliases(country, iso)}
            country_ok = ycountry.casefold() in aliases if ycountry else False
            country_ev = f"YouTube country={ycountry}" if country_ok else ""
            country_source = profile["profile_url"] if country_ok else ""
            channel_id = profile_data.get("channelId") if isinstance(profile_data, dict) else None
            params2 = {"channelId": channel_id} if channel_id else {"handle": handle}
            post_data = client.get("/v1/youtube/channel-videos", params2)
            posts = youtube_posts(post_data, handle)
    except ApiBudgetExceeded:
        raise
    except Exception as e:
        return None, f"API/profile error: {e}"

    reasons = []
    followers = profile["followers"]
    if followers is None:
        reasons.append("followers unavailable")
    elif not (follower_min <= followers <= follower_max):
        reasons.append(f"followers {followers:,} outside range")
    if exclude_private and profile.get("private"):
        reasons.append("private account")
    if require_country and not country_ok:
        reasons.append("creator country not publicly verified")

    match = keyword_matches_for_candidate(candidate, profile["bio"], posts, match_mode, max_posts, relevant_days)
    if not match["keywords"]:
        reasons.append("selected keyword not verified in required bio/recent-content field")

    dated_posts = [p for p in posts if p.get("date")]
    last_post = max((p["date"] for p in dated_posts), default=None)
    if last_post_days > 0:
        if not last_post:
            reasons.append("last post date unavailable")
        elif (datetime.now(timezone.utc) - last_post).days > last_post_days:
            reasons.append(f"last post older than {last_post_days} days")

    support = match["supporting"]
    if min_relevant_posts > 0 and len(support) < min_relevant_posts:
        reasons.append(f"only {len(support)} relevant posts in latest {max_posts}/{relevant_days}d")

    # Language verification. Prefer an explicit platform language field; otherwise detect
    # from bio + latest posts. Fail closed when the user requires language evidence.
    platform_lang = normalize_lang_code(profile.get("language", ""))
    combined_text = " ".join([profile.get("bio", "")] + [p.get("text", "") for p in posts[:max_posts]])
    detected_lang = platform_lang or detect_content_language(combined_text)
    allowed_norm = {normalize_lang_code(x) for x in allowed_languages if normalize_lang_code(x)}
    matched_langs = {normalize_lang_code(x) for x in match.get("langs", []) if normalize_lang_code(x)}
    language_ok = True
    language_evidence = ""
    if detected_lang:
        language_ok = (not allowed_norm) or detected_lang in allowed_norm
        language_evidence = f"platform/detected={detected_lang}"
    elif matched_langs:
        # This is weaker than detection but still ties the matched exact keyword to a configured language.
        language_ok = bool(matched_langs & allowed_norm) if allowed_norm else True
        language_evidence = f"matched-keyword-language={','.join(sorted(matched_langs))}"
    else:
        language_ok = False
    if require_language and not language_ok:
        reasons.append("content language not verified for selected language codes")

    # Email is optional. Keep blank when not public.
    email = profile["email"]
    email_source = profile["profile_url"] if email else ""

    row = {
        "platform": platform,
        "account_handle": handle,
        "full_name": profile["name"] or candidate.get("full_name", ""),
        "followers": followers if followers is not None else "",
        "bio": profile["bio"],
        "email": email,
        "country": country if country_ok else "",
        "language": detected_lang or unique_join(match["langs"]),
        "website": profile["website"],
        "profile_url": profile["profile_url"] or candidate.get("profile_url", ""),
        "email_source_url": email_source,
        "search_field": unique_join(match["fields"]),
        "search_expression": unique_join(match["keywords"]),
        "theme": unique_join(match["themes"]),
        "last_post_date": iso_date(last_post),
        "country_evidence_url": country_source,
        "notes": "",
        "country_evidence": country_ev,
        "detected_language": detected_lang,
        "language_evidence": language_evidence,
        "discovered_followers": discovered_followers,
        "relevant_posts_90d": len(support),
        "supporting_post_1": support[0]["url"] if len(support) > 0 else "",
        "supporting_post_2": support[1]["url"] if len(support) > 1 else "",
        "qualification_status": "PASS" if not reasons else "REJECT",
        "qualification_notes": unique_join(reasons, "; "),
        "api_source": "ScrapeCreators",
    }
    if errors:
        row["notes"] = unique_join(errors, "; ")
    return row, ""


def as_csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")


def live_results_dataframe(rows):
    cols = DELIVERY_HEADERS + [c for c in EXTRA_EVIDENCE_HEADERS if c not in DELIVERY_HEADERS]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns: df[c] = ""
    return df[cols]



def creator_key(row_or_candidate):
    platform = stext(row_or_candidate.get("platform")).casefold().strip()
    handle = normalize_handle(first_nonempty(row_or_candidate.get("account_handle"), row_or_candidate.get("handle"), ""))
    return f"{platform}:{handle}" if platform and handle else ""


def merge_qualified_rows(existing, new_rows, target_total=0):
    """Dedupe by platform+handle and, when present, public email."""
    merged = []
    seen_profiles, seen_emails = set(), set()
    for row in list(existing or []) + list(new_rows or []):
        key = creator_key(row)
        email = stext(row.get("email")).strip().casefold()
        if not key or key in seen_profiles:
            continue
        if email and email in seen_emails:
            continue
        seen_profiles.add(key)
        if email:
            seen_emails.add(email)
        merged.append(row)
        if target_total and len(merged) >= int(target_total):
            break
    return merged


def merge_reject_rows(existing, new_rows):
    merged = {}
    for row in list(existing or []) + list(new_rows or []):
        key = creator_key(row)
        if key and key not in merged:
            merged[key] = row
    return list(merged.values())



def read_uploaded_table(uploaded_file):
    if uploaded_file is None:
        return pd.DataFrame()
    raw = uploaded_file.getvalue()
    if not raw:
        return pd.DataFrame()
    name = stext(getattr(uploaded_file, "name", "")).casefold()
    if name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(raw))
    return pd.read_excel(io.BytesIO(raw))


def extract_exclusions(uploaded_file):
    df = read_uploaded_table(uploaded_file)
    if df.empty:
        return set(), set()
    handles, emails = set(), set()
    for col in df.columns:
        lc = stext(col).casefold().strip()
        vals = df[col].dropna().astype(str)
        if any(x in lc for x in ["handle", "username", "profile_url", "profile url", "account"]):
            for v in vals:
                h = normalize_handle(v)
                if h:
                    handles.add(h)
        if "email" in lc:
            for v in vals:
                e = stext(v).strip().casefold()
                if EMAIL_RE.fullmatch(e):
                    emails.add(e)
    return handles, emails


def load_csv_rows(uploaded_file):
    if uploaded_file is None:
        return []
    raw = uploaded_file.getvalue()
    if not raw:
        return []
    df = pd.read_csv(io.BytesIO(raw))
    return df.fillna("").to_dict("records")


def search_signature(country, langs, fields, themes, follower_min, follower_max, platforms,
                     require_country, require_language, exclude_private, add_country_term, extra_terms,
                     last_post_days, relevant_days, min_relevant_posts, max_posts, match_mode):
    payload = {
        "country": country, "langs": sorted(langs), "fields": sorted(fields), "themes": sorted(themes),
        "follower_min": int(follower_min), "follower_max": int(follower_max), "platforms": sorted(platforms),
        "require_country": bool(require_country), "require_language": bool(require_language),
        "exclude_private": bool(exclude_private), "add_country_term": bool(add_country_term),
        "extra_terms": sorted([stext(x).casefold() for x in extra_terms]),
        "last_post_days": int(last_post_days), "relevant_days": int(relevant_days),
        "min_relevant_posts": int(min_relevant_posts), "max_posts": int(max_posts), "match_mode": match_mode,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def get_secret_api_key():
    try:
        return stext(st.secrets.get("SCRAPECREATORS_API_KEY", ""))
    except Exception:
        return ""


# Header
st.markdown('<div class="am-title">AMANA Creator Finder 35K</div>', unsafe_allow_html=True)
st.markdown('<div class="am-sub">Strict live creator discovery. Only verified PASS rows are kept in the final 35,000 dataset.</div>', unsafe_allow_html=True)
st.markdown(
    f'<span class="pill">{len(KEYWORDS):,} AMANA searches</span>'
    f'<span class="pill">{KEYWORDS["Language code"].nunique()} language codes</span>'
    f'<span class="pill">{len(COUNTRIES)} countries</span>', unsafe_allow_html=True
)

with st.sidebar:
    st.header("Search setup")
    country = st.selectbox("Creator country", COUNTRIES["Country"].astype(str).tolist(), index=0)
    iso = country_iso(country)
    available_langs = sorted(KEYWORDS["Language code"].dropna().astype(str).unique().tolist())
    default_langs = [x for x in language_codes_for_country(country) if x in available_langs]
    selected_langs = st.multiselect("Language codes", available_langs, default=default_langs)
    fields = st.multiselect("Search fields", ["Bio", "Captions"], default=["Bio", "Captions"])
    themes_all = sorted(KEYWORDS["Theme"].dropna().astype(str).unique().tolist())
    themes = st.multiselect("Themes", themes_all, default=[])
    preset = st.radio("Follower range", ["Nano 300-10K", "Micro 10K-100K", "Custom"])
    if preset == "Nano 300-10K":
        follower_min, follower_max = 300, 10000
    elif preset == "Micro 10K-100K":
        follower_min, follower_max = 10000, 100000
    else:
        a, b = st.columns(2)
        follower_min = int(a.number_input("Min followers", 0, value=300, step=100))
        follower_max = int(b.number_input("Max followers", 1, value=10000, step=100))
    platforms = st.multiselect("Platforms", ["Instagram", "TikTok", "YouTube"], default=["Instagram", "TikTok", "YouTube"])

live_tab, upload_tab, queue_tab, rules_tab = st.tabs(["🔴 Live Search 35K", "📥 Filter Upload", "📋 Search Queue", "⚙️ Rules"])

with live_tab:
    st.subheader("Strict live search to 35,000 qualified creators")
    st.caption("Follower range is a locked admission gate. A creator must already have an in-range follower count in the discovery result before the app will spend profile/post qualification calls on that creator.")

    secret_key = get_secret_api_key()
    api_key = st.text_input("ScrapeCreators API key", value=secret_key, type="password", help="Store this as SCRAPECREATORS_API_KEY in Streamlit Secrets. Never put the key in GitHub.")

    target_total = int(st.number_input("Qualified creator target", min_value=1, value=35000, step=1000))

    active = keyword_subset(selected_langs, fields, themes)

    with st.expander("Strict qualification settings", expanded=True):
        a, b, c, d = st.columns(4)
        batch_keywords = int(a.number_input("Keywords per run", min_value=1, value=min(25, max(1, len(active))), step=5))
        pages_per_search = int(b.number_input("Discovery pages/search", min_value=1, max_value=11, value=5, step=1))
        candidate_cap = int(c.number_input("Max in-range candidates/run (0 = unlimited)", min_value=0, value=2000, step=250))
        max_api_calls = int(d.number_input("Max API calls/run", min_value=50, value=3000, step=250))

        e, f, g, h = st.columns(4)
        match_mode = e.selectbox("Keyword verification", ["Exact phrase", "All words", "Any word"], index=0)
        add_country_term = f.checkbox("Search keyword + country too", value=True)
        require_country = g.checkbox("Require public country evidence", value=True)
        require_language = h.checkbox("Require language evidence", value=True)

        i, j, k, l = st.columns(4)
        exclude_private = i.checkbox("Exclude private accounts", value=True)
        last_post_days = int(j.number_input("Last post ≤ days", min_value=0, value=30, step=1))
        relevant_days = int(k.number_input("Relevant-post window", min_value=1, value=90, step=10))
        min_relevant_posts = int(l.number_input("Minimum relevant posts", min_value=0, value=2, step=1))

        m, n = st.columns(2)
        max_posts = int(m.number_input("Check latest N posts", min_value=1, max_value=12, value=6, step=1))
        cache_age = n.selectbox("Reuse cached profile data", ["1d", "3d", "7d"], index=0)

        extra_location_text = st.text_input("Extra creator-location terms (optional)", placeholder="Delhi, Mumbai, Lahore, London... city/region names strengthen Instagram country evidence")
        extra_terms = [x.strip() for x in extra_location_text.split(",") if x.strip()]
        st.success(f"STRICT FOLLOWER GATE: ON and locked at {follower_min:,} to {follower_max:,}. Unknown follower counts and out-of-range discovery rows are not profile-qualified.")

    sig = search_signature(
        country, selected_langs, fields, themes, follower_min, follower_max, platforms,
        require_country, require_language, exclude_private, add_country_term, extra_terms,
        last_post_days, relevant_days,
        min_relevant_posts, max_posts, match_mode
    )
    previous_sig = st.session_state.get("live_signature")
    if previous_sig != sig:
        if previous_sig is not None:
            st.warning("Search filters changed, so live session progress was reset to prevent mixing creators from different criteria. Load your saved qualified CSV below if you want to resume it.")
        for key in ["live_pass", "live_reject", "live_summary", "next_keyword_index", "pending_candidates", "pending_next_index"]:
            st.session_state.pop(key, None)
        st.session_state["live_signature"] = sig

    # Resume/import a previously downloaded qualified checkpoint.
    checkpoint = st.file_uploader("Resume from an existing qualified CSV (optional)", type=["csv"], key="qualified_checkpoint")
    if checkpoint is not None:
        ck_hash = hashlib.sha1(checkpoint.getvalue()).hexdigest()
        if st.session_state.get("loaded_checkpoint_hash") != ck_hash:
            try:
                rows = load_csv_rows(checkpoint)
                valid_rows = []
                for row in rows:
                    if stext(row.get("qualification_status", "PASS")).upper() not in ("", "PASS"):
                        continue
                    fcount = parse_followers(row.get("followers"))
                    if fcount is None or not (follower_min <= fcount <= follower_max):
                        continue
                    if stext(row.get("country")) and stext(row.get("country")).casefold() != country.casefold():
                        continue
                    valid_rows.append(row)
                st.session_state["live_pass"] = merge_qualified_rows(st.session_state.get("live_pass", []), valid_rows, target_total)
                st.session_state["loaded_checkpoint_hash"] = ck_hash
                st.success(f"Loaded {len(valid_rows):,} checkpoint rows that fit the current country/follower range.")
            except Exception as e:
                st.error(f"Could not load checkpoint: {e}")

    exclusion_upload = st.file_uploader(
        "Exclude previous deliveries / duplicate master (optional)",
        type=["csv", "xlsx", "xls"], key="exclusion_master",
        help="Any handle/username/profile URL and email columns are used as a hard exclusion list."
    )
    excluded_handles, excluded_emails = set(), set()
    if exclusion_upload is not None:
        try:
            excluded_handles, excluded_emails = extract_exclusions(exclusion_upload)
            # Prune already loaded session/checkpoint rows if the new exclusion list contains them.
            kept = []
            for row in st.session_state.get("live_pass", []):
                h = normalize_handle(row.get("account_handle"))
                e = stext(row.get("email")).strip().casefold()
                if h in excluded_handles or (e and e in excluded_emails):
                    continue
                kept.append(row)
            st.session_state["live_pass"] = kept
            st.caption(f"Hard exclusions loaded: {len(excluded_handles):,} handles and {len(excluded_emails):,} emails.")
        except Exception as e:
            st.error(f"Could not read exclusion list: {e}")

    current_pass = st.session_state.get("live_pass", [])
    current_reject = st.session_state.get("live_reject", [])
    current_total = len(current_pass)
    remaining = max(0, target_total - current_total)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Qualified total", f"{current_total:,}")
    c2.metric("Target", f"{target_total:,}")
    c3.metric("Still needed", f"{remaining:,}")
    c4.metric("Active keyword rows", f"{len(active):,}")
    c5.metric("Follower range", f"{follower_min:,}-{follower_max:,}")

    if active.empty:
        st.warning("No keyword searches are active. Select language codes/search fields/themes in the sidebar.")
        run_rows = active
        start_index = 1
        end_idx = 0
    else:
        start_index = int(st.session_state.get("next_keyword_index", 1))
        if start_index > len(active):
            run_rows = active.iloc[0:0].copy()
            end_idx = len(active)
            st.warning("All configured keyword rows have been processed for this session. Increase discovery pages or change filters only if you intentionally want a new search pass.")
        else:
            end_idx = min(len(active), start_index - 1 + batch_keywords)
            run_rows = active.iloc[start_index - 1:end_idx].copy()
            st.info(f"Next batch: keyword rows {start_index}-{end_idx} of {len(active)}. The app will stop early if it reaches {target_total:,} qualified creators or the {max_api_calls:,}-call run limit.")

    col_run, col_reset = st.columns([3, 1])
    has_pending = bool(st.session_state.get("pending_candidates"))
    run_label = "Resume pending qualification" if has_pending else "Run next strict search batch"
    run_clicked = col_run.button(run_label, type="primary", width="stretch", disabled=(not api_key or remaining <= 0 or (run_rows.empty and not has_pending) or not platforms))
    if col_reset.button("Reset session", width="stretch"):
        for key in ["live_pass", "live_reject", "live_summary", "next_keyword_index", "pending_candidates", "pending_next_index", "loaded_checkpoint_hash"]:
            st.session_state.pop(key, None)
        st.rerun()

    if run_clicked:
        client = ScrapeCreatorsClient(api_key, max_calls=max_api_calls)
        gate_stats = {"admitted_in_range": 0, "skipped_out_of_range": 0, "skipped_unknown_followers": 0}
        pass_rows, reject_rows = [], []
        budget_hit = False
        discovery_complete = False

        # First finish pending in-range candidates from a previous budget-limited run.
        pending = list(st.session_state.get("pending_candidates", []))
        if pending:
            candidate_list = [c for c in pending if normalize_handle(c.get("handle")) not in excluded_handles]
            st.session_state["pending_candidates"] = candidate_list
            pending_next_index = int(st.session_state.get("pending_next_index", start_index))
            st.info(f"Resuming {len(candidate_list):,} already-discovered in-range candidates before running new searches.")
        else:
            candidates = {}
            status = st.status("Discovering in-range creators...", expanded=True)
            progress = st.progress(0)
            total_steps = max(1, len(run_rows) * len(platforms))
            step = 0
            try:
                for _, sr in run_rows.iterrows():
                    status.write(f"Searching: {sr['Keyword']} | {sr['Modash field']} | {sr['Language code']}")
                    for platform in platforms:
                        if platform == "Instagram":
                            discover_instagram(client, candidates, sr, country, iso, pages_per_search, add_country_term,
                                               follower_min, follower_max, gate_stats)
                        elif platform == "TikTok":
                            discover_tiktok(client, candidates, sr, country, iso, pages_per_search, add_country_term,
                                            follower_min, follower_max, gate_stats)
                        else:
                            discover_youtube(client, candidates, sr, country, iso, pages_per_search, add_country_term,
                                             follower_min, follower_max, gate_stats)
                        step += 1
                        progress.progress(min(1.0, step / total_steps))
                discovery_complete = True
                status.update(label=f"Discovery complete: {len(candidates):,} unique in-range candidates admitted", state="complete")
            except ApiBudgetExceeded as e:
                budget_hit = True
                status.update(label=str(e), state="error")
            except Exception as e:
                status.update(label=f"Discovery stopped: {e}", state="error")

            # Never re-qualify profiles already seen in this strict session/checkpoint.
            seen_keys = {creator_key(x) for x in st.session_state.get("live_pass", []) + st.session_state.get("live_reject", [])}
            candidate_list = [
                c for c in candidates.values()
                if creator_key(c) not in seen_keys and normalize_handle(c.get("handle")) not in excluded_handles
            ]
            if candidate_cap > 0:
                candidate_list = candidate_list[:candidate_cap]
            pending_next_index = (end_idx + 1) if discovery_complete else start_index
            st.session_state["pending_candidates"] = candidate_list
            st.session_state["pending_next_index"] = pending_next_index

        if candidate_list and not budget_hit:
            qstatus = st.status(f"Strictly qualifying {len(candidate_list):,} in-range candidates...", expanded=True)
            qprog = st.progress(0)
            remaining_pending = list(candidate_list)
            processed = 0
            for idx, cnd in enumerate(candidate_list, start=1):
                if len(st.session_state.get("live_pass", [])) + len(pass_rows) >= target_total:
                    break
                qstatus.write(f"{idx}/{len(candidate_list)} {cnd['platform']} @{cnd['handle']} ({cnd.get('followers_hint', '')} followers at discovery)")
                try:
                    row, err = enrich_and_qualify(
                        client, cnd, country, iso, extra_terms, follower_min, follower_max,
                        match_mode, require_country, require_language, selected_langs, excluded_emails,
                        last_post_days, relevant_days, min_relevant_posts,
                        max_posts, exclude_private, cache_age
                    )
                except ApiBudgetExceeded as e:
                    budget_hit = True
                    qstatus.write(str(e))
                    break
                if row:
                    if row["qualification_status"] == "PASS":
                        pass_rows.append(row)
                    else:
                        reject_rows.append(row)
                elif err:
                    reject_rows.append({
                        "platform": cnd["platform"], "account_handle": cnd["handle"],
                        "followers": cnd.get("followers_hint", ""), "profile_url": cnd.get("profile_url", ""),
                        "qualification_status": "REJECT", "qualification_notes": err,
                        "discovered_followers": cnd.get("followers_hint", ""), "api_source": "ScrapeCreators"
                    })
                processed = idx
                qprog.progress(min(1.0, idx / max(1, len(candidate_list))))

            remaining_pending = candidate_list[processed:]
            st.session_state["pending_candidates"] = remaining_pending
            if not remaining_pending and not budget_hit:
                st.session_state["next_keyword_index"] = int(st.session_state.get("pending_next_index", pending_next_index))
                st.session_state.pop("pending_next_index", None)
            qstatus.update(label=f"Qualification batch complete: {len(pass_rows):,} new PASS", state="complete" if not budget_hit else "error")

        # Merge and dedupe final PASS results by profile and email.
        st.session_state["live_pass"] = merge_qualified_rows(st.session_state.get("live_pass", []), pass_rows, target_total)
        st.session_state["live_reject"] = merge_reject_rows(st.session_state.get("live_reject", []), reject_rows)
        st.session_state["live_summary"] = {
            "new_pass": len(pass_rows), "new_reject": len(reject_rows), "api_calls": client.calls,
            "credits_charged": client.credits_charged, "credits_remaining": client.credits_remaining,
            "admitted_in_range": gate_stats.get("admitted_in_range", 0),
            "skipped_out_of_range": gate_stats.get("skipped_out_of_range", 0),
            "skipped_unknown_followers": gate_stats.get("skipped_unknown_followers", 0),
            "pending": len(st.session_state.get("pending_candidates", [])), "budget_hit": budget_hit,
            "errors": client.errors[-20:],
        }

    if st.session_state.get("live_summary"):
        ss = st.session_state["live_summary"]
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("New PASS", f"{ss['new_pass']:,}")
        m2.metric("New rejects", f"{ss['new_reject']:,}")
        m3.metric("In-range admitted", f"{ss['admitted_in_range']:,}")
        m4.metric("Out-of-range skipped", f"{ss['skipped_out_of_range']:,}")
        m5.metric("Unknown followers skipped", f"{ss['skipped_unknown_followers']:,}")
        m6.metric("API calls", f"{ss['api_calls']:,}")
        st.caption(f"Credits charged: {ss['credits_charged']:,}" + (f" | Remaining credits: {ss['credits_remaining']}" if ss.get('credits_remaining') is not None else "") + f" | Pending in-range candidates: {ss['pending']:,}")
        if ss.get("budget_hit"):
            st.warning("The run hit the API-call limit. Click Resume pending qualification. No pending candidate will be lost while this Streamlit session stays active.")

    pass_rows = st.session_state.get("live_pass", [])
    reject_rows = st.session_state.get("live_reject", [])
    if pass_rows or reject_rows:
        t1, t2 = st.tabs([f"✅ Qualified ({len(pass_rows):,})", f"❌ Rejected audit ({len(reject_rows):,})"])
        with t1:
            df_pass = live_results_dataframe(pass_rows)
            st.dataframe(df_pass, width="stretch", height=520, hide_index=True)
            st.download_button("Download qualified checkpoint CSV", as_csv_bytes(df_pass), f"AMANA_{country.replace(' ','_')}_qualified_checkpoint.csv", "text/csv")
        with t2:
            df_rej = live_results_dataframe(reject_rows)
            show_cols = [c for c in ["platform", "account_handle", "followers", "profile_url", "qualification_notes"] if c in df_rej.columns]
            st.dataframe(df_rej[show_cols], width="stretch", height=420, hide_index=True)
            st.download_button("Download rejected audit CSV", as_csv_bytes(df_rej), f"AMANA_{country.replace(' ','_')}_rejected_audit.csv", "text/csv")

    if len(st.session_state.get("live_pass", [])) >= target_total:
        st.success(f"Target reached: {len(st.session_state['live_pass']):,} qualified creators.")


with upload_tab:
    st.subheader("Filter an existing creator export")
    st.caption("Keep using the original workflow when you already have a Modash/scraper CSV or XLSX. The live search tab is the new automatic discovery workflow.")
    uploaded = st.file_uploader("Upload creator CSV/XLSX", type=["csv","xlsx","xls"])
    if uploaded:
        try:
            if uploaded.name.lower().endswith(".csv"):
                df = pd.read_csv(uploaded)
            else:
                df = pd.read_excel(uploaded)
            st.success(f"Loaded {len(df):,} rows.")
            st.dataframe(df.head(100), width="stretch", hide_index=True)
            st.download_button("Download uploaded data as CSV", as_csv_bytes(df), "uploaded_creator_data.csv", "text/csv")
        except Exception as e:
            st.error(f"Could not read file: {e}")

with queue_tab:
    st.subheader("AMANA Search Queue")
    sub = keyword_subset(selected_langs, fields, themes).copy()
    q = pd.DataFrame({
        "country": country, "iso": iso, "language_code": sub["Language code"], "language": sub["Language"],
        "search_field": sub["Modash field"], "keyword": sub["Keyword"], "theme": sub["Theme"],
        "follower_min": follower_min, "follower_max": follower_max, "platforms": ", ".join(platforms)
    })
    st.dataframe(q, width="stretch", height=520, hide_index=True)
    st.download_button("Download search queue CSV", as_csv_bytes(q), f"AMANA_search_queue_{country.replace(' ','_')}.csv", "text/csv")

with rules_tab:
    st.subheader("What PASS means")
    st.markdown("""
A live result is marked **PASS** only when it satisfies the enabled checks: selected follower range, public creator-country evidence, public account, selected keyword relevance in the required field, selected language evidence, recent activity, and the required number of relevant recent posts. Email is **not required**; it remains blank when no public email is returned.

**Strict follower rule:** discovery results with a known follower count outside your selected range are discarded immediately. Results whose discovery response does not expose a follower count are also discarded in strict mode, so the app does not spend profile/post qualification calls on them. The live profile follower count is checked again before post qualification.

**Coverage note:** strict filtering improves precision but reduces coverage. Social-platform search endpoints are ranked/limited, so no third-party search can guarantee literally every creator in a country or guarantee that 35,000 qualifying creators exist for a given filter set. The app stops at 35,000 PASS rows if the target is reached.
""")
    st.subheader("Live provider")
    st.markdown("The live module uses the ScrapeCreators public-data API. Keep the API key in Streamlit Secrets, not GitHub.")
