#!/usr/bin/env python3
"""
Real-time M&A Monitor for SEC filings and PR Newswire.
Optimized for Railway.app deployment with strict target filtering and state persistence.
"""
import os
import re
import time
import logging
import argparse
import requests
import feedparser
import sqlite3
import signal
import sys
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import yfinance as yf
import urllib.parse

# === CONFIGURATION ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
# Test date can come from CLI or ENV
ENV_TEST_DATE = os.getenv("TEST_DATE")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DATABASE = os.getenv("DATABASE", "ma_monitor.db")

# Feeds to monitor
FEEDS = [
    {"name": "SEC 8-K",        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"name": "SEC S-4",        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"name": "SEC SC TO-C",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"name": "SEC SC 13D",     "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"name": "SEC DEFM14A",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"},
    {"name": "PR Newswire M&A", "url": "https://www.prnewswire.com/rss/Acquisitions-Mergers-and-Takeovers-list.rss"}
]

# Logging setup
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)
logger = logging.getLogger(__name__)

# Exit if Telegram credentials missing
def check_credentials():
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Telegram credentials missing – aborting")
        sys.exit(1)

# Graceful shutdown handler
def handle_shutdown(signum, frame):
    logger.info("🛑 Shutdown signal received. Exiting gracefully...")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

# Direction-aware regex patterns
PATTERN_ACQUIRES = re.compile(
    r"(?P<acquirer>[\w\s&'\.-]{3,40}?)\b(?:acquires|to acquire|agreement to acquire)\b\s+(?P<target>[\w\s&'\.-]{3,40}?)\b",
    re.IGNORECASE
)
PATTERN_BY = re.compile(
    r"(?P<target>[\w\s&'\.-]{3,40}?)\b(?:announc(?:es|ed) acquisition by|to be acquired by)\b\s+(?P<acquirer>[\w\s&'\.-]{3,40}?)\b",
    re.IGNORECASE
)

# Positive/negative filters
POSITIVE_PATTERNS = [
    re.compile(r"\b(announces|intends to|agrees to|enters into).{0,20}?(acquisition|merger|acqui(re|sition|ring)|buyout|takeover|tender offer|exchange offer|definitive agreement)\b", re.IGNORECASE),
    re.compile(r"\bproposed (acquisition|merger)\b", re.IGNORECASE),
    re.compile(r"\b(annonce|entend).{0,20}?(acquisition|fusion)\b", re.IGNORECASE),
    re.compile(r"\b(aankondigt|voornemens om).{0,20}?(overname|fusie)\b", re.IGNORECASE)
]
NEGATIVE_PATTERNS = [
    re.compile(r"\b(completed|closing|closed|finalized|concluded|settled)\b", re.IGNORECASE),
    re.compile(r"\b(talent|data|customer|inventory|brand|division|portfolio|asset|property) acquisition\b", re.IGNORECASE),
    re.compile(r"\bsince [0-9]{4}\b", re.IGNORECASE),
    re.compile(r"\bover the past\b", re.IGNORECASE),
    re.compile(r"\b(product launch|event|partnership|sponsorship|joint venture)\b", re.IGNORECASE)
]

# Ticker regex
TICKER_REGEX = re.compile(
    r"(?:NYSE|NASDAQ|AMEX|OTC(?:QB|QX)?|TSX(?:V)?|NEO):?\s*([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\b",
    re.IGNORECASE
)

# Caches and state
_equity_cache = {}
t_sent_links = set()
latest_dates = {}

# --- Database functions ---
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS latest_dates (feed_name TEXT PRIMARY KEY, date TEXT)")
    conn.commit()
    conn.close()

def load_sent_links():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT link FROM sent_links")
    links = {row[0] for row in c.fetchall()}
    conn.close()
    return links

def save_sent_link(link):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO sent_links VALUES (?)", (link,))
    conn.commit()
    conn.close()

def load_latest_dates():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT feed_name, date FROM latest_dates")
    rows = c.fetchall()
    conn.close()
    return {row[0]: datetime.fromisoformat(row[1]) for row in rows}

def save_latest_date(feed_name, dt):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO latest_dates VALUES (?, ?)", (feed_name, dt.isoformat()))
    conn.commit()
    conn.close()

# --- Utility functions ---
def escape_md(text):
    return re.sub(r'([\\*_\[\]()~`>#+-=|{}\.!])', r'\\\1', text)

# --- Telegram notifier ---
def send_telegram_message(text):
    check_credentials()
    escaped = escape_md(text)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": escaped, "parse_mode": "MarkdownV2"}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.debug("Telegram message sent")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# --- Market data & extraction ---
def get_market_price(ticker):
    stock = yf.Ticker(ticker)
    info = stock.info
    return info.get("regularMarketPrice") or info.get("previousClose") or info.get("currentPrice")


def extract_offer_price(text):
    for pat in [
        r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"for\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"at\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"per share\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"consideration of\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
    ]:
        m = re.search(pat, text)
        if m:
            try:
                return float(m.group(1).replace(',', ''))
            except ValueError:
                continue
    return None

def fetch_full_text(url):
    r = requests.get(url, headers={'User-Agent': 'M&A Monitor Bot'}, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, 'html.parser').get_text()


def lookup_ticker_by_name(name):
    q = urllib.parse.quote(name)
    r = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={q}", timeout=15)
    r.raise_for_status()
    for item in r.json().get("quotes", []):
        if item.get("quoteType") == "EQUITY":
            return item.get("symbol").replace('.', '-')
    return None

def is_listed_equity(ticker):
    if ticker in _equity_cache:
        return _equity_cache[ticker]
    info = yf.Ticker(ticker).info
    eq = info.get("quoteType") == "EQUITY"
    _equity_cache[ticker] = eq
    return eq

def extract_target_ticker(target_name, title, content):
    pat = re.compile(
        rf"{re.escape(target_name)}.*?\((?:NYSE|NASDAQ|AMEX|OTC(?:QB|QX)?|TSX(?:V)?|NEO):?\s*([A-Z]{{1,5}}(?:\.[A-Z]{{1,2}})?)\)",
        re.IGNORECASE
    )
    for text in (title, content):
        m = pat.search(text)
        if m:
            t = m.group(1).upper().replace('.', '-')
            if is_listed_equity(t): return t
    m2 = TICKER_REGEX.search(content)
    if m2 and is_listed_equity(m2.group(1).upper().replace('.', '-')):
        return m2.group(1).upper().replace('.', '-')
    return lookup_ticker_by_name(target_name)

# --- State initialization ---
def init_state():
    init_db()
    global t_sent_links, latest_dates
    t_sent_links = load_sent_links()
    saved = load_latest_dates()
    now = datetime.now(timezone.utc)
    latest_dates = {f['name']: saved.get(f['name'], now) for f in FEEDS}
    # persist initial dates
    for name, dt in latest_dates.items():
        if name not in saved:
            save_latest_date(name, dt)

# --- Process single entry ---
def process_entry(feed_name, entry):
    link = entry.link
    # parse date
    for attr in ('updated_parsed','published_parsed','created_parsed'):
        if hasattr(entry, attr):
            pub_date = datetime(*getattr(entry, attr)[:6], tzinfo=timezone.utc)
            break
    else:
        return
    if pub_date <= latest_dates.get(feed_name) or link in t_sent_links:
        return
    title = (entry.title or '').strip()
    raw = entry.content[0].value if hasattr(entry,'content') and entry.content else entry.get('summary','')
    text = BeautifulSoup(raw,'html.parser').get_text()
    lc = f"{title}. {text}".lower()
    if any(p.search(lc) for p in NEGATIVE_PATTERNS):
        latest_dates[feed_name] = pub_date; save_latest_date(feed_name,pub_date); return
    if not any(p.search(lc) for p in POSITIVE_PATTERNS):
        latest_dates[feed_name] = pub_date; save_latest_date(feed_name,pub_date); return
    # direction
    for pat in (PATTERN_ACQUIRES,PATTERN_BY):
        m = pat.search(title) or pat.search(text[:500])
        if m:
            target = m.group('target').strip()
            acquirer = m.group('acquirer').strip()
            break
    else:
        return
    if not any(s in target.lower() for s in ['inc.','corp.','ltd.','plc','llc','corporation']): return
    ticker = extract_target_ticker(target,title,text)
    if not ticker or not is_listed_equity(ticker): return
    full = fetch_full_text(link)
    offer = extract_offer_price(text) or extract_offer_price(full)
    market = get_market_price(ticker)
    msg = [f"📢 *New M&A Alert ({feed_name})!*",
           f"🎯 *Target:* {target} ({ticker})",
           f"🏢 *Acquirer:* {acquirer}",
           f"📅 *Date:* {pub_date.strftime('%Y-%m-%d %H:%M UTC')}",
           f"🔗 [Link]({link})"]
    if offer: msg.append(f"💰 *Offer:* ${offer:.2f}")
    if market: msg.append(f"📈 *Market:* ${market:.2f}")
    if offer and market:
        try: msg.append(f"🔥 *Premium:* {(offer-market)/market*100:.1f}%")
        except: pass
    send_telegram_message("\n".join(msg))
    t_sent_links.add(link); save_sent_link(link)
    latest_dates[feed_name]=pub_date; save_latest_date(feed_name,pub_date)

# --- Test mode ---
def test_for_date(date_str):
    try:
        dt = datetime.strptime(date_str,'%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        logger.error('Invalid test date format')
        return
    for k in latest_dates: latest_dates[k]=dt - timedelta(seconds=1)
    for feed in FEEDS:
        data=feedparser.parse(feed['url'])
        for e in data.entries: process_entry(feed['name'],e)

# --- Monitor loop ---
def run_monitor():
    init_state(); check_credentials()
    send_telegram_message("🟢 *M&A Monitor started*: Watching SEC & PR Newswire 🚀")
    backoff=60
    while True:
        try:
            for f in FEEDS:
                data=feedparser.parse(f['url'])
                for e in data.entries: process_entry(f['name'],e)
            backoff=60
            time.sleep(backoff)
        except Exception as e:
            logger.critical(f"Fatal: {e}")
            backoff=min(backoff*2,300)
            time.sleep(backoff)

# --- Entry point ---
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--test-date', help='YYYY-MM-DD')
    args=parser.parse_args()
    if args.test_date or ENV_TEST_DATE:
        init_state()
        test_for_date(args.test_date or ENV_TEST_DATE)
    else:
        run_monitor()
