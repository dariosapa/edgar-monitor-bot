#!/usr/bin/env python3
"""
Real-time M&A Monitor for SEC filings and PR Newswire.
Fetches latest deals, extracts key data (ticker, offer price, market price, premium)
and sends Telegram notifications for publicly traded targets.
Supports a test mode for a specific date (via --test-date or TEST_DATE env var),
and fallback ticker lookup by company name to ensure no acquisition of a public
company is missed.
"""
import os
import re
import time
import logging
import argparse
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import yfinance as yf
import urllib.parse

# === CONFIGURATION ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI")  # Telegram bot token
CHAT_ID = os.getenv("CHAT_ID", "687693382")  # Telegram chat ID
# Default TEST_DATE is None: only enable test mode if env var or CLI flag provided
TEST_DATE = os.getenv("TEST_DATE", "2025-07-11")  # e.g. "2025-07-11"

# Feeds to monitor
FEEDS = [
    {"name": "SEC 8-K", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"name": "SEC S-4", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"name": "SEC SC TO-C", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"name": "SEC SC 13D", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"name": "SEC DEFM14A", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"},
    {"name": "PR Newswire M&A", "url": "https://www.prnewswire.com/rss/Acquisitions-Mergers-and-Takeovers-list.rss"}
]

# Keywords for detection
POSITIVE_KEYWORDS = [
    r"\bacquisition\b", r"\bmerger\b", r"\bwill acquire\b", r"\bto acquire\b",
    r"\bacquires\b", r"\bbuyout\b", r"\btakeover\b", r"\bmerger of equals\b",
    r"\bstock[- ]for[- ]stock\b", r"\btender offer\b", r"\bexchange offer\b"
]
NEGATIVE_KEYWORDS = [
    r"\bcompleted\b", r"\bclosing(?: of)?\b", r"\beffective as of\b",
    r"\bsubject to closing conditions\b"
]

# Regex for ticker extraction
TICKER_REGEX = re.compile(
    r"\b(?:NYSE(?:\sAmerican)?|NASDAQ|AMEX|TSX(?:V)?|TSXV|OTCQB|OTCQX):\s*([A-Z\.\-]+)\b",
    re.IGNORECASE
)

# Patterns for offer price extraction
PRICE_PATTERNS = [
    r"for\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
    r"at\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
    r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s*per share",
    r"consideration of\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)"
]

# State trackers
t_sent_links = set()
latest_dates = {}

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    resp = requests.post(url, data=payload)
    if resp.status_code != 200:
        logging.error(f"Telegram API error {resp.status_code}: {resp.text}")


def get_market_price(ticker: str):
    try:
        return yf.Ticker(ticker).info.get("regularMarketPrice")
    except Exception as e:
        logging.warning(f"Failed to fetch market price for {ticker}: {e}")
        return None


def extract_offer_price(text: str):
    for pat in PRICE_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def extract_ticker(text: str):
    m = TICKER_REGEX.search(text)
    return m.group(1).upper() if m else None


def fetch_full_text_ticker(url: str):
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            full_text = BeautifulSoup(r.text, "html.parser").get_text()
            return extract_ticker(full_text)
    except Exception as e:
        logging.warning(f"Fallback ticker fetch failed for {url}: {e}")
    return None


def lookup_ticker_by_name(name: str):
    try:
        query = urllib.parse.quote(name)
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}"
        r = requests.get(url, timeout=10)
        data = r.json()
        for item in data.get("quotes", []):
            symbol = item.get("symbol")
            exch = item.get("exchange")
            if symbol and exch in {"NMS","NYQ","ASE","AME","NCM","TSX","TSXV"}:
                return symbol
    except Exception as e:
        logging.warning(f"Name lookup failed for '{name}': {e}")
    return None


def extract_target_name(title: str):
    patterns = [
        r"to\s+acquire\s+([A-Z0-9 '&\.\-]+)",
        r"will\s+acquire\s+([A-Z0-9 '&\.\-]+)",
        r"acquisition\s+of\s+([A-Z0-9 '&\.\-]+)",
        r"merger\s+with\s+([A-Z0-9 '&\.\-]+)",
        r"merger\s+of\s+equals\s+with\s+([A-Z0-9 '&\.\-]+)"
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip('.,;:')
    return None


def init_latest_dates():
    now = datetime.now(timezone.utc)
    for feed in FEEDS:
        latest_dates[feed['name']] = now


def process_entry(feed_name: str, entry):
    link = entry.link
    pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc) if hasattr(entry, 'updated_parsed') else datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

    if pub <= latest_dates[feed_name] or link in t_sent_links:
        return

    title = (entry.title or "").strip()
    raw_html = entry.content[0].value if hasattr(entry, 'content') and entry.content else entry.get('summary', entry.get('description', ''))
    content = BeautifulSoup(raw_html, "html.parser").get_text()
    tl = title.lower()

    if any(re.search(p, tl) for p in NEGATIVE_KEYWORDS):
        latest_dates[feed_name] = max(latest_dates[feed_name], pub)
        return
    if not any(re.search(p, tl) for p in POSITIVE_KEYWORDS):
        latest_dates[feed_name] = max(latest_dates[feed_name], pub)
        return

    ticker = extract_ticker(content) or fetch_full_text_ticker(link)
    if not ticker:
        target = extract_target_name(title)
        ticker = lookup_ticker_by_name(target) if target else None
    if not ticker:
        ticker = lookup_ticker_by_name(title)

    if not ticker:
        msg = [
            f"📢 *New M&A Alert ({feed_name})!*",
            f"🏢 *Title:* {title}",
            f"🎯 *Ticker:* 🔍 non trovato",
            f"📅 *Date:* {pub.isoformat()}",
            f"🔗 [Link]({link})"
        ]
        send_telegram_message("\n".join(msg))
        t_sent_links.add(link)
        latest_dates[feed_name] = max(latest_dates[feed_name], pub)
        return

    market_price = get_market_price(ticker)
    offer_price = extract_offer_price(content)
    premium_pct = None
    if market_price is not None and offer_price is not None:
        try:
            premium_pct = (offer_price - market_price) / market_price * 100
        except ZeroDivisionError:
            premium_pct = None

    msg = [
        f"📢 *New M&A Alert ({feed_name})!*",
        f"🏢 *Title:* {title}",
        f"🎯 *Ticker:* {ticker}"
    ]
    if offer_price is not None:
        msg.append(f"💰 *Offer Price:* ${offer_price:.2f}")
    if market_price is not None:
        msg.append(f"📈 *Market Price:* ${market_price:.2f}")
    if premium_pct is not None:
        msg.append(f"🔥 *Premium:* {premium_pct:.1f}%")
    msg.extend([f"📅 *Date:* {pub.isoformat()}", f"🔗 [Link]({link})"])

    send_telegram_message("\n".join(msg))
    t_sent_links.add(link)
    latest_dates[feed_name] = max(latest_dates[feed_name], pub)


def test_for_date(date_str: str):
    test_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for k in latest_dates:
        latest_dates[k] = test_dt - timedelta(seconds=1)
    global send_telegram_message
    send_telegram_message = lambda text: print(f"[TEST NOTIFICATION]\n{text}\n")
    for feed in FEEDS:
        data = feedparser.parse(feed['url'])
        for entry in data.entries:
            process_entry(feed['name'], entry)


def run():
    init_latest_dates()
    send_telegram_message("🟢 *M&A Monitor started*: watching SEC & PR Newswire 🚀")
    while True:
        for feed in FEEDS:
            data = feedparser.parse(feed['url'])
            for entry in data.entries:
                process_entry(feed['name'], entry)
        time.sleep(60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M&A Monitor")
    parser.add_argument("--test-date", help="Run a single test pass for given date 2025-07-11")
    args = parser.parse_args()
    date_to_test = args.test_date or TEST_DATE
    if date_to_test:
        init_latest_dates()
        test_for_date(date_to_test)
    else:
        run()

