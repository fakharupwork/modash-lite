import io
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="AMANA Creator Finder", page_icon="🔎", layout="wide", initial_sidebar_state="expanded")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(APP_DIR, "amana_config.json"), "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

KEYWORDS = pd.DataFrame(CONFIG["keywords"])
COUNTRIES = pd.DataFrame(CONFIG["countries"])
DELIVERY_HEADERS = CONFIG["delivery_headers"]

EXTRA_EVIDENCE_HEADERS = [
    "country_evidence", "relevant_posts_90d", "supporting_post_1", "supporting_post_2",
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
    s = stext(v).strip().lower().replace(",", "").replace(" ", "")
    if not s:
        return None
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1000000, s[:-1]
    m = re.search(r"\d+(?:\.\d+)?", s)
    return int(float(m.group()) * mult) if m else None


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
        if v not in (None, "", [], {}):
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

    def __init__(self, api_key, timeout=45):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.calls = 0
        self.credits_charged = 0
        self.credits_remaining = None
        self.errors = []
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": self.api_key, "User-Agent": "AMANA-Creator-Finder/2.0"})

    def get(self, path, params=None, retries=2):
        url = self.BASE + path
        last = None
        for attempt in range(retries + 1):
            try:
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
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(0.7 * (attempt + 1))
                    continue
                self.errors.append(f"{path}: {e}")
        raise last


def add_candidate(store, platform, handle, full_name="", profile_url="", followers=None, search_row=None, discovery_url="", discovery_text=""):
    handle = normalize_handle(handle)
    if not handle:
        return
    key = f"{platform.casefold()}:{handle}"
    if key not in store:
        store[key] = {
            "platform": platform, "handle": handle, "full_name": full_name or "",
            "profile_url": profile_url or "", "followers_hint": parse_followers(followers),
            "searches": [], "discovery_urls": [], "discovery_texts": []
        }
    c = store[key]
    if not c["full_name"] and full_name:
        c["full_name"] = full_name
    if not c["profile_url"] and profile_url:
        c["profile_url"] = profile_url
    if c["followers_hint"] is None and followers is not None:
        c["followers_hint"] = parse_followers(followers)
    if search_row:
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


def discover_instagram(client, store, search_row, country, iso, pages, add_country_term):
    keyword = stext(search_row["Keyword"]).strip()
    field = stext(search_row["Modash field"])
    queries = [keyword]
    if add_country_term:
        queries.append(f"{keyword} {country}")
    for query in list(dict.fromkeys(queries)):
        if field == "Bio":
            try:
                data = client.get("/v1/instagram/search/profiles", {"query": query})
            except Exception:
                continue
            for p in data.get("profiles", []) if isinstance(data, dict) else []:
                add_candidate(store, "Instagram", p.get("username"), p.get("full_name"), p.get("url"), p.get("follower_count"), search_row)
        else:
            for page in range(1, min(int(pages), 11) + 1):
                try:
                    data = client.get("/v2/instagram/reels/search", {"query": query, "date_posted": "last-year", "page": page})
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
                        owner.get("follower_count"), search_row, reel.get("url", ""), reel.get("caption", "")
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


def discover_tiktok(client, store, search_row, country, iso, pages, add_country_term):
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
                if cursor not in (None, "", 0): params["cursor"] = cursor
                try:
                    data = client.get("/v1/tiktok/search/users", params)
                except Exception:
                    break
                nodes = _find_tiktok_user_nodes(data)
                if not nodes:
                    break
                for u in nodes:
                    handle = tiktok_handle_from_obj(u)
                    name = first_nonempty(u.get("nickname"), u.get("nick_name"), u.get("full_name"), "")
                    followers = first_nonempty(u.get("follower_count"), u.get("followerCount"), nested_get(u, "stats.followerCount"))
                    add_candidate(store, "TikTok", handle, name, f"https://www.tiktok.com/@{handle}" if handle else "", followers, search_row)
                nxt = data.get("cursor") if isinstance(data, dict) else None
                if nxt in (None, "", cursor): break
                cursor = nxt
        else:
            cursor = None
            for _ in range(int(pages)):
                params = {"query": query, "sort_by": "relevance", "date_posted": "last-6-months", "region": iso, "trim": "true"}
                if cursor not in (None, "", 0): params["cursor"] = cursor
                try:
                    data = client.get("/v1/tiktok/search/keyword", params)
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
                    followers = first_nonempty(author.get("follower_count") if isinstance(author, dict) else None, author.get("followerCount") if isinstance(author, dict) else None, (stats or {}).get("followerCount") if isinstance(stats, dict) else None)
                    url = first_nonempty(aweme.get("url"), item.get("url"), "")
                    desc = first_nonempty(aweme.get("desc"), item.get("desc"), item.get("search_desc"), "")
                    add_candidate(store, "TikTok", handle, name, f"https://www.tiktok.com/@{handle}" if handle else "", followers, search_row, url, desc)
                nxt = data.get("cursor") if isinstance(data, dict) else None
                if nxt in (None, "", cursor): break
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


def discover_youtube(client, store, search_row, country, iso, pages, add_country_term):
    keyword = stext(search_row["Keyword"]).strip()
    field = stext(search_row["Modash field"])
    queries = [keyword]
    if add_country_term:
        queries.append(f"{keyword} {country}")
    for query in list(dict.fromkeys(queries)):
        token = None
        for _ in range(int(pages)):
            params = {"query": query, "type": "channels" if field == "Bio" else "videos", "sortBy": "relevance", "region": iso, "includeExtras": "true"}
            if token: params["continuationToken"] = token
            try:
                data = client.get("/v1/youtube/search", params)
            except Exception:
                break
            result_lists = []
            if isinstance(data, dict):
                for key in ["channels", "videos", "results", "items"]:
                    if isinstance(data.get(key), list): result_lists.extend(data[key])
            if not result_lists:
                break
            for item in result_lists:
                ch = _youtube_channel_from_result(item)
                if not ch: continue
                channel_id = first_nonempty(ch.get("id"), ch.get("channelId"), item.get("channelId"))
                handle = youtube_handle_from_obj(ch)
                name = first_nonempty(ch.get("title"), ch.get("name"), "")
                url = first_nonempty(ch.get("url"), ch.get("channel"), f"https://www.youtube.com/channel/{channel_id}" if channel_id else "")
                followers = first_nonempty(ch.get("subscriberCount"), ch.get("subscriber_count"), ch.get("subscribers"))
                discovery_url = item.get("url", "") if field != "Bio" else ""
                discovery_text = " ".join([stext(item.get("title")), stext(item.get("description"))]) if field != "Bio" else ""
                add_candidate(store, "YouTube", handle or channel_id, name, url, followers, search_row, discovery_url, discovery_text)
            token2 = first_nonempty(data.get("continuationToken"), data.get("continuation_token")) if isinstance(data, dict) else None
            if not token2 or token2 == token: break
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


def enrich_and_qualify(client, candidate, country, iso, extra_terms, follower_min, follower_max,
                       match_mode, require_country, last_post_days, relevant_days, min_relevant_posts,
                       max_posts, exclude_private, cache_age="1d"):
    platform = candidate["platform"]
    handle = candidate["handle"]
    errors = []
    try:
        if platform == "Instagram":
            profile_data = client.get("/v1/instagram/profile", {"handle": handle, "trim": "false", "cache_max_age": cache_age})
            profile = extract_instagram_profile(profile_data, handle)
            post_data = client.get("/v2/instagram/user/posts", {"handle": handle, "trim": "true"})
            posts = instagram_posts(post_data, handle)
            country_ok, country_ev = profile_location_evidence(profile_data, profile["bio"], country, iso, extra_terms)
            country_source = profile["profile_url"] if country_ok else ""
        elif platform == "TikTok":
            profile_data = client.get("/v1/tiktok/profile", {"handle": handle, "cache_max_age": cache_age})
            profile = extract_tiktok_profile(profile_data, handle)
            region = {}
            try:
                region = client.get("/v1/tiktok/profile/region", {"handle": handle})
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
            # Use profile URL if the discovery handle is really a channel ID.
            params = {"cache_max_age": cache_age}
            if handle.startswith("UC") and len(handle) > 15: params["channelId"] = handle
            elif candidate.get("profile_url"): params["url"] = candidate["profile_url"]
            else: params["handle"] = handle
            profile_data = client.get("/v1/youtube/channel", params)
            profile = extract_youtube_profile(profile_data, handle)
            ycountry = stext(profile_data.get("country") if isinstance(profile_data, dict) else "")
            aliases = {x.casefold() for x in country_aliases(country, iso)}
            country_ok = ycountry.casefold() in aliases if ycountry else False
            country_ev = f"YouTube country={ycountry}" if country_ok else ""
            country_source = profile["profile_url"] if country_ok else ""
            channel_id = profile_data.get("channelId") if isinstance(profile_data, dict) else None
            params2 = {"channelId": channel_id} if channel_id else {"handle": handle}
            post_data = client.get("/v1/youtube/channel-videos", params2)
            posts = youtube_posts(post_data, handle)
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
        reasons.append("selected keyword not verified in bio/recent content")

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
        "language": unique_join(match["langs"]) or profile.get("language", ""),
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


def get_secret_api_key():
    try:
        return stext(st.secrets.get("SCRAPECREATORS_API_KEY", ""))
    except Exception:
        return ""


# Header
st.markdown('<div class="am-title">AMANA Creator Finder Live</div>', unsafe_allow_html=True)
st.markdown('<div class="am-sub">Live creator discovery + strict AMANA qualification for Instagram, TikTok and YouTube.</div>', unsafe_allow_html=True)
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

live_tab, upload_tab, queue_tab, rules_tab = st.tabs(["🔴 Live Search Creators", "📥 Filter Upload", "📋 Search Queue", "⚙️ Rules"])

with live_tab:
    st.subheader("Live Search Creators")
    st.caption("Searches public platform data through ScrapeCreators, then verifies follower range, country evidence, recent activity and content relevance. Email is optional and stays blank when unavailable.")

    secret_key = get_secret_api_key()
    api_key = st.text_input("ScrapeCreators API key", value=secret_key, type="password", help="For Streamlit Cloud, store this as SCRAPECREATORS_API_KEY in App settings → Secrets. Do not put the key in GitHub.")

    active = keyword_subset(selected_langs, fields, themes)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Active searches", f"{len(active):,}")
    c2.metric("Country", f"{country} ({iso})")
    c3.metric("Followers", f"{follower_min:,}-{follower_max:,}")
    c4.metric("Platforms", len(platforms))

    with st.expander("Coverage and qualification settings", expanded=True):
        a, b, c = st.columns(3)
        start_index = int(a.number_input("Start at keyword #", min_value=1, max_value=max(1, len(active)), value=1, step=1))
        max_keywords = int(b.number_input("Keywords this run (0 = all remaining)", min_value=0, value=min(20, max(1, len(active))), step=5))
        pages_per_search = int(c.number_input("Discovery pages per search", min_value=1, max_value=11, value=2, step=1))

        d, e, f = st.columns(3)
        candidate_cap = int(d.number_input("Max unique candidates to qualify (0 = unlimited)", min_value=0, value=200, step=50))
        match_mode = e.selectbox("Keyword verification", ["Exact phrase", "All words", "Any word"], index=0)
        add_country_term = f.checkbox("Also search 'keyword + country'", value=True)

        g, h, i = st.columns(3)
        require_country = g.checkbox("Require public creator-country evidence", value=True)
        exclude_private = h.checkbox("Exclude private accounts", value=True)
        cache_age = i.selectbox("Reuse cached profile data", ["1d", "3d", "7d"], index=0)

        j, k, l = st.columns(3)
        last_post_days = int(j.number_input("Last post within days", min_value=0, value=30, step=1))
        relevant_days = int(k.number_input("Relevant-post window days", min_value=1, value=90, step=10))
        min_relevant_posts = int(l.number_input("Minimum relevant posts", min_value=0, value=2, step=1))
        max_posts = int(st.number_input("Check latest N posts", min_value=1, max_value=12, value=6, step=1))
        extra_location_text = st.text_input("Extra creator-location terms (optional)", placeholder="Delhi, Mumbai, Karachi, London... Use city/region names for stronger Instagram verification")
        extra_terms = [x.strip() for x in extra_location_text.split(",") if x.strip()]

    if active.empty:
        st.warning("No keyword searches are active. Select language codes/search fields/themes in the sidebar.")
    else:
        end_idx = len(active) if max_keywords == 0 else min(len(active), start_index - 1 + max_keywords)
        run_rows = active.iloc[start_index - 1:end_idx].copy()
        discovery_multiplier = (2 if add_country_term else 1) * max(1, pages_per_search)
        st.info(f"This run will process keyword rows {start_index}-{end_idx} of {len(active)}. Discovery can use roughly {len(run_rows) * max(1,len(platforms)) * discovery_multiplier:,} API calls before profile qualification. Actual usage varies by platform and pagination.")

    col_run, col_clear = st.columns([3,1])
    run_clicked = col_run.button("Find & qualify creators", type="primary", width="stretch", disabled=(not api_key or active.empty or not platforms))
    if col_clear.button("Clear live results", width="stretch"):
        st.session_state.pop("live_pass", None)
        st.session_state.pop("live_reject", None)
        st.session_state.pop("live_summary", None)
        st.rerun()

    if run_clicked:
        client = ScrapeCreatorsClient(api_key)
        candidates = {}
        status = st.status("Discovering creators...", expanded=True)
        progress = st.progress(0)
        total_steps = max(1, len(run_rows) * len(platforms))
        step = 0
        for _, sr in run_rows.iterrows():
            status.write(f"Searching: {sr['Keyword']} | {sr['Modash field']} | {sr['Language code']}")
            for platform in platforms:
                try:
                    if platform == "Instagram":
                        discover_instagram(client, candidates, sr, country, iso, pages_per_search, add_country_term)
                    elif platform == "TikTok":
                        discover_tiktok(client, candidates, sr, country, iso, pages_per_search, add_country_term)
                    else:
                        discover_youtube(client, candidates, sr, country, iso, pages_per_search, add_country_term)
                except Exception as e:
                    status.write(f"{platform} search error: {e}")
                step += 1
                progress.progress(min(1.0, step / total_steps))
        status.update(label=f"Discovery complete: {len(candidates):,} unique candidates", state="complete")

        # Cheap prefilter using discovered follower count when available.
        candidate_list = []
        for cnd in candidates.values():
            fh = cnd.get("followers_hint")
            if fh is not None and not (follower_min <= fh <= follower_max):
                continue
            candidate_list.append(cnd)
        if candidate_cap > 0:
            candidate_list = candidate_list[:candidate_cap]

        qstatus = st.status(f"Qualifying {len(candidate_list):,} candidates...", expanded=True)
        qprog = st.progress(0)
        pass_rows, reject_rows = [], []
        for idx, cnd in enumerate(candidate_list, start=1):
            qstatus.write(f"{idx}/{len(candidate_list)} {cnd['platform']} @{cnd['handle']}")
            row, err = enrich_and_qualify(
                client, cnd, country, iso, extra_terms, follower_min, follower_max,
                match_mode, require_country, last_post_days, relevant_days, min_relevant_posts,
                max_posts, exclude_private, cache_age
            )
            if row:
                if row["qualification_status"] == "PASS": pass_rows.append(row)
                else: reject_rows.append(row)
            elif err:
                reject_rows.append({"platform": cnd["platform"], "account_handle": cnd["handle"], "profile_url": cnd.get("profile_url", ""), "qualification_status": "REJECT", "qualification_notes": err})
            qprog.progress(min(1.0, idx / max(1, len(candidate_list))))
        qstatus.update(label=f"Qualification complete: {len(pass_rows):,} passed", state="complete")

        # Dedupe against existing session results.
        old_pass = st.session_state.get("live_pass", [])
        old_reject = st.session_state.get("live_reject", [])
        merged = {}
        for row in old_pass + pass_rows:
            key = f"{stext(row.get('platform')).casefold()}:{normalize_handle(row.get('account_handle'))}"
            if key and key not in merged: merged[key] = row
        rej_merged = {}
        for row in old_reject + reject_rows:
            key = f"{stext(row.get('platform')).casefold()}:{normalize_handle(row.get('account_handle'))}"
            if key and key not in rej_merged: rej_merged[key] = row
        st.session_state["live_pass"] = list(merged.values())
        st.session_state["live_reject"] = list(rej_merged.values())
        st.session_state["live_summary"] = {
            "discovered": len(candidates), "prefiltered": len(candidate_list), "passed_this_run": len(pass_rows),
            "rejected_this_run": len(reject_rows), "api_calls": client.calls, "credits_charged": client.credits_charged,
            "credits_remaining": client.credits_remaining, "errors": client.errors[-20:]
        }

    if st.session_state.get("live_summary"):
        s = st.session_state["live_summary"]
        m1,m2,m3,m4,m5 = st.columns(5)
        m1.metric("Discovered", f"{s['discovered']:,}")
        m2.metric("Qualified this run", f"{s['passed_this_run']:,}")
        m3.metric("Rejected this run", f"{s['rejected_this_run']:,}")
        m4.metric("API calls", f"{s['api_calls']:,}")
        m5.metric("Credits charged", f"{s['credits_charged']:,}")
        if s.get("credits_remaining") is not None:
            st.caption(f"ScrapeCreators credits remaining: {s['credits_remaining']}")

    pass_rows = st.session_state.get("live_pass", [])
    reject_rows = st.session_state.get("live_reject", [])
    if pass_rows or reject_rows:
        t1, t2 = st.tabs([f"✅ Qualified ({len(pass_rows):,})", f"❌ Rejected ({len(reject_rows):,})"])
        with t1:
            df_pass = live_results_dataframe(pass_rows)
            st.dataframe(df_pass, width="stretch", height=520, hide_index=True)
            st.download_button("Download qualified creators CSV", as_csv_bytes(df_pass), "AMANA_live_qualified.csv", "text/csv")
        with t2:
            df_rej = live_results_dataframe(reject_rows)
            show_cols = [c for c in ["platform","account_handle","followers","profile_url","qualification_notes"] if c in df_rej.columns]
            st.dataframe(df_rej[show_cols], width="stretch", height=420, hide_index=True)
            st.download_button("Download rejected audit CSV", as_csv_bytes(df_rej), "AMANA_live_rejected_audit.csv", "text/csv")

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
A live result is marked **PASS** only when it satisfies the enabled checks: follower range, public creator-country evidence, public account, selected keyword relevance, recent activity, and the required number of relevant recent posts. Email is **not required**; it remains blank when no public email is returned.

**Coverage note:** the tool can search all configured AMANA keywords and paginate where the provider supports it, but no third-party tool can guarantee literally every creator in a country. Instagram, TikTok and YouTube expose ranked/limited public search results and some creators do not publish country data. The strict mode therefore favors verified rows over guessing location.
""")
    st.subheader("Live provider")
    st.markdown("The live module uses the ScrapeCreators public-data API. Keep the API key in Streamlit Secrets, not GitHub.")
