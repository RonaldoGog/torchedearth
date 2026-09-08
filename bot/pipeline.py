#!/usr/bin/env python3
"""Daily run: fetch -> keep only new -> classify -> group duplicates -> save.

    python bot/pipeline.py            # normal run
    python bot/pipeline.py --dry-run  # fetch and report, change nothing

State lives in data/:
  items.json        every story kept (the database the site is built from)
  dropped.json      ids judged not about climate, so they aren't re-sent to the model
  feed_status.json  what each feed and search returned on the last run
  url_cache.json    Google News redirect -> publisher URL
"""
import sys
from datetime import date, timedelta

from classify import classify, match_stories
from common import DATA, load_json, load_yaml, save_json
from dedupe import assign_groups
from fetch import fetch_all, repair_google_links

ITEMS = DATA / "items.json"
DROPPED = DATA / "dropped.json"


def main(dry_run=False):
    settings = load_yaml("settings.yml")
    feeds = load_yaml("feeds.yml")["feeds"]
    queries = load_yaml("queries.yml")
    items = load_json(ITEMS, [])
    dropped = set(load_json(DROPPED, []))
    today = date.today()

    raw, _ = fetch_all(feeds, queries, settings)
    known = {i["id"] for i in items} | dropped
    new = []
    for r in raw:
        if r["id"] in known:
            continue
        known.add(r["id"])
        r["found"] = today.isoformat()
        r["status"] = "pending"
        new.append(r)
    print(f"fetched {len(raw)} candidates, {len(new)} new, {sum(1 for i in items if i.get('status') == 'pending')} still pending")
    if dry_run:
        for r in new[:30]:
            print(f"  [{r['source']}] {r['title']}")
        return

    items.extend(new)
    repair_google_links(items, feeds, settings)
    classify(items, settings)

    # Ask the model which of today's stories are the same as each other or as the last few days'.
    match_days = int(settings.get("llm", {}).get("match_days", 3))
    since = (today - timedelta(days=match_days)).isoformat()
    fresh = [i for i in new if i.get("status") == "ok" and i.get("relevance", 0) >= settings.get("min_relevance", 6)]
    fresh_ids = {i["id"] for i in fresh}
    recent = [i for i in items if i.get("status") == "ok" and i["id"] not in fresh_ids
              and i.get("published", "") >= since and i.get("group", i["id"]) == i["id"]]
    match_stories(fresh, recent, settings)

    keep = []
    for it in items:
        # Stories imported from the earlier hand-built sites were chosen by a person; never drop them.
        if it.get("status") == "ok" and it.get("relevance", 0) < settings.get("min_relevance", 6) and not it.get("archive"):
            dropped.add(it["id"])
        else:
            keep.append(it)
    items = assign_groups(keep, today)
    items.sort(key=lambda i: (i.get("published", ""), i.get("found", "")), reverse=True)

    save_json(ITEMS, items)
    save_json(DROPPED, sorted(dropped))
    ok = sum(1 for i in items if i.get("status") == "ok")
    print(f"saved {len(items)} items ({ok} classified), {len(dropped)} dropped as off-topic")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
