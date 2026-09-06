#!/usr/bin/env python3
"""Build the Sunday newsletter: the ten highest-ranked stories of the past week.

Writes data/newsletters/YYYY-MM-DD.html and .md (the site publishes the HTML
under /weekly/), then, if an email provider is configured, creates the issue there.

  BUTTONDOWN_API_KEY   -> creates the issue in Buttondown. NEWSLETTER_SEND=true
                          sends it; otherwise it is saved as a draft for a final look.

Other providers (beehiiv, Kit) have their own APIs; add a function like
push_buttondown() for whichever you choose. The .md file is also fine to paste
into any provider's editor by hand.
"""
import os
import sys
from datetime import date, timedelta

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from build import date_label
from common import DATA, TEMPLATES, load_json, load_yaml, save_json

OUT = DATA / "newsletters"


def pick(items, settings, today, days=7):
    since = (today - timedelta(days=days)).isoformat()
    types = settings["types"]
    by_group = {}
    for it in items:
        if it.get("status") == "ok":
            by_group.setdefault(it.get("group", it["id"]), []).append(it)
    chosen = []
    for gid, members in by_group.items():
        rep = next((m for m in members if m["id"] == gid), members[0])
        if rep.get("found", "") < since or rep.get("type") not in settings["homepage_types"]:
            continue
        rep = dict(rep)
        rep["type_label"] = types.get(rep.get("type"), "")
        rep["date_label"] = date_label(rep["published"], today)
        rep["also"] = [{"source": m["source"], "url": m["url"], "paywall": m.get("paywall", False)}
                       for m in sorted(members, key=lambda m: (m.get("paywall", False), -m.get("weight", 5))) if m["id"] != gid]
        chosen.append(rep)
    chosen.sort(key=lambda r: (r.get("score", 0), r["published"]), reverse=True)
    total = sum(1 for it in items if it.get("status") == "ok" and it.get("found", "") >= since and it.get("group", it["id"]) == it["id"])
    if len(chosen) < 5 and days < 28:          # slow week: look back further rather than send a thin issue
        return pick(items, settings, today, days * 2)
    return chosen[: int(settings.get("newsletter_top_n", 10))], total


def markdown(issue_label, items, total, settings):
    lines = [f"# {settings['site_name']}: week ending {issue_label}", "",
             f"The most important climate stories of the week. {total} stories were listed on the site this week.", ""]
    for it in items:
        label = f" *{it['type_label']}*" if it["type_label"] else ""
        pay = " $" if it.get("paywall") else ""
        lines.append(f"**[{it['title']}]({it['url']})**{label}  ")
        lines.append(f"{it['summary']}  ")
        also = ", ".join(f"[{a['source']}]({a['url']}){' $' if a['paywall'] else ''}" for a in it["also"])
        lines.append(f"_{it['source']}{pay}, {it['date_label']}{' · Also: ' + also if also else ''}_")
        lines.append("")
    lines.append(f"Headlines belong to their publishers; links go to the originals. [Search the archive]({settings['base_url']}/archive/).")
    return "\n".join(lines)


def push_buttondown(subject, body_md):
    key = os.environ.get("BUTTONDOWN_API_KEY")
    if not key:
        return None
    status = "about_to_send" if os.environ.get("NEWSLETTER_SEND", "").lower() == "true" else "draft"
    r = requests.post("https://api.buttondown.com/v1/emails",
                      headers={"Authorization": f"Token {key}"},
                      json={"subject": subject, "body": body_md, "status": status}, timeout=60)
    r.raise_for_status()
    return status


def main(today=None):
    today = today or date.today()
    if "--today" in sys.argv:
        today = date.fromisoformat(sys.argv[sys.argv.index("--today") + 1])
    settings = load_yaml("settings.yml")
    items = load_json(DATA / "items.json", [])
    chosen, total = pick(items, settings, today)
    if not chosen:
        print("nothing to send this week")
        return
    issue_date = today.isoformat()
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    issue_label = f"{today:%B} {today.day}, {today.year}"
    html = env.get_template("newsletter.html").render(site=settings, issue_date=issue_date, issue_label=issue_label, items=chosen, total=total)
    md = markdown(issue_label, chosen, total, settings)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{issue_date}.html").write_text(html, encoding="utf-8")
    (OUT / f"{issue_date}.md").write_text(md, encoding="utf-8")
    subject = f"{settings['site_name']}: {chosen[0]['title']}"
    result = push_buttondown(subject, md)
    print(f"issue {issue_date}: {len(chosen)} stories" + (f", Buttondown {result}" if result else ", saved locally only"))


if __name__ == "__main__":
    main()
