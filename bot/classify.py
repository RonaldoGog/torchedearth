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

from dedupe import tokens

SYSTEM = """You sort news items for Torched Earth, a site that lists climate-change headlines and links to the original articles.

For each item, judging only from its headline, source and blurb, return:

- relevance: 0-10. How central is climate change (its causes, impacts, or responses) to the story? 10 = the story is about climate change; 5 = climate is one thread among several; 0 = unrelated. Weather and disaster stories count only when they connect to climate, climate-driven records or trends.
- section: one of causes, effects, solutions, other.
    Apply these two rules first; they override the topic definitions below.
    1. OPINION ALWAYS GOES IN other, whatever its subject. An item is opinion when its purpose is to argue a position or press a judgement rather than to report what happened: op-eds, editorials, columns, letters to the editor, commentary and guest posts, personal essays, advocacy pieces, "Why we must…", "It's time to…", "The case for…", a rhetorical-question headline that then argues, a named columnist's take. Straight news reports, studies, data releases, interviews, profiles and descriptive explainers are NOT opinion.
    2. DISINFORMATION IS A CAUSE. Climate misinformation, disinformation and denial, fossil-fuel PR and influence campaigns, greenwashing, industry-funded advocacy, corruption, and the censoring or suppression of climate science and climate education (removing climate content from schoolbooks or agency sites, firing or muzzling climate scientists, killing climate research) all go in causes — unless the item is itself opinion, in which case rule 1 wins and it goes in other.
    3. STEPS BACKWARD ARE CAUSES, NOT SOLUTIONS. Judge a policy, legal, business or diplomatic story by its direction of travel. If the event moves the world toward more emissions or less protection — a climate target scrapped or weakened, a regulation rolled back or a protection cut, a subsidy for emitters restored, a clean-energy incentive killed, a climate law struck down or an accountability effort defeated, a negotiation that stalls or goes backward, an industry lobbying to weaken a rule — it goes in causes, even though it is "about" climate policy. Only progress goes in solutions: an emissions cut, a plant closed, a target adopted, a rule tightened, a lawsuit won, a technology deployed, a deal reached, adaptation built. A fight still in progress goes with the side the story is about: an effort to cut emissions is solutions; an effort to block or weaken one is causes.
    causes = emissions, fossil fuels, deforestation, methane, carbon budgets, what drives warming, attribution of warming to human activity, the disinformation and corruption in rule 2, and the setbacks in rule 3.
    effects = heat, fire, drought, floods, storms, sea level, ice, oceans, ecosystems, health, economic damage, water shortages, migration.
    solutions = clean energy, EVs, efficiency, policy and regulation, courts and litigation, finance, adaptation, carbon removal, activism — when the story is about progress being made (rule 3).
    other = opinion of every kind (rule 1), politics without a concrete policy action, culture, media coverage of climate itself, explainers.
- type: one of technical, substantive, clickbait, reprint.
    technical = reports a specific study, dataset, forecast or official report (journals, agencies, think tanks, company data releases).
    substantive = original reporting or analysis with new information.
    clickbait = provocative or vague headline on thin content; listicles; celebrity angles; questions as headlines; speculative "could/may" pieces with no new reporting.
    reprint = wire copy or a rewrite of another outlet's story or a press release (clues: a non-wire outlet running copy credited "(Bloomberg) --", "(AP)" or "Reuters"; "according to a report by"; press-release phrasing; aggregator sites). A story published by Reuters, AP or Bloomberg themselves is their own reporting, not a reprint.
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
    if not it.get("section_locked"):   # imported stories keep the section chosen by hand
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
# today's new stories are compared against the last few days'.
#
# The matcher used to be far too generous: asked to compare sixty new items against
# several hundred recent ones, a small model linked anything on the same topic, and
# every El Niño story for three weeks ended up as one headline with 57 "Also" links.
# Three things keep it honest now: the prompt spells out that same topic is not the
# same story; the model reports a confidence and only confident links are kept; and
# only recent items that share some vocabulary with the new batch are shown to it.

MATCH_SYSTEM = """You match news items that report the same story: the same single event, announcement, study, ruling, data release or record. "Same story" means a reader who had seen one item would learn nothing new from the headline of the other, apart from the outlet's angle or emphasis.

Be strict. These are NOT the same story:
- two items on the same subject (El Niño, wildfires, Nepal's floods, the 1.5°C report) reporting different events, findings, places or days;
- a study and a news event on the same topic;
- a follow-up with a new development (a death toll rising, a lawsuit filed, a new forecast);
- an explainer, opinion piece or overview and a news report on its subject.

These ARE the same story: many outlets covering one lawsuit, one report, one record, one disaster on the same day; a wire story and its reprints; a press release and the coverage of it.

You will get RECENT items (labels like R3) and NEW items (labels like N7). For each NEW item that is the same story as a RECENT item or as an earlier NEW item, output one object with keys "item" (the NEW label), "same_as" (the matching label; prefer the RECENT label if one exists, otherwise the lowest-numbered NEW label) and "confidence" (1-10: 10 = certainly the same event; 5 = same topic, probably a different event). When in doubt, leave it out — an unmatched duplicate costs little, a wrong match hides a story.
Respond with a JSON array only — no prose, no code fences. Output [] if nothing matches."""

def _words(it):
    """Content words of an item's headline and summary (same tokenizer as dedupe.py)."""
    return set(tokens(f"{it['title']} {it.get('summary', '')}"))


def _line(label, it):
    return f"{label}. [{it['source']}] {it['title']} — {it.get('summary', '')}"


def match_stories(new_items, recent_items, settings, log=print):
    """Set it['same_as'] (and it['match_confidence']) = an earlier item that reports the same story."""
    provider, model, key = _config(settings)
    if not key or not new_items:
        return 0
    llm = settings.get("llm", {})
    min_conf = int(llm.get("match_min_confidence", 8))
    size = 60
    matched = 0
    for start in range(0, len(new_items), size):
        batch = new_items[start:start + size]
        # Only show the model recent items that share at least two content words with something new.
        batch_words = [_words(it) for it in batch]
        candidates = [r for r in recent_items if any(len(_words(r) & w) >= 2 for w in batch_words)]
        recent_lines = "\n".join(_line(f"R{n}", it) for n, it in enumerate(candidates, 1)) or "(none)"
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
                target = candidates[int(label[1:]) - 1] if label.startswith("R") else batch[int(label[1:]) - 1]
                conf = int(r.get("confidence", 0))
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if target is item or conf < min_conf:
                continue
            if not (_words(item) & _words(target)):  # two items with no words in common are never one story
                continue
            item["same_as"] = target["id"]
            item["match_confidence"] = conf
            matched += 1
        time.sleep(float(llm.get("pause_seconds", 7)))
    log(f"story matching: {matched} of {len(new_items)} new items match an earlier story")
    return matched
