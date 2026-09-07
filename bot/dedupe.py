"""Group items that are the same story.

When AP publishes a story and forty papers run it, the site should show it once
and list the other outlets as "Also: ...". Items in a group share a `group` field
holding the id of the representative item (the one that gets displayed).

Two signals: title similarity within a 7-day window (catches wire reprints) and
`same_as` links written by the model's story matcher in classify.py (catches the
same story told in different words). Only the last three weeks are re-examined
each run, so old groups stay stable.

Two guards keep groups honest. A group may never span more than WINDOW_DAYS
between its earliest and latest item: one event gets covered for a few days, so
anything that would stretch a group across weeks is a different story, however
similar it sounds. And a model link is only trusted when the matcher recorded a
confidence for it (links from before the matcher was tightened carry none and
are ignored).
"""
from datetime import timedelta
from difflib import SequenceMatcher
import re
import unicodedata

from common import parse_date

# Words that say nothing about which story a headline is: grammar, news-speak, and
# the site's own subject ("climate change", "global warming" open half the headlines).
STOPWORDS = set("""a an the of to in on for and or with as by at from is are was were be has have
its it this that new says say said report reports study finds after over amid could may
than into out about us up how why what will would climate change global warming""".split())

WINDOW_DAYS = 7
LOOKBACK_DAYS = 21
THRESHOLD = 0.62
MIN_SHARED = 2      # two titles must share at least this many real words ...
MIN_OVERLAP = 0.34  # ... making up at least this fraction of all their words, before the
                    # looser word-order comparison below is even consulted


def tokens(title):
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()  # Niño -> Nino
    words = re.findall(r"[a-z0-9]+", plain.lower())
    return [w for w in words if w not in STOPWORDS and (len(w) > 2 or w.isdigit())]  # numbers count


def similarity(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    if jaccard >= THRESHOLD:
        return jaccard
    if len(sa & sb) < MIN_SHARED or jaccard < MIN_OVERLAP:
        return 0.0
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb))
    return max(jaccard, seq.ratio() if seq.quick_ratio() >= THRESHOLD else 0.0)


def _rep_key(it):
    """Higher is better: real reporting beats reprints, free beats paywalled, then source weight."""
    return (
        it.get("type") != "reprint",
        not it.get("paywall"),
        it.get("weight", 5),
        it.get("score", 0),
        it.get("published", ""),
    )


def assign_groups(items, today):
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent = sorted((i for i in items if i.get("published", "") >= cutoff), key=lambda i: i["published"])
    parent = {i["id"]: i["id"] for i in recent}
    span = {i["id"]: (parse_date(i["published"]), parse_date(i["published"])) for i in recent}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        """Merge the groups of a and b unless the result would span more than WINDOW_DAYS."""
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        lo = min(span[ra][0], span[rb][0])
        hi = max(span[ra][1], span[rb][1])
        if (hi - lo).days > WINDOW_DAYS:
            return
        parent[rb] = ra
        span[ra] = (lo, hi)

    for it in recent:
        if it.get("same_as") in parent and it.get("match_confidence"):
            union(it["same_as"], it["id"])

    for idx, a in enumerate(recent):
        da = parse_date(a["published"])
        for b in recent[idx + 1:]:
            if (parse_date(b["published"]) - da).days > WINDOW_DAYS:
                break
            if find(a["id"]) == find(b["id"]):
                continue
            if similarity(a["title"], b["title"]) >= THRESHOLD:
                union(a["id"], b["id"])

    groups = {}
    for it in recent:
        groups.setdefault(find(it["id"]), []).append(it)
    for members in groups.values():
        rep = max(members, key=_rep_key)
        for m in members:
            m["group"] = rep["id"]
    for it in items:
        it.setdefault("group", it["id"])
    return items
