#!/usr/bin/env python3
"""Daily viral tech/science news digest -> Telegram."""

import os
import re
import html
import time
import json
import urllib.parse
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

import requests
import feedparser

# ---------------- config ----------------

WINDOW_HOURS = 48
QUOTA = {"ai": 2, "health": 1, "quantum": 1, "tech": 1}

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

UA = "Mozilla/5.0 (compatible; news-digest-bot/1.0)"

SUBREDDITS = {
    "ai": ["artificial", "singularity", "MachineLearning", "LocalLLaMA"],
    "health": ["medicine", "science", "biotech"],
    "quantum": ["QuantumComputing", "quantum"],
    "tech": ["technology", "programming", "hardware"],
}

RSS_FEEDS = {
    "ai": [
        "https://news.mit.edu/rss/topic/artificial-intelligence2",
        "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    ],
    "health": [
        "https://www.nature.com/nm.rss",
        "https://www.statnews.com/feed/",
        "https://www.sciencedaily.com/rss/health_medicine.xml",
    ],
    "quantum": [
        "https://phys.org/rss-feed/physics-news/quantum-physics/",
        "https://quantumcomputingreport.com/feed/",
    ],
    "tech": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
    ],
}

# HN Algolia queries per topic
HN_QUERIES = {
    "ai": ["AI", "LLM", "machine learning", "neural network", "OpenAI", "Anthropic"],
    "health": ["medicine", "clinical trial", "FDA", "cancer", "vaccine", "biotech"],
    "quantum": ["quantum computing", "qubit", "quantum"],
    "tech": ["chip", "semiconductor", "software", "security", "hardware"],
}

# ---------------- quality filters ----------------

BLOCKED_DOMAINS = {
    "medium.com", "substack.com", "linkedin.com", "twitter.com", "x.com",
    "facebook.com", "youtube.com", "tiktok.com", "prnewswire.com",
    "businesswire.com", "globenewswire.com", "einpresswire.com",
    "benzinga.com", "zacks.com", "marketbeat.com", "investorplace.com",
    "msn.com", "yahoo.com", "dailymail.co.uk", "the-sun.com", "nypost.com",
    "buzzfeed.com", "vice.com", "futurism.com", "interestingengineering.com",
    "analyticsindiamag.com", "cointelegraph.com", "coindesk.com",
}

TRUSTED_DOMAINS = {
    "nature.com", "science.org", "nejm.org", "thelancet.com", "bmj.com",
    "jamanetwork.com", "arxiv.org", "pubmed.ncbi.nlm.nih.gov", "nih.gov",
    "cdc.gov", "who.int", "ema.europa.eu", "fda.gov",
    "arstechnica.com", "technologyreview.com", "statnews.com", "ieee.org",
    "phys.org", "quantamagazine.org", "theverge.com", "wired.com",
    "reuters.com", "apnews.com", "bloomberg.com", "ft.com", "economist.com",
    "mit.edu", "stanford.edu", "berkeley.edu", "nasa.gov", "esa.int",
}

TITLE_REJECT = re.compile(
    r"^\s*\d+\s+(best|top|ways|things|reasons|tips)\b"
    r"|\b(top|best)\s+\d+\b"
    r"|\byou won'?t believe\b"
    r"|\bhere'?s (why|how|what)\b.*\?$"
    r"|\bshould you buy\b"
    r"|\bdeal[s]?\b.*\b(save|off|discount)\b"
    r"|\bstock[s]? to (buy|watch)\b"
    r"|\bcould (destroy|kill|end)\b"
    r"|\bis (dead|over)\b\s*$"
    r"|\bwill (destroy|kill|replace) (us|humanity|everything)\b"
    r"|\brumou?r(ed)?\b"
    r"|\bleak(ed|s)?\b.*\b(render|spec)"
    r"|\bgiveaway\b|\bcoupon\b|\bsponsored\b",
    re.IGNORECASE,
)

MIN_TITLE_WORDS = 4
MIN_HN_POINTS = 40
MIN_REDDIT_SCORE = 200

# ---------------- model ----------------

@dataclass
class Item:
    title: str
    url: str
    topic: str
    source: str
    score: float
    published: datetime
    comments_url: str = ""
    snippet: str = ""
    domains: set = field(default_factory=set)


def domain_of(url: str) -> str:
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def base_domain(d: str) -> str:
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


def passes_quality(title: str, url: str) -> bool:
    if not title or not url:
        return False
    if len(title.split()) < MIN_TITLE_WORDS:
        return False
    if TITLE_REJECT.search(title):
        return False
    d = domain_of(url)
    if not d:
        return False
    bd = base_domain(d)
    if bd in BLOCKED_DOMAINS or d in BLOCKED_DOMAINS:
        return False
    return True


def trust_multiplier(url: str) -> float:
    d = domain_of(url)
    bd = base_domain(d)
    if d in TRUSTED_DOMAINS or bd in TRUSTED_DOMAINS:
        return 1.6
    if d.endswith(".edu") or d.endswith(".gov") or d.endswith(".ac.uk"):
        return 1.5
    return 1.0


def recency_multiplier(published: datetime, now: datetime) -> float:
    age_h = (now - published).total_seconds() / 3600.0
    if age_h <= 12:
        return 1.0
    if age_h <= 24:
        return 0.85
    return 0.7


# ---------------- collectors ----------------

def fetch_hn(topic: str, now: datetime, cutoff_ts: int) -> list:
    items = []
    for q in HN_QUERIES[topic]:
        url = (
            "https://hn.algolia.com/api/v1/search"
            f"?query={urllib.parse.quote(q)}"
            f"&tags=story&numericFilters=created_at_i>{cutoff_ts},points>{MIN_HN_POINTS}"
            "&hitsPerPage=30"
        )
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            hits = r.json().get("hits", [])
        except Exception as e:
            print(f"[hn] {topic}/{q}: {e}")
            continue

        for h in hits:
            link = h.get("url") or ""
            title = h.get("title") or ""
            if not link or not passes_quality(title, link):
                continue
            pub = datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc)
            points = h.get("points", 0) or 0
            ncom = h.get("num_comments", 0) or 0
            raw = points + 2.0 * ncom
            items.append(
                Item(
                    title=html.unescape(title.strip()),
                    url=link,
                    topic=topic,
                    source="HN",
                    score=raw * trust_multiplier(link) * recency_multiplier(pub, now),
                    published=pub,
                    comments_url=f"https://news.ycombinator.com/item?id={h['objectID']}",
                )
            )
        time.sleep(0.3)
    return items


def fetch_reddit(topic: str, now: datetime, cutoff: datetime) -> list:
    items = []
    for sub in SUBREDDITS[topic]:
        url = f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=50"
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            children = r.json()["data"]["children"]
        except Exception as e:
            print(f"[reddit] r/{sub}: {e}")
            continue

        for c in children:
            d = c["data"]
            if d.get("is_self") or d.get("stickied") or d.get("over_18"):
                continue
            score = d.get("score", 0) or 0
            if score < MIN_REDDIT_SCORE:
                continue
            link = d.get("url_overridden_by_dest") or d.get("url") or ""
            title = d.get("title") or ""
            if not passes_quality(title, link):
                continue
            pub = datetime.fromtimestamp(d["created_utc"], tz=timezone.utc)
            if pub < cutoff:
                continue
            ncom = d.get("num_comments", 0) or 0
            ratio = d.get("upvote_ratio", 0.5) or 0.5
            if ratio < 0.75:
                continue
            raw = (score + 3.0 * ncom) * 0.25  # normalize vs HN scale
            items.append(
                Item(
                    title=html.unescape(title.strip()),
                    url=link,
                    topic=topic,
                    source=f"r/{sub}",
                    score=raw * trust_multiplier(link) * recency_multiplier(pub, now),
                    published=pub,
                    comments_url="https://www.reddit.com" + d.get("permalink", ""),
                )
            )
        time.sleep(1.0)
    return items


def fetch_rss(topic: str, now: datetime, cutoff: datetime) -> list:
    items = []
    for feed_url in RSS_FEEDS[topic]:
        try:
            f = feedparser.parse(feed_url, agent=UA)
        except Exception as e:
            print(f"[rss] {feed_url}: {e}")
            continue

        for e in f.entries[:25]:
            link = getattr(e, "link", "") or ""
            title = getattr(e, "title", "") or ""
            if not passes_quality(title, link):
                continue
            tm = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if not tm:
                continue
            pub = datetime.fromtimestamp(time.mktime(tm), tz=timezone.utc)
            if pub < cutoff:
                continue
            raw = 25.0  # RSS has no virality signal; trust+recency decide
            items.append(
                Item(
                    title=html.unescape(title.strip()),
                    url=link,
                    topic=topic,
                    source=domain_of(link) or "RSS",
                    score=raw * trust_multiplier(link) * recency_multiplier(pub, now),
                    published=pub,
                    snippet=html.unescape(re.sub(r"<[^>]+>", "", getattr(e, "summary", ""))[:200]),
                )
            )
    return items


# ---------------- dedup + selection ----------------

def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower())


def dedupe(items: list) -> list:
    seen_url, seen_title, out = set(), [], []
    for it in sorted(items, key=lambda x: x.score, reverse=True):
        u = it.url.split("?")[0].rstrip("/")
        if u in seen_url:
            continue
        nt = set(norm_title(it.title).split())
        dup = False
        for prev in seen_title:
            if not nt or not prev:
                continue
            jac = len(nt & prev) / len(nt | prev)
            if jac > 0.6:
                dup = True
                break
        if dup:
            continue
        seen_url.add(u)
        seen_title.append(nt)
        out.append(it)
    return out


def select(items: list) -> list:
    by_topic = {t: [] for t in QUOTA}
    for it in items:
        by_topic[it.topic].append(it)
    for t in by_topic:
        by_topic[t].sort(key=lambda x: x.score, reverse=True)

    chosen, used = [], set()
    for t, n in QUOTA.items():
        for it in by_topic[t]:
            if len(chosen) >= sum(QUOTA.values()):
                break
            key = it.url.split("?")[0].rstrip("/")
            if key in used:
                continue
            chosen.append(it)
            used.add(key)
            if sum(1 for c in chosen if c.topic == t) >= n:
                break

    # backfill if a topic came up empty
    if len(chosen) < 5:
        pool = [i for i in items if i.url.split("?")[0].rstrip("/") not in used]
        pool.sort(key=lambda x: x.score, reverse=True)
        for it in pool:
            if len(chosen) >= 5:
                break
            chosen.append(it)
            used.add(it.url.split("?")[0].rstrip("/"))

    order = {t: i for i, t in enumerate(QUOTA)}
    chosen.sort(key=lambda x: (order.get(x.topic, 9), -x.score))
    return chosen[:5]


# ---------------- output ----------------

LABEL = {"ai": "🤖 AI", "health": "🧬 Health", "quantum": "⚛️ Quantum", "tech": "💻 Tech"}


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def build_message(items: list, now: datetime) -> str:
    lines = [f"<b>Daily Digest — {now.strftime('%d %b %Y')}</b>", ""]
    if not items:
        lines.append("No items passed the quality filter in the last 48h.")
        return "\n".join(lines)

    for i, it in enumerate(items, 1):
        lines.append(f"<b>{i}. {LABEL.get(it.topic, it.topic)}</b>")
        lines.append(f'<a href="{esc(it.url)}">{esc(it.title)}</a>')
        meta = f"{esc(it.source)} · {domain_of(it.url)} · score {it.score:.0f}"
        if it.comments_url:
            meta += f' · <a href="{esc(it.comments_url)}">discussion</a>'
        lines.append(f"<i>{meta}</i>")
        lines.append("")
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not r.ok:
        print(r.text)
    r.raise_for_status()


def main() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    cutoff_ts = int(cutoff.timestamp())

    all_items = []
    for topic in QUOTA:
        all_items += fetch_hn(topic, now, cutoff_ts)
        all_items += fetch_reddit(topic, now, cutoff)
        all_items += fetch_rss(topic, now, cutoff)

    print(f"collected={len(all_items)}")
    items = dedupe(all_items)
    print(f"after dedupe={len(items)}")
    chosen = select(items)
    print(f"chosen={len(chosen)}")
    send_telegram(build_message(chosen, now))


if __name__ == "__main__":
    main()
