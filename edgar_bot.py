#!/usr/bin/env python3
"""
Real-time M&A Monitor with backfill support and persistent state.

Usage:
  python monitor.py [--test-date YYYY-MM-DD] [--backfill-days N]

Options:
  --test-date YYYY-MM-DD    Run a one-shot test for that date (prints to stdout instead of Telegram).
  --backfill-days N         On real run, send all items from the last N days (backfill historical data).

Configuration:
  Modify BOT_TOKEN and CHAT_ID directly in the script before running. Ensure you keep the quotes.
"""
import re
import time
import argparse
import sqlite3
import logging
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from bs4 import BeautifulSoup
import yfinance as yf

# === CONFIGURATION ===
BOT_TOKEN = "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI"  # Inserisci qui il tuo token
CHAT_ID = "687693382"  # Inserisci qui il tuo chat ID
DB_PATH = "state.db"  # Percorso al file SQLite per persistere lo stato

# === FEEDS ===
FEEDS = [
    {"name": "SEC 8-K",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"name": "SEC S-4",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"name": "SEC SC TO-C", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"name": "SEC SC 13D", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"name": "SEC DEFM14A","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"},
    {"name": "PR Newswire M&A", "url": "https://www.prnewswire.com/rss/Acquisitions-Mergers-and-Takeovers-list.rss"}
]

# === PATTERNS ===
POSITIVE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bacquisition\b", r"\bmerger\b", r"to acquire",
        r"acqu(?:ire|sition|ring)", r"buyout", r"takeover",
        r"merger of equals", r"stock[- ]for[- ]stock",
        r"tender offer", r"exchange offer",
        r"enters into exclusive discussions", r"definitive agreement"
    ]
]
NEGATIVE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bcompleted\b", r"closing of", r"effective as of",
        r"finalized", r"concluded"
    ]
]
TICKER_REGEX = re.compile(
    r"(?:NYSE|NASDAQ|AMEX|OTC(?:QB|QX)?|TSX(?:V)?|NEO):?\s*([A-Z0-9\.\-]{1,5})\b",
    re.IGNORECASE
)
PRICE_REGEX = re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)")

# === LOGGING ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)
logger = logging.getLogger(__name__)

# === STATE PERSISTENCE ===
def init_db(path: str):
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_links (
            link TEXT PRIMARY KEY,
            pub_date TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, link: str) -> bool:
    c = conn.cursor()
    c.execute("SELECT 1 FROM seen_links WHERE link = ?", (link,))
    return c.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, link: str, pub: datetime):
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO seen_links(link, pub_date) VALUES (?, ?)",
        (link, pub.isoformat())
    )
    conn.commit()

# === NOTIFICATION ===
def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Telegram API error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

# === DATA EXTRACTION ===
def extract_text(html: str) -> str:
    return BeautifulSoup(html, "lxml").get_text(separator=" ")


def extract_ticker(text: str) -> str:
    m = TICKER_REGEX.search(text)
    return m.group(1).upper().replace('.', '-') if m else None


def lookup_ticker(name: str) -> str:
    try:
        q = requests.utils.quote(name)
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={q}"
        r = requests.get(url, timeout=10)
        items = r.json().get('quotes', [])
        for itm in items:
            if itm.get('symbol') and itm.get('exchange') in {"NMS","NYQ","ASE","AMEX","TSX","TSXV"}:
                return itm['symbol']
    except Exception as e:
        logger.warning(f"Yahoo lookup failed for '{name}': {e}")
    return None


def get_market_price(ticker: str) -> float:
    try:
        info = yf.Ticker(ticker).info
        return info.get('regularMarketPrice') or info.get('previousClose')
    except Exception as e:
        logger.warning(f"Market price fetch failed for {ticker}: {e}")
        return None


def extract_offer_price(text: str) -> float:
    m = PRICE_REGEX.search(text)
    if not m:
        return None
    s = m.group(1).replace(',', '')
    try:
        return float(s)
    except ValueError:
        return None

# === ENTRY PROCESSING ===
def process_entry(
    conn: sqlite3.Connection,
    cutoff: datetime,
    feed_name: str,
    entry,
    test_mode: bool = False
):
    link = entry.link
    pub_struct = entry.get('published_parsed') or entry.get('updated_parsed')
    pub = datetime(*pub_struct[:6], tzinfo=timezone.utc)

    if pub < cutoff or is_seen(conn, link):
        return

    title = entry.title or ''
    raw_html = ''.join([c.value for c in entry.get('content', [])]) or entry.get('summary', '')
    text = extract_text(raw_html)
    combined = f"{title}. {text}"

    # FILTERS
    if any(p.search(combined) for p in NEGATIVE_PATTERNS):
        mark_seen(conn, link, pub)
        return
    if not any(p.search(combined) for p in POSITIVE_PATTERNS):
        mark_seen(conn, link, pub)
        return

    # TICKER
    ticker = extract_ticker(text) or lookup_ticker(title)

    # BUILD MESSAGE
    msg = [
        f"📢 *New M&A Alert ({feed_name})*",
        f"*Title:* {title}",
        f"*Date:* {pub.strftime('%Y-%m-%d %H:%M UTC')}",
        f"[Link]({link})"
    ]
    if ticker:
        msg.insert(2, f"*Ticker:* {ticker}")
        offer = extract_offer_price(text)
        market = get_market_price(ticker)
        if offer is not None:
            msg.append(f"*Offer Price:* ${offer:.2f}")
        if market is not None:
            msg.append(f"*Market Price:* ${market:.2f}")
        if offer and market:
            try:
                prem = (offer - market) / market * 100
                msg.append(f"*Premium:* {prem:.1f}%")
            except ZeroDivisionError:
                pass
    else:
        msg.insert(2, "*Ticker:* 🔍 ricerca in corso")

    text_msg = "\n".join(msg)
    if test_mode:
        print("[TEST NOTIFICATION]\n" + text_msg + "\n")
    else:
        send_telegram_message(text_msg)

    mark_seen(conn, link, pub)
    logger.info(f"Notification sent for: {title}")

# === MAIN ===
def main():
    parser = argparse.ArgumentParser(description="Real-time M&A Monitor")
    parser.add_argument(
        "--test-date",
        help="One-shot test for date YYYY-MM-DD (prints to stdout)"
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        help="Real run: send all items from the last N days"
    )
    args = parser.parse_args()

    conn = init_db(DB_PATH)
    now = datetime.now(timezone.utc)

    if args.test_date:
        test_dt = datetime.strptime(args.test_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        cutoff = test_dt - timedelta(seconds=1)
        test_mode = True
    elif args.backfill_days:
        cutoff = now - timedelta(days=args.backfill_days)
        test_mode = False
    else:
        cutoff = now
        test_mode = False

    # ONE-PASS TEST/BACKFILL
    for feed in FEEDS:
        data = feedparser.parse(feed['url'])
        for entry in data.entries:
            process_entry(conn, cutoff, feed['name'], entry, test_mode)

    # CONTINUOUS LOOP
    if not test_mode:
        send_telegram_message("🟢 *M&A Monitor avviato* 🚀")
        while True:
            try:
                for feed in FEEDS:
                    data = feedparser.parse(feed['url'])
                    for entry in data.entries:
                        process_entry(conn, cutoff, feed['name'], entry)
                time.sleep(60)
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                time.sleep(300)

if __name__ == "__main__":
    main()
