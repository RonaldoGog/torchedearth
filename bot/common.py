"""Small helpers shared by the bot scripts."""
import hashlib
import json
import re
import urllib.parse
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
DATA = ROOT / "data"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
SITE = ROOT / "site"

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "cmpid", "ncid", "smid", "src", "mc_cid", "mc_eid",
}


def load_yaml(name):
    with open(CFG / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def canonical(url):
    """Normalise a URL so the same article always gets the same id."""
    p = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k.lower() not in TRACKING_PARAMS]
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", p.path) or "/"
    if path.endswith("/amp"):
        path = path[:-4] or "/"
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(query), ""))


def domain_of(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def item_id(url):
    return hashlib.sha1(canonical(url).encode("utf-8")).hexdigest()[:12]


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_date(s):
    return date.fromisoformat(s[:10])


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;|&rsquo;|&lsquo;", "'", text)
    return re.sub(r"\s+", " ", text).strip()
