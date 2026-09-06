#!/usr/bin/env python3
"""Post the day's top stories to Bluesky and Mastodon.

Environment variables (set as GitHub secrets):
  BLUESKY_HANDLE, BLUESKY_APP_PASSWORD   -> posts to Bluesky   (create an app password in Bluesky settings)
  MASTODON_URL, MASTODON_TOKEN           -> posts to Mastodon  (server URL + an access token with write:statuses)

Picks up to settings.social.posts_per_day stories found today that qualify for the
front page, highest score first, and remembers what it posted in data/posted.json.
X/Twitter is deliberately not wired in: its API charges per post with a link.
"""
import json
import os
from datetime import date, datetime, timezone

import requests

from common import DATA, load_json, load_yaml, save_json

POSTED = DATA / "posted.json"


def compose(it, limit=290):
    tail = f" ({it['source']})\n{it['url']}"
    title = it["title"]
    room = limit - len(tail)
    if len(title) > room:
        title = title[: room - 1].rstrip() + "…"
    return title + tail


def post_bluesky(text, url):
    handle, pw = os.environ.get("BLUESKY_HANDLE"), os.environ.get("BLUESKY_APP_PASSWORD")
    if not (handle and pw):
        return False
    s = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                      json={"identifier": handle, "password": pw}, timeout=30)
    s.raise_for_status()
    session = s.json()
    b = text.encode("utf-8")
    start = b.find(url.encode("utf-8"))
    record = {"$type": "app.bsky.feed.post", "text": text,
              "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    if start >= 0:
        record["facets"] = [{"index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
                             "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]}]
    r = requests.post("https://bsky.social/xrpc/com.atproto.repo.createRecord",
                      headers={"Authorization": f"Bearer {session['accessJwt']}"},
                      json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record}, timeout=30)
    r.raise_for_status()
    return True


def post_mastodon(text):
    base, token = os.environ.get("MASTODON_URL"), os.environ.get("MASTODON_TOKEN")
    if not (base and token):
        return False
    r = requests.post(f"{base.rstrip('/')}/api/v1/statuses", headers={"Authorization": f"Bearer {token}"},
                      data={"status": text}, timeout=30)
    r.raise_for_status()
    return True


def main():
    settings = load_yaml("settings.yml")
    items = load_json(DATA / "items.json", [])
    posted = set(load_json(POSTED, []))
    today = date.today().isoformat()
    n = int(settings.get("social", {}).get("posts_per_day", 5))

    candidates = [i for i in items if i.get("status") == "ok" and i.get("found") == today
                  and i.get("group", i["id"]) == i["id"] and i["id"] not in posted
                  and i.get("type") in settings["homepage_types"]
                  and i.get("score", 0) >= settings["min_score_homepage"]]
    candidates.sort(key=lambda i: i.get("score", 0), reverse=True)
    if not any(os.environ.get(k) for k in ("BLUESKY_HANDLE", "MASTODON_TOKEN")):
        print("no social accounts configured; skipping")
        return
    for it in candidates[:n]:
        text = compose(it)
        try:
            b = post_bluesky(text, it["url"])
            m = post_mastodon(text)
            posted.add(it["id"])
            print(f"posted ({'bluesky ' if b else ''}{'mastodon' if m else ''}): {it['title']}")
        except Exception as e:
            print(f"failed: {it['title']}: {e}")
    save_json(POSTED, sorted(posted))


if __name__ == "__main__":
    main()
