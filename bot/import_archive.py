#!/usr/bin/env python3
"""Bring hand-picked headlines from the earlier Torched Earth sites into the database.

    python bot/import_archive.py data/imports/old-sites.csv

The CSV has one row per story: site, date, date_basis, section, source, title, url.
Each becomes an item in data/items.json with:
  - the section from the sheet, locked so the classifier won't change it
  - archive = which old site it came from (the templates show "from the archive")
  - status "pending", so the next daily run adds the importance score, the
    study/reprint label and the one-line summary, exactly as for a new headline.
    Imported stories are never dropped for low relevance: they were chosen by hand.
Stories whose URL is already in the database are skipped, so the script can be
run again safely.
"""
import csv
import re
import sys
from datetime import date

from common import DATA, canonical, item_id, load_json, load_yaml, save_json
from fetch import _source_index, label_source

ITEMS = DATA / "items.json"


def main(path):
    settings = load_yaml("settings.yml")
    feeds = load_yaml("feeds.yml")["feeds"]
    index = _source_index(feeds)
    items = load_json(ITEMS, [])
    known = {i["id"] for i in items}
    sections = set(settings["sections"])
    today = date.today().isoformat()

    added = skipped = 0
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.get("date", "")) or not row.get("title", "").strip():
                sys.exit(f"row without a valid date or title, nothing imported: {row}")
            url = canonical(row["url"])
            iid = item_id(url)
            if iid in known:
                skipped += 1
                continue
            source, source_id, paywall, weight = label_source(url, row["source"], index, settings)
            items.append({
                "id": iid,
                "url": url,
                "title": row["title"].strip(),
                "source": source,
                "source_id": source_id,
                "published": row["date"],
                "blurb": "",
                "paywall": paywall,
                "weight": weight,
                "via": "archive",
                "archive": row["site"],
                "found": today,
                "status": "pending",
                "section": row["section"] if row["section"] in sections else "other",
                "section_locked": True,
            })
            known.add(iid)
            added += 1

    items.sort(key=lambda i: (i.get("published", ""), i.get("found", "")), reverse=True)
    save_json(ITEMS, items)
    print(f"imported {added} stories, skipped {skipped} already in the database; {len(items)} items total")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DATA / "imports" / "old-sites.csv")
