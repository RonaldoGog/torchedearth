"""Gather candidate headlines from the curated feeds and from Google News searches.

Returns plain dicts:
  {id, url, title, source, source_id, published, blurb, paywall, weight, via}
Nothing here decides whether a story is about climate — that's classify.py.
"""
import base64
import concurrent.futures
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

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


def _google_token(url):
    m = re.search(r"news\.google\.com/(?:rss/)?(?:articles|read)/([^/?#]+)", url)
    return m.group(1) if m else None


def _decode_google_link(url):
    """Fallback for the old link format, where the target URL was inside the base64 token."""
    token = _google_token(url)
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    m2 = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m2:
        return None
    found = m2.group(0).decode("ascii", "ignore")
    return re.split(r"[\x00-\x1f]", found)[0].rstrip("\x01\x02\x03").strip("\"'") or None


# Since 2024 Google's news links no longer redirect and no longer contain the target
# URL. The publisher's URL has to be asked for: load the article page to pick up a
# signature and timestamp, then post those to Google's internal "batchexecute"
# endpoint, which answers with the real link. This is the method the open-source
# googlenewsdecoder package uses (github.com/SSujitX/google-news-url-decoder); if
# Google changes things again, that project is the place to look for the new recipe.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
RESOLVE_PAUSE = 0.5   # seconds between lookups, per worker


def _ask_google(token):
    page = requests.get(f"https://news.google.com/articles/{token}",
                        headers={"User-Agent": BROWSER_UA}, timeout=TIMEOUT)
    page.raise_for_status()
    sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not sig or not ts:
        return None
    req = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
           f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{token}",{ts.group(1)},"{sig.group(1)}"]')
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", req, None, "generic"]]]))
    r = requests.post(BATCH_URL, data=body, timeout=TIMEOUT,
                      headers={"User-Agent": BROWSER_UA,
                               "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    r.raise_for_status()
    # The reply is an anti-hijack prefix, then one or more JSON arrays (sometimes each
    # preceded by a byte count). The URL is inside a JSON string in the first array.
    text = r.text
    start = text.find("[[")
    if start < 0:
        return None
    outer, _ = json.JSONDecoder().raw_decode(text, start)
    for part in outer:
        if isinstance(part, list) and len(part) > 2 and isinstance(part[2], str):
            try:
                inner = json.loads(part[2])
            except ValueError:
                continue
            if isinstance(inner, list) and len(inner) > 1 and str(inner[1]).startswith("http"):
                return inner[1]
    return None


def resolve_google_link(url):
    """Turn a news.google.com link into the publisher's URL. Returns None if it can't."""
    token = _google_token(url)
    if not token:
        return None
    for attempt in range(3):
        try:
            found = _ask_google(token)
            time.sleep(RESOLVE_PAUSE)
            if found:
                return found
            break
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:  # Google asking us to slow down
                time.sleep(15 * (attempt + 1))
                continue
            break
        except Exception:
            break
    # Older fallbacks, kept in case Google ever reverts.
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
        if "news.google.com" not in r.url:
            return r.url
    except Exception:
        pass
    return _decode_google_link(url)


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

    # Unwrap Google's links to the publisher's URL, in parallel. Successful lookups
    # are cached in data/url_cache.json (committed with the rest of data/) so a link
    # is only ever asked about once; entries drop out after `cache_days`.
    today = datetime.now(timezone.utc).date().isoformat()
    keep_days = int(fcfg.get("cache_days", 7))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).date().isoformat()
    cache = {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("seen", "") >= cutoff}
    todo = sorted({r["glink"] for r in raw if r["glink"] and r["glink"] not in cache})
    workers = int(fcfg.get("resolve_workers", 4))
    resolved = failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for glink, final in zip(todo, ex.map(resolve_google_link, todo)):
            if final and "news.google.com" not in final:
                cache[glink] = {"url": final, "seen": today}
                resolved += 1
            else:
                failed += 1
    for r in raw:
        if r["glink"] in cache:
            cache[r["glink"]]["seen"] = today
    save_json(URL_CACHE, cache)
    statuses["resolve"] = f"{resolved} links unwrapped, {failed} could not be (left pointing at Google)"

    items = []
    for r in raw:
        url = cache[r["glink"]]["url"] if r["glink"] in cache else r["glink"]
        if not url or not r["title"]:
            continue
        source, source_id, paywall, weight = label_source(url, r["source_name"], source_index, settings)
        items.append({
            "id": item_id(url),
            "url": canonical(url),
            "title": r["title"],
            "source": source,
            "source_id": source_id,
            "published": r["published"],
            "blurb": r["blurb"],
            "paywall": paywall,
            "weight": weight,
            "via": "google",
        })
    return items, statuses


def label_source(url, source_name, source_index, settings):
    """(source, source_id, paywall, weight) for a publisher URL: from feeds.yml if the
    outlet is listed there, otherwise from Google's name for it and settings.paywalled_domains."""
    dom = domain_of(url)
    known = source_index.get(dom) or next((f for d, f in source_index.items() if dom.endswith("." + d)), None)
    if known:
        return known["name"], known["id"], bool(known.get("paywall")), int(known.get("weight", 5))
    paywalled = settings.get("paywalled_domains", [])
    name = source_name or dom
    return (name, slug(name), any(dom == p or dom.endswith("." + p) for p in paywalled),
            int(settings.get("fetch", {}).get("default_weight", 5)))


def repair_google_links(items, feeds, settings):
    """Stories saved while unwrapping wasn't working still link through news.google.com and
    carry a default weight and no paywall flag. Unwrap a batch of them each run, newest first,
    and relabel. Ids are left alone so groups and the dropped list stay valid."""
    limit = int(settings.get("fetch", {}).get("repair_per_run", 150))
    todo = sorted((i for i in items if "news.google.com" in i.get("url", "")),
                  key=lambda i: i.get("published", ""), reverse=True)[:limit]
    if not todo:
        return 0
    index = _source_index(feeds)
    workers = int(settings.get("fetch", {}).get("resolve_workers", 4))
    fixed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for it, final in zip(todo, ex.map(resolve_google_link, [i["url"] for i in todo])):
            if final and "news.google.com" not in final:
                it["url"] = canonical(final)
                it["source"], it["source_id"], it["paywall"], it["weight"] = label_source(final, it["source"], index, settings)
                fixed += 1
    print(f"repaired {fixed} of {len(todo)} old Google links")
    return fixed


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
    print("google links:", st.get("resolve", "no searches ran"))
    for it in got:
        items.setdefault(it["id"], it)
    save_json(DATA / "feed_status.json", status)
    return list(items.values()), status


if __name__ == "__main__":
    # Quick check that Google links unwrap. With no arguments it tries three links from
    # the archive:  python bot/fetch.py      or:  python bot/fetch.py <news.google.com link>
    import sys
    links = sys.argv[1:] or [i["url"] for i in load_json(DATA / "items.json", []) if "news.google.com" in i["url"]][:3]
    for u in links:
        print(resolve_google_link(u) or "could not unwrap", "<-", u[:60] + "...")
