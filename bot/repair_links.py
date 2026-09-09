#!/usr/bin/env python3
"""Unwrap a batch of the Google News links still sitting in data/items.json.

    python bot/repair_links.py         # one batch (fetch.repair_per_run in settings.yml)
    python bot/repair_links.py 50      # a smaller batch

Why this runs on its own schedule (.github/workflows/repair-links.yml) instead of
only inside the daily run: Google rate-limits these lookups by IP, and a runner
that has just asked 900 times in a row gets refused. Several small batches spread
across the day, each from a fresh runner, get through where one morning burst does
not. Newest and highest-scoring stories are repaired first, so the ones a reader
is most likely to click are fixed soonest.

Writes data/items.json and data/url_cache.json. The site itself is not rebuilt
here — the next daily run picks the repaired links up.
"""
import sys

from common import DATA, load_json, load_yaml, save_json
from fetch import repair_google_links

ITEMS = DATA / "items.json"


def main(limit=None):
    settings = load_yaml("settings.yml")
    if limit:
        settings.setdefault("fetch", {})["repair_per_run"] = int(limit)
    feeds = load_yaml("feeds.yml")["feeds"]
    items = load_json(ITEMS, [])
    if not items:
        print("no stories saved yet, nothing to repair")
        return 0

    before = sum(1 for i in items if "news.google.com" in i.get("url", ""))
    if not before:
        print("every story already links to its publisher")
        return 0

    fixed = repair_google_links(items, feeds, settings)
    if fixed:
        save_json(ITEMS, items)
    print(f"{fixed} repaired, {before - fixed} of {len(items)} stories still linking through Google")
    return fixed


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
