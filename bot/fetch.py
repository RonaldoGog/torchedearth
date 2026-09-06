"""Gather candidate headlines from the curated feeds and from Google News searches.

Returns plain dicts:
  {id, url, title, source, source_id, published, blurb, paywall, weight, via}
Nothing here decides whether a story is about climate — that's classify.py.
"""
import base64
import concurrent.futures
import re
import time
import urllib.parse
from datetime import datetime, timezone

import feedparser
import requests

from common import DATA, canonical, domain_of, item_id, load_json, save_json, slug, strip_html

UA = "TorchedEarthBot/1.0 (+https://torchedearth.org/sources/)"
TIMEOUT = 20
URL_CACHE = DATA / "url_cache.json"


def _date(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


def _get(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _source_index(feeds):
    return {f["domain"]: f for f in feeds if f.get("domain")}


def fetch_feed(feed):
    """One curated RSS/Atom feed -> (items, status_string)."""
    try:
        parsed = feedparser.parse(_get(feed["rss"]).content)
    except Exception as e:  # network error, 404, etc.
        return [], f"error: {e.__class__.__name__}"
    if parsed.bozo and not parsed.entries:
        return [], "error: not a valid feed"
    items = []
    for e in parsed.entries:
        link = e.get("link") or ""
        title = strip_html(e.get("title") or "")
        if not link or not title:
            continue
        items.append({
            "id": item_id(link),
            "url": canonical(link),
            "title": title,
            "source": feed["name"],
            "source_id": feed["id"],
            "published": _date(e),
            "blurb": strip_html(e.get("summary") or e.get("description") or "")[:400],
            "paywall": bool(feed.get("paywall")),
            "weight": int(feed.get("weight", 5)),
            "via": "rss",
        })
    return items, f"ok: {len(items)} items"


# ---- Google News ----------------------------------------------------------

def google_news_url(query, window):
    q = urllib.parse.quote_plus(f"{query} when:{window}".strip())
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def _decode_google_link(url):
    """Best effort: older Google News links embed the target URL in base64."""
    m = re.search(r"/articles/([^/?]+)", url)
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    m2 = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m2:
        return None
    found = m2.group(0).decode("ascii", "ignore")
    return re.split(r"[\x00-\x1f]", found)[0].rstrip("\x01\x02\x03").strip("\"'") or None


def resolve_google_link(url):
    """Turn a news.google.com redirect into the publisher's URL (or leave it)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
        if "news.google.com" not in r.url:
            return r.url
    except Exception:
        pass
    decoded = _decode_google_link(url)
    return decoded or url


def fetch_google(queries, settings, source_index):
    """All Google News searches -> (items, statuses)."""
    fcfg = settings.get("fetch", {})
    window = fcfg.get("google_news_window", "2d")
    per_query = int(fcfg.get("google_news_per_query", 40))
    cache = load_json(URL_CACHE, {})
    searches = [(f"topic: {q}", q) for q in queries.get("topics", [])]
    searches += [(f"site: {s['domain']}", f"site:{s['domain']} {s.get('query', '')}".strip())
                 for s in queries.get("sites", [])]

    raw, statuses = [], {}
    for label, q in searches:
        try:
            parsed = feedparser.parse(_get(google_news_url(q, window)).content)
            entries = parsed.entries[:per_query]
            statuses[label] = f"ok: {len(entries)} items"
        except Exception as e:
            statuses[label] = f"error: {e.__class__.__name__}"
            continue
        for e in entries:
            title = strip_html(e.get("title") or "")
            source_name = (e.get("source") or {}).get("title") or ""
            if source_name and title.endswith(f" - {source_name}"):
                title = title[: -len(source_name) - 3].strip()
            raw.append({"glink": e.get("link"), "title": title, "source_name": source_name,
                        "published": _date(e), "blurb": strip_html(e.get("summary") or "")[:400]})

    # Unwrap Google's redirect links in parallel (cached across days).
    todo = {r["glink"] for r in raw if r["glink"] and r["glink"] not in cache}
    workers = int(fcfg.get("resolve_workers", 12))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for glink, final in zip(todo, ex.map(resolve_google_link, todo)):
            cache[glink] = final
    save_json(URL_CACHE, cache)

    items = []
    paywalled = set(settings.get("paywalled_domains", []))
    default_weight = int(fcfg.get("default_weight", 5))
    for r in raw:
        url = cache.get(r["glink"], r["glink"])
        if not url or not r["title"]:
            continue
        dom = domain_of(url)
        known = source_index.get(dom) or next((f for d, f in source_index.items() if dom.endswith("." + d)), None)
        items.append({
            "id": item_id(url),
            "url": canonical(url),
            "title": r["title"],
            "source": known["name"] if known else (r["source_name"] or dom),
            "source_id": known["id"] if known else slug(r["source_name"] or dom),
            "published": r["published"],
            "blurb": r["blurb"],
            "paywall": bool(known.get("paywall")) if known else any(dom == p or dom.endswith("." + p) for p in paywalled),
            "weight": int(known.get("weight", default_weight)) if known else default_weight,
            "via": "google",
        })
    return items, statuses


def fetch_all(feeds, queries, settings):
    """Everything, de-duplicated by id. Returns (items, feed_status)."""
    status = {"run": datetime.now(timezone.utc).isoformat(timespec="seconds"), "feeds": {}, "google": {}}
    items = {}
    for feed in feeds:
        if not feed.get("rss"):
            continue
        got, st = fetch_feed(feed)
        status["feeds"][feed["id"]] = st
        for it in got:
            items.setdefault(it["id"], it)
        time.sleep(0.3)
    got, st = fetch_google(queries, settings, _source_index(feeds))
    status["google"] = st
    for it in got:
        items.setdefault(it["id"], it)
    save_json(DATA / "feed_status.json", status)
    return list(items.values()), status
