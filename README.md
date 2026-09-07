# Torched Earth

A site that proves climate change is in the news every day: a plain list of the
day's climate headlines, each linking to the original article, sorted into
Causes / Effects / Solutions, with a searchable archive, a weekly email, and
social posts — all updated automatically every morning.

Nothing here needs a server. The site is a folder of web pages rebuilt daily by a
small program that runs on GitHub's free schedule. You edit three text files to
steer it; everything else is automatic.

## What happens every morning

1. **Gather.** The bot reads ~20 RSS feeds (`config/feeds.yml`) and runs ~35
   Google News searches (`config/queries.yml`). Typically 300–600 headlines.
2. **Keep only the new ones.** Anything seen before is skipped.
3. **Sort.** A language model reads each new headline and blurb and decides:
   is it about climate (0–10)? which section? what type (study, original
   reporting, reprint, clickbait)? how important (1–10)? and writes a
   one-sentence summary. Twenty-five headlines per request.
4. **Match.** The model also says which of today's stories are the same story
   as each other or as last week's, so one event covered by twelve outlets
   appears once with the others under "Also:".
5. **Publish.** The site is rebuilt and pushed live. Front page = last four
   weeks; section pages = everything older; archive = everything, searchable.
   Nothing is ever moved by hand — a story's page follows from its date,
   section and type.
6. **Share.** The top five new stories go to Bluesky and Mastodon.
7. **Sundays:** the week's top ten become the newsletter, saved to the site
   and pushed to the email provider.

## What you'd need to set up (about an hour, no coding)

1. **A GitHub account** (free). Put this folder in a new repository named
   `torchedearth`. In the repository's Settings → Pages, set Source to
   "GitHub Actions". Settings → Actions → General: allow workflows to read and
   write.
2. **An AI key.** Cheapest: Google AI Studio (aistudio.google.com) → Create API
   key. Its free tier covers this bot's ~20 requests a day. In the repository:
   Settings → Secrets and variables → Actions → New repository secret
   `LLM_API_KEY`. (Optional variables `LLM_PROVIDER` = gemini | anthropic |
   openai | xai and `LLM_MODEL` to switch providers; defaults are in
   `config/settings.yml`.)
3. **Your domain.** At the registrar where you own torchedearth.org, add DNS
   records pointing to GitHub Pages (GitHub's "custom domain" help page lists
   the four A records and the `www` CNAME). Then Settings → Pages → Custom
   domain → `torchedearth.org`, tick "Enforce HTTPS".
4. **Run it once by hand.** Actions tab → "Update site" → Run workflow. Ten
   minutes later the site is live. It then runs itself every morning.
5. **Newsletter (any time later).** Sign up with an email provider, put your
   signup page's address in `config/settings.yml` → `newsletter_url` (or paste
   their embed form into `newsletter_embed`). For automatic Sunday issues add
   the secret `BUTTONDOWN_API_KEY`; issues are saved as drafts for a look before
   sending until you set the variable `NEWSLETTER_SEND` to `true`.
6. **Social (any time later).** Bluesky: Settings → App passwords → add
   secrets `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`. Mastodon: Preferences →
   Development → new application (write:statuses) → secrets `MASTODON_URL` and
   `MASTODON_TOKEN`. X is not wired in because X now charges per post with a
   link; it can be added later if the cost is worth it.

## Steering it

All in `config/`; edit on GitHub with the pencil icon, save, and the next run
uses it (or run the workflow by hand).

- `feeds.yml` — the sources. Add a line, get a source. Remove a line, lose it.
  Each has a `weight` (1–10) that decides ranking and which outlet represents a
  story many outlets covered, and a `paywall` flag that puts a $ next to it.
- `queries.yml` — the Google News searches.
- `overrides.yml` — pin a lead story, force a story onto the front page, or hide
  one, by pasting its URL.
- `settings.yml` — how long stories stay on the front page (28 days), the front
  page cap (150), the relevance and importance thresholds, section names, the
  model, the newsletter link.

## Files

    config/        the three files above plus settings.yml
    bot/           the program: fetch.py, classify.py, dedupe.py, pipeline.py,
                   build.py, newsletter.py, social.py, common.py
    templates/     the page layouts (Jinja2 HTML)
    static/        style.css (inlined into every page)
    data/          the database: items.json (every story), dropped.json,
                   feed_status.json, url_cache.json, posted.json, newsletters/
    site/          the built website (regenerated each run; not committed)
    .github/workflows/site.yml   the daily schedule

`data/items.json` is the whole archive — a plain text file you can back up,
search, or open in a spreadsheet. If the site ever needs to move, that file and
this folder are everything.

## Costs

- Hosting: $0 (GitHub Pages). Domain: already owned.
- AI: $0 on the Gemini free tier; a few dollars a month with Claude, OpenAI or
  Grok's small models.
- Newsletter: $0 to 100 subscribers on Buttondown, then about $9/month; beehiiv
  is free to 2,500 subscribers and Kit to 10,000 (check current terms).
- Social: $0 on Bluesky and Mastodon.

## Running it on your own computer (optional)

    pip install -r requirements.txt
    python bot/pipeline.py --dry-run      # fetch and list what's new, change nothing
    LLM_API_KEY=... python bot/pipeline.py
    python bot/build.py                   # writes site/ — open site/index.html
    python bot/newsletter.py --today 2026-09-06

## About the demo data

`data/items.json` ships with 36 real headlines from April–September 2026,
collected and sorted by hand to show the layout. Dates were taken from the
articles where shown and approximated where not. Delete the file before the
first real run, or leave it: the bot only adds to it.

## Things to know

- The bot never copies article text. Headline + one-sentence summary + link is
  the whole of what appears, the same as Drudge, Techmeme and Google News.
- It doesn't get around paywalls and shouldn't. Paywalled outlets are marked
  $, and free coverage of the same story is listed first under "Also".
- The model will sometimes misfile a story. It's still on the site, just in the
  wrong column; `overrides.yml` fixes anything that matters. Two weeks of
  watching the output is the right way to tune the feed list and thresholds.
- Feeds die. `data/feed_status.json` records what each feed returned on the
  last run, and the Sources page flags feeds that were unreachable.
- Google's news links are opaque tokens; the bot asks Google for the real
  publisher URL (two quick requests per link, answers cached in
  `data/url_cache.json`) so duplicates are recognised, outlets get their
  weight and $ flag, and readers land on the publisher. If Google changes the
  recipe, stories from searches still appear with links that bounce through
  Google, `feed_status.json` shows "0 links unwrapped", and the open-source
  googlenewsdecoder project (github.com/SSujitX/google-news-url-decoder) is
  where to look for the new recipe. Links saved while unwrapping was broken are
  repaired a batch at a time on later runs (`repair_per_run` in settings.yml).
- Two stories are grouped under one headline only when the bot is confident
  they report the same event: near-identical titles within a week, or a model
  match with confidence of at least `match_min_confidence`. A group can never
  span more than a week. If a story is wrongly hidden under another's "Also",
  or wrongly shown twice, those two settings are the knobs.
