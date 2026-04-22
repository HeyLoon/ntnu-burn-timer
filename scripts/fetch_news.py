#!/usr/bin/env python3
"""Daily news fetcher for the NTNU burn timer.

Searches Google News RSS for 師大-related controversy coverage,
updates incidents.json with new items, and auto-advances last_incident
when a high-confidence incident is detected.
"""

import json
import os
import sys
import re
import calendar
import difflib
import urllib.parse
from datetime import datetime, timezone, timedelta

try:
    import feedparser
except ImportError:
    print("feedparser not installed. Run: pip install feedparser", file=sys.stderr)
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────

QUERIES = [
    "臺灣師範大學 炎上",
    "師大 炎上",
    "師大 爭議",
    "師大生 批評",
    "師大 道歉",
    "師大 挨批",
    "臺師大 惹議",
    "師大 性平",
]

# Any match → add to recent_news
INCIDENT_RE = re.compile(
    r'炎上|道歉|撤下|撤展|抵制|批評|爭議|性平|歧視|privilege|特權|'
    r'網暴|公審|霸凌|挨批|惹議|挨轟|不雅|失言|歧視',
    re.IGNORECASE,
)

# High-confidence → also consider updating last_incident date
HIGH_CONF_RE = re.compile(
    r'炎上|道歉|撤下|撤展|網暴',
    re.IGNORECASE,
)

TW = timezone(timedelta(hours=8))
DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'incidents.json')
MAX_AGE_DAYS = 14
RECENT_NEWS_CAP = 100
SAME_EVENT_WINDOW_DAYS = 7
SAME_EVENT_SIMILARITY_THRESHOLD = 0.9

EVENT_TAG_PATTERNS = {
    'bully': re.compile(r'霸凌|霸凌案|恐怖統治|公審'),
    'sexual': re.compile(r'性騷|性平|性侵|猥褻'),
    'blood': re.compile(r'抽血|採血|驗血|血檢'),
    'suspend': re.compile(r'停聘|停權|停賽|停職'),
    'apology': re.compile(r'道歉|致歉|鞠躬道歉'),
}

DOMAIN_LABELS = {
    'newtalk.tw': 'Newtalk',
    'ettoday.net': 'ETtoday',
    'ltn.com.tw': '自由時報',
    'udn.com': '聯合新聞網',
    'chinatimes.com': '中時',
    'cna.com.tw': '中央社',
    'ctwant.com': 'CTWANT',
    'nownews.com': 'NOWnews',
    'storm.mg': '風傳媒',
    'businesstoday.com.tw': '今周刊',
    'gvm.com.tw': '遠見雜誌',
    'setn.com': '三立新聞',
    'tvbs.com.tw': 'TVBS',
    'mirrormedia.mg': '鏡週刊',
    'dcard.tw': 'Dcard',
    'ptt.cc': 'PTT',
    'threads.com': 'Threads',
    'threads.net': 'Threads',
}


def source_label(url: str) -> str:
    for key, label in DOMAIN_LABELS.items():
        if key in url:
            return label
    try:
        parts = urllib.parse.urlparse(url).netloc.split('.')
        return '.'.join(parts[-2:]) if len(parts) >= 2 else url
    except Exception:
        return url


def fetch_feed(query: str) -> list:
    encoded = urllib.parse.quote(query)
    url = (
        f"https://news.google.com/rss/search"
        f"?q={encoded}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    feed = feedparser.parse(url)
    return feed.entries or []


def parse_date(entry) -> tuple:
    """Return (datetime | None, 'YYYY-MM-DD')."""
    if entry.get('published_parsed'):
        ts = calendar.timegm(entry.published_parsed)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TW)
        return dt, dt.strftime('%Y-%m-%d')
    return None, datetime.now(TW).strftime('%Y-%m-%d')


def clean_title(title: str) -> str:
    # Google News appends "- Source Name" to titles; strip it
    return re.sub(r'\s*[-–—]\s*\S.*$', '', title).strip()


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication by dropping tracking query params."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered_query = []
        for k, v in query:
            key = k.lower()
            if key.startswith('utm_') or key in {'oc', 'guccounter', 'guce_referrer', 'guce_referrer_sig'}:
                continue
            filtered_query.append((k, v))
        norm_query = urllib.parse.urlencode(filtered_query, doseq=True)
        return urllib.parse.urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip('/'),
                parsed.params,
                norm_query,
                '',
            )
        )
    except Exception:
        return url.strip()


def title_key(title: str) -> str:
    return re.sub(r'\s+', '', title).lower()


def parse_ymd(date_str: str) -> datetime | None:
    try:
        return datetime.strptime(date_str, '%Y-%m-%d')
    except (TypeError, ValueError):
        return None


def char_bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return set()
    return {text[i:i+2] for i in range(len(text) - 1)}


def event_tags(title: str) -> set[str]:
    tags: set[str] = set()
    for tag, pattern in EVENT_TAG_PATTERNS.items():
        if pattern.search(title):
            tags.add(tag)
    return tags


def same_event(title_a: str, date_a: str, title_b: str, date_b: str) -> bool:
    a = title_key(title_a)
    b = title_key(title_b)
    if not a or not b:
        return False

    d1 = parse_ymd(date_a)
    d2 = parse_ymd(date_b)
    if not d1 or not d2:
        return False
    if abs((d1 - d2).days) > SAME_EVENT_WINDOW_DAYS:
        return False

    if a == b:
        return True

    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio >= SAME_EVENT_SIMILARITY_THRESHOLD:
        return True

    shared_tags = event_tags(a) & event_tags(b)
    if not shared_tags:
        return False

    overlap = len(char_bigrams(a) & char_bigrams(b))
    return overlap >= 2


def item_fingerprint(item: dict) -> tuple[str, str, str]:
    """Stable key: normalized URL + normalized title + date."""
    return (
        normalize_url(item.get('url', '')),
        title_key(item.get('title', '')),
        item.get('date', ''),
    )


def dedupe_news(items: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_fp: set[tuple[str, str, str]] = set()
    seen_topics: list[tuple[str, str]] = []
    for item in items:
        fp = item_fingerprint(item)
        if fp in seen_fp:
            continue
        if any(same_event(item.get('title', ''), item.get('date', ''), t, d) for t, d in seen_topics):
            continue
        seen_fp.add(fp)
        seen_topics.append((item.get('title', ''), item.get('date', '')))
        deduped.append(item)
    return deduped


def main() -> None:
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Build deduplication set from existing data
    seen: set[str] = set()
    seen_title_date: set[tuple[str, str]] = set()
    seen_topics: list[tuple[str, str]] = []
    for item in data.get('recent_news', []):
        seen.add(normalize_url(item.get('url', '')))
        seen_title_date.add((title_key(item.get('title', '')), item.get('date', '')))
        seen_topics.append((item.get('title', ''), item.get('date', '')))
    for inc in data.get('incidents', []):
        for src in inc.get('sources', []):
            seen.add(normalize_url(src.get('url', '')))

    cutoff = datetime.now(TW) - timedelta(days=MAX_AGE_DAYS)
    new_items: list[dict] = []

    for query in QUERIES:
        try:
            entries = fetch_feed(query)
        except Exception as exc:
            print(f"[warn] Failed to fetch '{query}': {exc}", file=sys.stderr)
            continue

        for entry in entries[:15]:
            raw_url = entry.get('link', '').strip()
            norm_url = normalize_url(raw_url)
            if not raw_url or norm_url in seen:
                continue

            title = clean_title(entry.get('title', '').strip())
            summary = entry.get('summary', '')
            combined = f"{title} {summary}"

            if not INCIDENT_RE.search(combined):
                continue

            pub_dt, date_str = parse_date(entry)
            if pub_dt and pub_dt < cutoff:
                continue
            if (title_key(title), date_str) in seen_title_date:
                continue
            if any(same_event(title, date_str, t, d) for t, d in seen_topics):
                continue

            new_items.append({
                'date': date_str,
                'title': title,
                'url': raw_url,
                'source': source_label(raw_url),
                'high_confidence': bool(HIGH_CONF_RE.search(combined)),
            })
            seen.add(norm_url)
            seen_title_date.add((title_key(title), date_str))
            seen_topics.append((title, date_str))

    new_items.sort(key=lambda x: x['date'], reverse=True)

    # Prepend to recent_news and cap length
    merged_items = new_items + data.get('recent_news', [])
    data['recent_news'] = dedupe_news(merged_items)[:RECENT_NEWS_CAP]

    # Auto-advance last_incident when a high-confidence item is newer
    high_conf = [i for i in new_items if i['high_confidence']]
    if high_conf:
        latest_date = high_conf[0]['date']
        current_last = data['last_incident'][:10]
        article_dt = datetime.strptime(latest_date, '%Y-%m-%d').replace(tzinfo=TW)
        recency_cutoff = datetime.now(TW) - timedelta(days=14)
        if latest_date > current_last and article_dt >= recency_cutoff:
            data['last_incident'] = f"{latest_date}T00:00:00+08:00"
            data['last_incident_title'] = f"自動偵測：{high_conf[0]['title']}"
            data['auto_updated'] = True
            print(f"[info] last_incident advanced → {latest_date}")
        else:
            data['auto_updated'] = False
    else:
        data['auto_updated'] = False

    data['last_updated'] = datetime.now(TW).isoformat()

    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(
        f"[done] +{len(new_items)} new items  "
        f"| total recent_news: {len(data['recent_news'])}"
    )


if __name__ == '__main__':
    main()
