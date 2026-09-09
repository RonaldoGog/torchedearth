#!/usr/bin/env python3
"""Render the site from data/items.json into site/.

    python bot/build.py                    # today's date
    python bot/build.py --today 2026-09-05 # pretend it's another day (testing)

Rules (all from config/settings.yml):
  front page  = the lead (the best of today's stories), then stories from the
                last `recent_days` days
                (today and yesterday) scoring >= `recent_min_score`, at most
                `recent_max_per_day` a day, listed most important first under
                their date, not yet sorted into sections; then older stories
                from the last `homepage_days`, capped at `homepage_max`, grouped
                into Effects / Solutions / Causes columns with an "Elsewhere"
                strip for `other`. Only the types in `homepage_types` scoring
                >= `min_score_homepage` reach the front page at all.
  section page = everything else in that section, newest first, by month
  archive      = every story, with a search box
Nothing is ever "moved": a story's page is a function of its date, section and type.
"""
import html
import shutil
import sys
from collections import defaultdict
from datetime import date, timedelta
from email.utils import format_datetime
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import DATA, SITE, STATIC, TEMPLATES, canonical, load_json, load_yaml, parse_date

SECTION_INTRO = {
    "causes": "What is driving warming: emissions, fossil fuels, land use, the science that ties them to rising temperatures — and the steps backward: targets scrapped, rules rolled back, protections cut, and the disinformation and corruption behind them.",
    "effects": "What warming is doing: heat, fire, water, storms, ice, oceans, ecosystems, health and money.",
    "solutions": "What is being done: clean energy, policy, courts, finance, adaptation — progress, not promises. Setbacks are filed under Causes.",
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


def local_now(settings):
    """The moment of this build, in the site's own time zone."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings.get("timezone", "UTC")))
    except Exception:
        return datetime.now(timezone.utc)


def local_today(settings):
    """Today's date in the site's own time zone, so 'Today' means today for the readers."""
    return local_now(settings).date()


def updated_label(now):
    """'Monday, September 8, 2026 at 6:15 a.m. PDT' — when the pages were last rebuilt.

    Written out by hand rather than with strftime: the 12-hour and no-leading-zero
    codes differ between Windows and Linux, and this runs on both.
    """
    hour = now.hour % 12 or 12
    ampm = "a.m." if now.hour < 12 else "p.m."
    zone = now.strftime("%Z") or "UTC"
    return f"{now:%A, %B} {now.day}, {now.year} at {hour}:{now:%M} {ampm} {zone}"


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
    # Rank: pinned stories first, then importance, then how far the outlet is
    # trusted (`weight` in feeds.yml; 5 for anything not listed there), then
    # recency. Weight is what keeps a scraped rewrite from outranking the Guardian
    # when the classifier hands them the same score.
    by_rank = lambda r: (r["featured"], r.get("score", 0), r.get("weight", 5), r["published"])

    # The newest stories (today and yesterday) go up top, listed by day, most
    # important first. The bar is higher there (`recent_min_score`, with
    # `recent_max_per_day` as a ceiling); stories under the bar skip the front page
    # and appear on their section page the same day. Older stories compete for the
    # capped, sectioned part of the page below.
    recent_from = (today - timedelta(days=int(settings.get("recent_days", 2)) - 1)).isoformat()
    recent_min = int(settings.get("recent_min_score", settings["min_score_homepage"]))
    per_day = int(settings.get("recent_max_per_day", 0)) or None
    by_day = defaultdict(list)
    for r in eligible:
        if r["published"] >= recent_from and (r["featured"] or r.get("score", 0) >= recent_min):
            by_day[r["published"]].append(r)
    recent = []
    for iso in by_day:
        by_day[iso] = sorted(by_day[iso], key=by_rank, reverse=True)[:per_day]
        recent += by_day[iso]
    older = sorted((r for r in eligible if r["published"] < recent_from), key=by_rank, reverse=True)
    older = older[: int(settings["homepage_max"])]
    front = recent + older

    # The lead is the best of today's stories, not the best of the last four weeks:
    # a site that updates every morning should not open on a four-day-old headline.
    # Yesterday's stand in if today has none, the whole front page only if neither
    # day does. A story still linking through Google rather than to the publisher
    # is never the lead — the one headline every reader clicks has to land somewhere
    # real. `overrides.yml lead:` overrules all of this.
    def can_lead(r):
        return "news.google.com" not in r["url"]

    lead = next((r for r in front if lead_url and canonical(r["url"]) == lead_url), None)
    if lead is None:
        for iso in sorted(by_day, reverse=True):          # today, then yesterday
            pool = [r for r in by_day[iso] if can_lead(r)]
            if pool:
                lead = max(pool, key=by_rank)
                break
        else:
            pool = [r for r in front if can_lead(r)] or front
            lead = max(pool, key=by_rank) if pool else None

    # by_day[iso] is already in `by_rank` order (featured, then score, then date),
    # so the day lists read most important first.
    days = [{"label": day_label(iso, today),
             "stories": [r for r in by_day[iso] if r is not lead]}
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


def rss(front, settings, now):
    def esc(s):
        return html.escape(s or "", quote=True)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
           f"<title>{esc(settings['site_name'])}</title>",
           f"<link>{esc(settings['base_url'])}/</link>",
           f"<description>{esc(settings['tagline'])}</description>",
           f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>"]
    for r in front[:50]:
        pub = format_datetime(datetime.combine(parse_date(r["published"]), datetime.min.time(), tzinfo=timezone.utc))
        out.append(f"<item><title>{esc(r['title'])}</title><link>{esc(r['url'])}</link>"
                   f"<guid isPermaLink=\"false\">{esc(r['id'])}</guid><pubDate>{pub}</pubDate>"
                   f"<description>{esc((r.get('summary') or '') + ' (' + r['source'] + ')')}</description></item>")
    out.append("</channel></rss>")
    return "\n".join(out)


def main(today=None):
    settings = load_yaml("settings.yml")
    now = local_now(settings)
    today = today or now.date()
    feeds = load_yaml("feeds.yml")["feeds"]
    overrides = load_yaml("overrides.yml")
    items = load_json(DATA / "items.json", [])
    status = load_json(DATA / "feed_status.json", {})
    ctx = prepare(items, settings, feeds, overrides, today)

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    base = {"site": settings, "css": css, "base": "/", "stats_sentence": stats_sentence(ctx["stats"]),
            "today": f"{today:%A, %B} {today.day}, {today.year}", "feed_status": status,
            "updated": updated_label(now), "updated_iso": now.isoformat(timespec="minutes")}

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

    (SITE / "feed.xml").write_text(rss(ctx["front"], settings, now), encoding="utf-8")
    (SITE / "CNAME").write_text(settings["domain"] + "\n", encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    recent = sum(len(d["stories"]) for d in ctx["days"])
    print(f"built site/: front page {len(ctx['front'])} stories ({recent} in the day lists), archive {len(ctx['archive'])}, lead: {ctx['lead']['title'] if ctx['lead'] else '—'}")


if __name__ == "__main__":
    t = None
    if "--today" in sys.argv:
        t = date.fromisoformat(sys.argv[sys.argv.index("--today") + 1])
    main(t)
