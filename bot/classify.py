"""Ask a language model to sort each headline.

For every item the model returns:
  relevance 0-10   is this about climate change?  (below settings.min_relevance -> discarded)
  section          causes | effects | solutions | other
  type             technical | substantive | clickbait | reprint
  score 1-10       importance this week
  summary          one plain sentence, <= 25 words

Headlines are sent in batches of ~25, so a busy day is ~20 requests. Any of
Gemini, Claude, OpenAI or Grok works; pick with LLM_PROVIDER / LLM_API_KEY /
LLM_MODEL environment variables (or config/settings.yml).
"""
import json
import os
import re
import time

import requests

SYSTEM = """You sort news items for Torched Earth, a site that lists climate-change headlines and links to the original articles.

For each item, judging only from its headline, source and blurb, return:

- relevance: 0-10. How central is climate change (its causes, impacts, or responses) to the story? 10 = the story is about climate change; 5 = climate is one thread among several; 0 = unrelated. Weather and disaster stories count only when they connect to climate, climate-driven records or trends.
- section: one of causes, effects, solutions, other.
    causes = emissions, fossil fuels, deforestation, methane, carbon budgets, what drives warming, attribution of warming to human activity.
    effects = heat, fire, drought, floods, storms, sea level, ice, oceans, ecosystems, health, economic damage, water shortages, migration.
    solutions = clean energy, EVs, efficiency, policy and regulation, courts and litigation, finance, adaptation, carbon removal, activism.
    other = politics without a concrete policy action, culture, opinion, misinformation and denial, media coverage of climate itself, explainers.
- type: one of technical, substantive, clickbait, reprint.
    technical = reports a specific study, dataset, forecast or official report (journals, agencies, think tanks, company data releases).
    substantive = original reporting or analysis with new information.
    clickbait = provocative or vague headline on thin content; listicles; celebrity angles; questions as headlines; speculative "could/may" pieces with no new reporting.
    reprint = wire copy or a rewrite of another outlet's story or a press release (clues: "(Bloomberg) --", "(AP)", "Reuters", "according to a report by", press-release phrasing, aggregator sites).
- score: 1-10 importance for a reader trying to understand climate change this week (authority of the source, scale of what happened, novelty).
- summary: one plain sentence, at most 25 words, saying what happened. No hype, no opinion, never "this article".

Respond with a JSON array only — no prose, no code fences — one object per item, in the same order, with keys i, relevance, section, type, score, summary."""

SECTIONS = {"causes", "effects", "solutions", "other"}
TYPES = {"technical", "substantive", "clickbait", "reprint"}

OPENAI_COMPATIBLE = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
    "xai": "https://api.x.ai/v1",
}


def _config(settings):
    llm = settings.get("llm", {})
    provider = os.environ.get("LLM_PROVIDER") or llm.get("provider", "gemini")
    model = os.environ.get("LLM_MODEL") or llm.get("models", {}).get(provider)
    key = os.environ.get("LLM_API_KEY")
    return provider, model, key


def call_llm(system, user, provider, model, key):
    if provider == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 4000, "temperature": 0.2, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=180,
        )
        r.raise_for_status()
        return "".join(block.get("text", "") for block in r.json()["content"])
    base = OPENAI_COMPATIBLE[provider]
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
        json={"model": model, "temperature": 0.2,
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("no JSON array in response")
    return json.loads(text[start:end + 1])


def _prompt(batch):
    lines = []
    for n, it in enumerate(batch, 1):
        lines.append(f"{n}. [{it['source']}] {it['title']}")
        if it.get("blurb"):
            lines.append(f"   {it['blurb'][:300]}")
    return "Items:\n" + "\n".join(lines)


def _apply(it, r):
    it["relevance"] = max(0, min(10, int(r.get("relevance", 0))))
    it["section"] = r.get("section") if r.get("section") in SECTIONS else "other"
    it["type"] = r.get("type") if r.get("type") in TYPES else "substantive"
    it["score"] = max(1, min(10, int(r.get("score", 5))))
    it["summary"] = str(r.get("summary", "")).strip()[:240]
    it["status"] = "ok"


def classify(items, settings, log=print):
    """Classify every item with status 'pending' in place. Items that fail stay pending."""
    provider, model, key = _config(settings)
    pending = [i for i in items if i.get("status", "pending") == "pending"]
    if not pending:
        return 0
    if not key:
        log("LLM_API_KEY is not set: leaving %d items pending" % len(pending))
        return 0
    size = int(settings.get("llm", {}).get("batch_size", 25))
    pause = float(settings.get("llm", {}).get("pause_seconds", 7))
    done = 0
    for start in range(0, len(pending), size):
        batch = pending[start:start + size]
        for attempt in range(3):
            try:
                results = _parse(call_llm(SYSTEM, _prompt(batch), provider, model, key))
                by_i = {int(r["i"]): r for r in results if "i" in r}
                for n, it in enumerate(batch, 1):
                    if n in by_i:
                        _apply(it, by_i[n])
                        done += 1
                break
            except Exception as e:  # rate limit, bad JSON, network
                wait = 20 * (attempt + 1)
                log(f"batch {start // size + 1}: {e.__class__.__name__}: {e} — retrying in {wait}s")
                time.sleep(wait)
        time.sleep(pause)
    log(f"classified {done} of {len(pending)} with {provider}/{model}")
    return done


# ---- Story matching -------------------------------------------------------
# Title similarity catches wire reprints; it cannot tell that "World's oceans hit
# hottest temperature ever recorded" and "Copernicus: daily sea surface
# temperature breaks record" are the same story. The model can. Once a day,
# today's new stories are compared against the last week's.

MATCH_SYSTEM = """You match news items that report the same event, announcement, study, ruling or data release.
Two items are the same story when they are about the same thing that happened — many outlets covering one lawsuit, one report, one record — even from different angles or with different emphasis. Follow-ups with genuinely new developments, and different events on the same topic, are different stories.

You will get RECENT items (labels like R3) and NEW items (labels like N7). For each NEW item that is the same story as a RECENT item or as an earlier NEW item, output one object. Omit NEW items that match nothing.
Respond with a JSON array only — no prose, no code fences — of objects with keys "item" (the NEW label) and "same_as" (the label it matches; prefer the RECENT label if one exists, otherwise the lowest-numbered NEW label)."""


def _line(label, it):
    return f"{label}. [{it['source']}] {it['title']} — {it.get('summary', '')}"


def match_stories(new_items, recent_items, settings, log=print):
    """Set it['same_as'] = id of an earlier item that reports the same story."""
    provider, model, key = _config(settings)
    if not key or not new_items:
        return 0
    size = 60
    matched = 0
    for start in range(0, len(new_items), size):
        batch = new_items[start:start + size]
        recent_lines = "\n".join(_line(f"R{n}", it) for n, it in enumerate(recent_items, 1)) or "(none)"
        new_lines = "\n".join(_line(f"N{n}", it) for n, it in enumerate(batch, 1))
        user = f"RECENT items:\n{recent_lines}\n\nNEW items:\n{new_lines}"
        try:
            results = _parse(call_llm(MATCH_SYSTEM, user, provider, model, key))
        except Exception as e:
            log(f"story matching failed: {e.__class__.__name__}: {e}")
            continue
        for r in results:
            try:
                item = batch[int(str(r["item"]).strip("Nn")) - 1]
                label = str(r["same_as"]).strip().upper()
                target = recent_items[int(label[1:]) - 1] if label.startswith("R") else batch[int(label[1:]) - 1]
            except (KeyError, ValueError, IndexError):
                continue
            if target is not item:
                item["same_as"] = target["id"]
                matched += 1
        time.sleep(float(settings.get("llm", {}).get("pause_seconds", 7)))
    log(f"story matching: {matched} of {len(new_items)} new items match an earlier story")
    return matched
