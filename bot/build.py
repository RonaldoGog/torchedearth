#!/usr/bin/env python3
"""Render the site from data/items.json into site/.

    python bot/build.py                    # today's date
    python bot/build.py --today 2026-09-05 # pretend it's another day (testing)

Rules (all from config/settings.yml):
  front page  = the lead, then every story from the last `recent_days` days
                (today and yesterday) listed alphabetically under its date, not
                yet sorted into sections; then older stories from the last
                `homepage_days`, capped at `homepage_max`, grouped into
                Causes / Effects / Solutions columns with an "Elsewhere" strip
                for `other`. Only the types in `homepage_types` scoring
                >= `min_score_homepage` reach the front page.
  section page = everything else in that section, newest first, by month
  archive      = every story, with a search box
Nothing is ever "moved": a story's page is a function of its date, section and type.
"""
import html
import re
import shutil
import sys
from collections import defaultdict
from datetime import date, timedelta
from email.utils import format_datetime
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import DATA, SITE, STATIC, TEMPLATES, canonical, load_json, load_yaml, parse_date

SECTION_INTRO = {
    "causes": "What is driving warming: emissions, fossil fuels, land use, the science that ties them to rising temperatures, and the disinformation and corruption that keep it going.",
    "effects": "What warming is doing: heat, fire, water, storms, ice, oceans, ecosystems, health and money.",
    "solutions": "What is being done: clean energy, policy, courts, finance, adaptation and the fights over all of it.",
    "other": "Opinion, politics, culture and the coverage of climate itself. Every op-ed, editorial and column lands here, whatever its subject — argument is not news.",
}


def date_label(iso, today):
    d = parse_date(iso)
    return f"{d:%b} {d.day}" if d.year == today.year else f"{d:%b} {d.day}, {d.year}"


def month_label(iso):
    return parse_date(iso).strftime("%B %Y")


def day_label(iso, today):
    """'Today · Monday, September 8' for the front page's day-by-day blocks."""
    d = parse_date(iso)
    full = f"{d:%A, %B} {d.day}" if d.year == today.year else f"{d:%A, %B} {d.day}, {d.year}"
    if d == today:
        return f"Today · {full}"
    if d == today - timedelta(days=1):
        return f"Yesterday · {full}"
    return full


def alpha_key(it):
    """Sort key for an alphabetical headline list: ignore case and leading quotes or punctuation."""
    return re.sub(r"^[^0-9a-z]+", "", it["title"].lower())


def local_today(settings):
    """Today's date in the site's own time zone, so 'Today' means today for the readers."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings.get("timezone", "UTC"))).date()
    except Exception:
        return date.today()


def prepare(items, settings, feeds, overrides, today):
    hide = {canonical(u) for u in (overrides.get("hide") or [])}
    feature = {canonical(u) for u in (overrides.get("feature") or [])}
    lead_url = canonical(overrides["lead"]) if overrides.get("lead") else None
    types = settings["types"]

    visible = [i for i in items if i.get("status") == "ok" and canonical(i["url"]) not in hide]

    # config/overrides.yml can move an individual story between sections.
    moved = {canonical(u): key
             for key, urls in (overrides.get("section") or {}).items() if key in settings["sections"]
             for u in (urls or [])}
    if moved:
        for it in visible:
            it["section"] = moved.get(canonical(it["url"]), it.get("section", "other"))

    by_group = defaultdict(list)
    for it in visible:
        by_group[it.get("group") or it["id"]].append(it)

    reps = []
    for gid, members in by_group.items():
        rep = next((m for m in members if m["id"] == gid), None) or max(members, key=lambda m: m.get("weight", 5))
        others = sorted((m for m in members if m is not rep), key=lambda m: (m.get("paywall", False), -m.get("weight", 5)))
        rep["also"] = [{"source": m["source"], "url": m["url"], "paywall": m.get("paywall", False)} for m in others]
        rep["type_label"] = types.get(rep.get("type", "substantive"), "")
        rep["date_label"] = date_label(rep["published"], today)
        rep["featured"] = canonical(rep["url"]) in feature
        reps.append(rep)
    for it in visible:
        it.setdefault("type_label", types.get(it.get("type", "substantive"), ""))
        it.setdefault("date_label", date_label(it["published"], today))

    cutoff = (today - timedelta(days=int(settings["homepage_days"]))).isoformat()
    eligible = [r for r in reps if r["featured"] or (
        r["published"] >= cutoff
        and r.get("type") in settings["homepage_types"]
        and r.get("score", 0) >= settings["min_score_homepage"]
    )]
    by_rank = lambda r: (r["featured"], r.get("score", 0), r["published"])

    # The newest stories (today and yesterday) all go up top, listed by day;
    # older ones compete for the capped, sectioned part of the page below.
    recent_from = (today - timedelta(days=int(settings.get("recent_days", 2)) - 1)).isoformat()
    recent = [r for r in eligible if r["published"] >= recent_from]
    older = sorted((r for r in eligible if r["published"] < recent_from), key=by_rank, reverse=True)
    older = older[: int(settings["homepage_max"])]
    front = recent + older

    lead = next((r for r in front if lead_url and canonical(r["url"]) == lead_url), None) or (max(front, key=by_rank) if front else None)

    by_day = defaultdict(list)
    for r in recent:
        if r is not lead:
            by_day[r["published"]].append(r)
    days = [{"label": day_label(iso, today), "stories": sorted(by_day[iso], key=alpha_key)}
            for iso in sorted(by_day, reverse=True)]

    columns = defaultdict(list)
    for r in older:
        if r is not lead:
            columns[r.get("section", "other")].append(r)
    for col in columns.values():
        col.sort(key=lambda r: (r["published"], r.get("score", 0)), reverse=True)

    on_front = {id(r) for r in front}
    sections = {}
    for key in settings["sections"]:
        rows = sorted((r for r in reps if r.get("section", "other") == key and id(r) not in on_front),
                      key=lambda r: (r["published"], r.get("score", 0)), reverse=True)
        months = []
        for r in rows:
            label = month_label(r["published"])
            if not months or months[-1][0] != label:
                months.append((label, []))
            months[-1][1].append(r)
        sections[key] = months

    archive = sorted(visible, key=lambda r: (r["published"], r.get("score", 0)), reverse=True)

    week_ago = (today - timedelta(days=7)).isoformat()
    stats = {
        "today": sum(1 for r in reps if r.get("found") == today.isoformat()),
        "week": sum(1 for r in reps if r.get("found", "") >= week_ago),
        "total": len(reps),
        "since": parse_date(min(r["published"] for r in reps)).strftime("%B %Y") if reps else "",
    }

    counts = defaultdict(int)
    for it in visible:
        counts[it.get("source_id")] += 1
    sources = []
    for f in feeds:
        sources.append({**f, "count": counts.get(f["id"], 0)})
    other_sources = sorted(
        ({"name": it["source"], "id": it["source_id"], "count": counts[it["source_id"]]}
         for it in {i["source_id"]: i for i in visible}.values()
         if it["source_id"] not in {f["id"] for f in feeds}),
        key=lambda s: -s["count"])

    return {"lead": lead, "days": days, "columns": columns, "sections": sections, "archive": archive,
            "stats": stats, "sources": sources, "other_sources": other_sources, "front": front}


def stats_sentence(stats):
    if not stats["total"]:
        return "The bot hasn't run yet."
    parts = []
    if stats["today"]:
        parts.append(f"{stats['today']} new since yesterday")
    parts.append(f"{stats['week']} this week" if stats["week"] else "")
    parts = [p for p in parts if p]
    lead_in = ", ".join(parts) + ". " if parts else ""
    return f"{lead_in}{stats['total']:,} stories in the archive since {stats['since']}."


def rss(front, settings, today):
    def esc(s):
        return html.escape(s or "", quote=True)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
           f"<title>{esc(settings['site_name'])}</title>",
           f"<link>{esc(settings['base_url'])}/</link>",
           f"<description>{esc(settings['tagline'])}</description>"]
    for r in front[:50]:
        pub = format_datetime(datetime.combine(parse_date(r["published"]), datetime.min.time(), tzinfo=timezone.utc))
        out.append(f"<item><title>{esc(r['title'])}</title><link>{esc(r['url'])}</link>"
                   f"<guid isPermaLink=\"false\">{esc(r['id'])}</guid><pubDate>{pub}</pubDate>"
                   f"<description>{esc((r.get('summary') or '') + ' (' + r['source'] + ')')}</description></item>")
    out.append("</channel></rss>")
    return "\n".join(out)


def main(today=None):
    settings = load_yaml("settings.yml")
    today = today or local_today(settings)
    feeds = load_yaml("feeds.yml")["feeds"]
    overrides = load_yaml("overrides.yml")
    items = load_json(DATA / "items.json", [])
    status = load_json(DATA / "feed_status.json", {})
    ctx = prepare(items, settings, feeds, overrides, today)

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    base = {"site": settings, "css": css, "base": "/", "stats_sentence": stats_sentence(ctx["stats"]),
            "today": f"{today:%A, %B} {today.day}, {today.year}", "feed_status": status}

    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)

    def write(path, template, **kw):
        page = env.get_template(template).render(**base, **kw)
        target = SITE / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")

    write("index.html", "index.html", current="home", page_title=settings["site_name"],
          description=settings["tagline"], lead=ctx["lead"], days=ctx["days"], columns=ctx["columns"])
    for key, label in settings["sections"].items():
        write(f"{key}/index.html", "section.html", current=key, page_title=label,
              description=SECTION_INTRO[key], intro=SECTION_INTRO[key], months=ctx["sections"][key],
              section_key=key)
    write("archive/index.html", "archive.html", current="archive", page_title="Archive",
          description="Every climate story the site has listed, searchable.", rows=ctx["archive"])
    write("sources/index.html", "sources.html", current="sources", page_title="Sources",
          description="The outlets and searches the bot reads.", sources=ctx["sources"],
          other_sources=ctx["other_sources"])
    write("about/index.html", "about.html", current="about", page_title="About",
          description="Why this site exists and how it works.")

    issues = sorted((p for p in (DATA / "newsletters").glob("*.html")), reverse=True)
    for p in issues:
        (SITE / "weekly").mkdir(exist_ok=True)
        shutil.copy(p, SITE / "weekly" / p.name)
    write("weekly/index.html", "weekly.html", current="weekly", page_title="Weekly",
          description="The weekly top-ten newsletter.", issues=[p.stem for p in issues])

    (SITE / "feed.xml").write_text(rss(ctx["front"], settings, today), encoding="utf-8")
    (SITE / "CNAME").write_text(settings["domain"] + "\n", encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    recent = sum(len(d["stories"]) for d in ctx["days"])
    print(f"built site/: front page {len(ctx['front'])} stories ({recent} in the day lists), archive {len(ctx['archive'])}, lead: {ctx['lead']['title'] if ctx['lead'] else '—'}")


if __name__ == "__main__":
    t = None
    if "--today" in sys.argv:
        t = date.fromisoformat(sys.argv[sys.argv.index("--today") + 1])
    main(t)
