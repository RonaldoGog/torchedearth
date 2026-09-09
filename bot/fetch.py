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
    """One curated RSS/Atom feed -> (items, status_string).

    Tried first with the bot's own identity, then — if the site refuses that or hands
    back a web page instead of a feed — as an ordinary browser. Some hosts block
    anything that calls itself a bot. (A site behind a JavaScript "prove you are not a
    robot" challenge, like DeSmog, blocks both; use a Google News search for those.)"""
    parsed, error = None, None
    for ua in (UA, BROWSER_UA):
        try:
            r = requests.get(feed["rss"], headers={"User-Agent": ua}, timeout=TIMEOUT)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            if parsed.entries:
                break
            error = "not a valid feed"
        except Exception as e:  # network error, 403, 404, etc.
            error = e.__class__.__name__
    if parsed is None or not parsed.entries:
        return [], f"error: {error}"
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

# Unwrapping is bounded three ways, because Google refuses most lookups on a bad day
# and one run once spent four and a half hours being refused politely:
#   - a time budget per run (settings fetch.resolve_budget_seconds); when it's spent,
#     remaining links are skipped and picked up on a later run
#   - a circuit breaker: after RESOLVE_STRIKES "slow down" (429) answers the run stops
#     asking Google at all
#   - failures are remembered in url_cache.json and not retried for
#     fetch.retry_failed_days, so the same dead link isn't asked about every morning
RESOLVE_STRIKES = 3
_resolve = {"deadline": None, "strikes": 0, "tripped": False, "skipped": 0}


def start_resolve_budget(settings, seconds=None):
    secs = float(seconds if seconds is not None
                 else settings.get("fetch", {}).get("resolve_budget_seconds", 480))
    _resolve.update(deadline=time.monotonic() + secs, strikes=0, tripped=False, skipped=0)


def _oldest_last(iso):
    """Sort key that puts the newest date first in an ascending sort."""
    try:
        return tuple(-int(part) for part in str(iso).split("-")[:3])
    except (TypeError, ValueError):
        return (0, 0, 0)


def _resolve_open():
    if _resolve["tripped"]:
        return False
    return _resolve["deadline"] is None or time.monotonic() < _resolve["deadline"]


def _cache_split(cache, settings):
    """Drop stale entries. Successes live `cache_days`; failures are remembered `retry_failed_days`."""
    fcfg = settings.get("fetch", {})
    now = datetime.now(timezone.utc)
    keep_ok = (now - timedelta(days=int(fcfg.get("cache_days", 7)))).date().isoformat()
    keep_failed = (now - timedelta(days=int(fcfg.get("retry_failed_days", 3)))).date().isoformat()
    return {k: v for k, v in cache.items() if isinstance(v, dict)
            and ((v.get("url") and v.get("seen", "") >= keep_ok) or (not v.get("url") and v.get("failed", "") >= keep_failed))}


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


def _resolve_one(url):
    """('ok', publisher_url) | ('failed', None) if Google wouldn't say | ('skipped', None) if out of budget."""
    token = _google_token(url)
    if not token:
        return "failed", None
    if not _resolve_open():
        _resolve["skipped"] += 1
        return "skipped", None
    for attempt in range(2):
        try:
            found = _ask_google(token)
            time.sleep(RESOLVE_PAUSE)
            if found:
                return "ok", found
            break
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:  # Google asking us to slow down
                _resolve["strikes"] += 1
                if _resolve["strikes"] >= RESOLVE_STRIKES:
                    _resolve["tripped"] = True
                if attempt == 0 and not _resolve["tripped"]:
                    time.sleep(15)
                    continue
                _resolve["skipped"] += 1   # rate-limited, not refused: try again another day
                return "skipped", None
            break
        except Exception:
            break
    # Older fallback, kept in case Google ever reverts to plain redirects.
    if _resolve_open():
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
            if "news.google.com" not in r.url:
                return "ok", r.url
        except Exception:
            pass
    found = _decode_google_link(url)
    return ("ok", found) if found else ("failed", None)


def resolve_google_link(url):
    """Turn a news.google.com link into the publisher's URL. Returns None if it can't."""
    return _resolve_one(url)[1]


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

    # Unwrap Google's links to the publisher's URL, in parallel. Lookups are cached in
    # data/url_cache.json (committed with the rest of data/): a success is remembered
    # for `cache_days`, a failure for `retry_failed_days`, so no link is asked about
    # every morning. Bounded by the run's resolve budget (see start_resolve_budget).
    today = datetime.now(timezone.utc).date().isoformat()
    cache = _cache_split(cache, settings)
    if _resolve["deadline"] is None:
        start_resolve_budget(settings)
    # Which links get asked about matters as much as how many: on a throttled morning
    # only the first few hundred of these get an answer. Ask about the ones most likely
    # to reach a reader first — outlets already listed in feeds.yml, then newest —
    # rather than in URL order, which is what the alphabetical sort amounted to.
    listed = {(f.get("name") or "").lower() for f in source_index.values()}
    pending = {}
    for r in raw:
        glink = r["glink"]
        if not glink or glink in cache:
            continue
        key = (0 if (r["source_name"] or "").lower() in listed else 1,
               _oldest_last(r["published"]), glink)
        if glink not in pending or key < pending[glink]:
            pending[glink] = key
    todo = sorted(pending, key=pending.get)
    workers = int(fcfg.get("resolve_workers", 4))
    resolved = failed = skipped = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for glink, (state, final) in zip(todo, ex.map(_resolve_one, todo)):
            if state == "ok" and final and "news.google.com" not in final:
                cache[glink] = {"url": final, "seen": today}
                resolved += 1
            elif state == "skipped":
                skipped += 1
            else:
                cache[glink] = {"failed": today}
                failed += 1
    for r in raw:
        if cache.get(r["glink"], {}).get("url"):
            cache[r["glink"]]["seen"] = today
    save_json(URL_CACHE, cache)
    statuses["resolve"] = (f"{resolved} links unwrapped, {failed} could not be (left pointing at Google, "
                           f"not retried for {int(fcfg.get('retry_failed_days', 3))} days), {skipped} skipped: "
                           + ("Google asked us to stop" if _resolve["tripped"] else "out of time for this run"))

    items = []
    for r in raw:
        hit = cache.get(r["glink"]) or {}
        url = hit["url"] if hit.get("url") else r["glink"]
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
    cache = _cache_split(load_json(URL_CACHE, {}), settings)
    today = datetime.now(timezone.utc).date().isoformat()
    todo = sorted((i for i in items if "news.google.com" in i.get("url", "") and i["url"] not in cache),
                  key=lambda i: (i.get("published", ""), i.get("score", 0)), reverse=True)[:limit]
    if not todo:
        return 0
    # Repair holds its own budget and its own strike count. It used to share the search
    # step's, and checked whether that was still open before starting — so on any morning
    # Google tripped the breaker during the searches, this did nothing at all. That is how
    # 1,265 stories, 41% of the archive, were still linking through Google on 9 Sep 2026.
    # If the searches did trip it, wait before asking again rather than walking straight
    # back into the rate limit.
    fcfg = settings.get("fetch", {})
    if _resolve["tripped"]:
        time.sleep(float(fcfg.get("repair_cooldown_seconds", 60)))
    start_resolve_budget(settings, seconds=fcfg.get("repair_budget_seconds", 180))
    index = _source_index(feeds)
    workers = int(settings.get("fetch", {}).get("resolve_workers", 4))
    fixed = failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for it, (state, final) in zip(todo, ex.map(_resolve_one, [i["url"] for i in todo])):
            if state == "ok" and final and "news.google.com" not in final:
                cache[it["url"]] = {"url": canonical(final), "seen": today}
                it["url"] = canonical(final)
                it["source"], it["source_id"], it["paywall"], it["weight"] = label_source(final, it["source"], index, settings)
                fixed += 1
            elif state == "failed":
                cache[it["url"]] = {"failed": today}
                failed += 1
    save_json(URL_CACHE, cache)
    print(f"repaired {fixed} of {len(todo)} old Google links ({failed} refused, {len(todo) - fixed - failed} skipped)")
    return fixed


def fetch_all(feeds, queries, settings):
    """Everything, de-duplicated by id. Returns (items, feed_status)."""
    status = {"run": datetime.now(timezone.utc).isoformat(timespec="seconds"), "feeds": {}, "google": {}}
    start_resolve_budget(settings)   # one budget for this run's unwrapping, shared with repair_google_links
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
