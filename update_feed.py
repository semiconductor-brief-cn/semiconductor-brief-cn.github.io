"""Collect verified public subscriptions for the shared investment dashboard."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

MARKET = ["VOO", "QQQM", "QNDX"]
CHIPS = ["QQQM", "QNDX", "VGT", "SMH"]
FEEDS = [
    ("nvda", "英伟达新闻中心", "https://nvidianews.nvidia.com/cats/press_release.xml", "nvidia.com", ["NVDA", *CHIPS]),
    ("amd", "AMD 投资者关系", "https://ir.amd.com/news-events/press-releases/rss", "amd.com", CHIPS),
    ("fed", "美联储货币政策", "https://www.federalreserve.gov/feeds/press_monetary.xml", "federalreserve.gov", MARKET),
    ("bea", "美国经济分析局", "https://www.bea.gov/news/rss", "bea.gov", MARKET),
    ("bls-calendar", "美国劳工统计局日历", "https://www.bls.gov/schedule/news_release/bls.ics", "bls.gov", MARKET),
    ("bea-calendar", "美国经济分析局日历", "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics", "bea.gov", MARKET),
]
ECONOMIC = re.compile(r"consumer price index|producer price index|employment situation|job openings and labor turnover|personal income and outlays|gross domestic product|^gdp \(", re.I)
CHIP_NEWS = re.compile(r"earnings|financial results|data cent[er]+|accelerator|semiconductor|artificial intelligence|\bAI\b|\bGPU\b|export|appoint|board of directors|acquisition", re.I)
CALENDAR_LABELS = {
    "consumer price index": "美国消费者价格指数（CPI）",
    "producer price index": "美国生产者价格指数（PPI）",
    "employment situation": "美国非农就业报告",
    "job openings and labor turnover survey": "美国 JOLTS 职位空缺",
}
SEEDED_IDS = {
    "1d8ac89d-ed64-4dce-844a-a24951208945": "bls-jolts-2026-10",
    "06c28939-7346-46f0-872e-afa00ccadc5d": "bls-jobs-2026-11",
    "395d58c3-51cf-44ef-b5bd-d35240623299": "bea-gdp-2026-q3-est2",
    "c6b23055-287a-4975-bcf7-026c2c6dd8e1": "bea-pce-2026-10",
}

def canonical_url(value, domain):
    try:
        url = urlsplit(value.strip())
        host = url.hostname or ""
        if url.scheme not in ("https", "http") or url.username or url.password or url.port:
            return ""
        if host == domain or host.endswith("." + domain):
            return url._replace(scheme="https", query="", fragment="").geturl().rstrip("/")
    except ValueError:
        pass
    return ""

def stable_id(prefix, value):
    return prefix + "-" + sha256(value.encode()).hexdigest()[:20]

def plain(value):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value or "")).split())

def rss_items(raw, feed, now):
    key, name, _, domain, symbols = feed
    channel = ElementTree.fromstring(raw).find("channel")
    if channel is None:
        raise ValueError("Missing RSS channel")
    for entry in channel.findall("item"):
        title = plain(entry.findtext("title"))
        link = canonical_url(entry.findtext("link", ""), domain)
        if not title or not link:
            continue
        if key in ("amd", "nvda") and not CHIP_NEWS.search(title):
            continue
        if key == "bea" and not ECONOMIC.search(title):
            continue
        try:
            at = parsedate_to_datetime(entry.findtext("pubDate", ""))
        except (TypeError, ValueError):
            continue
        if at.tzinfo is None or not now - timedelta(days=90) <= at <= now + timedelta(minutes=5):
            continue
        earnings = bool(re.search(r"financial results|reports.*earnings|reports.*(quarter|fiscal)", title, re.I))
        category = "财报与指引" if earnings else {"bea": "宏观数据", "fed": "央行与货币政策"}.get(key, "半导体行业")
        yield {
            "id": stable_id(key, link), "kind": "news", "title": title, "category": category,
            "at": at.astimezone(UTC).isoformat(), "publisherDate": at.date().isoformat(),
            "sourceType": "财报信息" if earnings else ("已证实事实" if key in ("bea", "fed") else "公司公告"),
            "source": {"name": name, "url": link}, "symbols": symbols,
            "summary": "来自公开官方订阅，详情及数据请查看原文。", "certainty": "高",
        }

def calendar_items(raw, feed, now):
    key, name, _, domain, symbols = feed
    content = re.sub(r"\r?\n[ \t]", "", raw.decode("utf-8-sig"))
    if "BEGIN:VCALENDAR" not in content or "END:VCALENDAR" not in content:
        raise ValueError("Missing calendar")
    for block in re.findall(r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT", content, re.S):
        fields = {}
        for line in block.splitlines():
            label, _, value = line.partition(":")
            field, _, parameters = label.partition(";")
            fields[field] = (value, parameters)
        title = fields.get("SUMMARY", ("", ""))[0].replace(r"\,", ",")
        uid = fields.get("UID", ("", ""))[0]
        if not uid or fields.get("STATUS", ("", ""))[0] == "CANCELLED" or not ECONOMIC.search(title):
            continue
        value, timezone = fields.get("DTSTART", ("", ""))
        if not re.fullmatch(r"\d{8}T\d{6}Z?", value):
            continue
        if value.endswith("Z"):
            zone = UTC
        elif timezone in ("TZID=America/New_York", "TZID=US-Eastern"):
            zone = ZoneInfo("America/New_York")
        else:
            continue
        at = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=zone)
        if not now - timedelta(days=7) <= at <= now + timedelta(days=49):
            continue
        calendar_url = "https://www.bls.gov/schedule/" if domain == "bls.gov" else "https://www.bea.gov/news/schedule"
        yield {
            "id": SEEDED_IDS.get(uid) or stable_id(key, uid), "kind": "calendar",
            "title": CALENDAR_LABELS.get(title.lower(), title), "category": "宏观数据",
            "at": at.astimezone(UTC).isoformat(), "sourceType": "官方安排",
            "source": {"name": name, "url": calendar_url}, "symbols": symbols,
            "summary": "按官方日历更新，具体发布安排以原文为准。", "certainty": "高",
        }

def collect(feed, now):
    key, name, url, domain, _ = feed
    try:
        request = Request(url, headers={"User-Agent": "SemiconductorBrief/1.0 (public RSS and calendar reader)"})
        with urlopen(request, timeout=16) as response:
            if not canonical_url(response.geturl(), domain):
                raise ValueError("Unexpected feed host")
            raw = response.read(2_500_001)
        if len(raw) > 2_500_000:
            raise ValueError("Feed is too large")
        parse = calendar_items if key.endswith("-calendar") else rss_items
        items = list(parse(raw, feed, now))
        return key, {"name": name, "ok": True, "items": len(items)}, items
    except (OSError, ValueError, ElementTree.ParseError) as error:
        print(f"{key}: {type(error).__name__}")
        return key, {"name": name, "ok": False, "items": 0}, []

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    previous = json.loads(args.output.read_text()) if args.output.exists() else {}
    now = datetime.now(UTC).replace(microsecond=0)
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        results = list(pool.map(lambda feed: collect(feed, now), FEEDS))
    status = [result[1] for result in results]
    if not any(feed["ok"] for feed in status):
        raise SystemExit("All official subscriptions unavailable; kept previous snapshot.")
    batches = previous.get("batches", {})
    for key, state, items in results:
        if state["ok"]:
            if key.endswith("-calendar"):
                batches[key] = items
            else:
                recent = {item["id"]: item for item in batches.get(key, []) + items}
                batches[key] = sorted(recent.values(), key=lambda item: item["at"], reverse=True)[:60]
    items = [item for group in batches.values() for item in group]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"state": {"checkedAt": now.isoformat(), "feeds": status}, "items": items, "batches": batches}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Official subscriptions: {sum(feed['ok'] for feed in status)}/{len(status)}. Records: {len(items)}.")

if __name__ == "__main__":
    main()
