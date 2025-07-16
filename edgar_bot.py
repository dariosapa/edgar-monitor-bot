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
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DATABASE = os.getenv("DATABASE", "ma_monitor.db")

# Feeds to monitor
FEEDS = [
    {"name": "SEC 8-K",       "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"name": "SEC S-4",       "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"name": "SEC SC TO-C",   "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"name": "SEC SC 13D",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"name": "SEC DEFM14A",   "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"},
    {"name": "PR Newswire M&A","url": "https://www.prnewswire.com/rss/Acquisitions-Mergers-and-Takeovers-list.rss"}
]

# Logging setup
ing logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)
logger = logging.getLogger(__name__)

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
    re.compile(r"\b(announces|intends to|agrees to).{0,20}?(acquisition|merger|acqui(re|sition|ring)|buyout|takeover|tender offer|exchange offer|definitive agreement)\b", re.IGNORECASE),
    re.compile(r"\bproposed (acquisition|merger)\b", re.IGNORECASE),
    re.compile(r"\b(annonce|entend).{0,20}?(acquisition|fusion)\b", re.IGNORECASE),
    re.compile(r"\b(aankondigt|voornemens om).{0,20}?(overname|fusie)\b", re.IGNORECASE)
]
NEGATIVE_PATTERNS = [
    re.compile(r"\b(completed|closing|closed|finalized|concluded|settled)\b", re.IGNORECASE),
    re.compile(r"\b(talent|data|customer|inventory|brand|division|portfolio|asset) acquisition\b", re.IGNORECASE),
    re.compile(r"\bsince [0-9]{4}\b", re.IGNORECASE),
    re.compile(r"\bover the past\b", re.IGNORECASE),
    re.compile(r"\b(product launch|event|partnership|sponsorship)\b", re.IGNORECASE)
]

# Ticker regex (fixed)
TICKER_REGEX = re.compile(
    r"(?:NYSE|NASDAQ|AMEX|OTC(?:QB|QX)?|TSX(?:V)?|NEO):?\s*([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\b",
    re.IGNORECASE
)

# Caches and state
_equity_cache = {}
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

# --- Telegram notifier ---
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.debug("Telegram message sent")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# --- Market data & extraction ---
def get_market_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        return stock.info.get("regularMarketPrice") or stock.info.get("previousClose") or stock.info.get("currentPrice")
    except Exception as e:
        logger.warning(f"Market price fetch failed for {ticker}: {e}")
        return None

def extract_offer_price(text):
    for pat in [
        r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"for\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"at\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"per share\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"consideration of\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(',', ''))
            except ValueError:
                continue
    return None

def lookup_ticker_by_name(name):
    if not name:
        return None
    try:
        q = urllib.parse.quote(name)
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={q}"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        for item in r.json().get("quotes", []):
            if item.get("quoteType") == "EQUITY":
                return item.get("symbol").replace('.', '-')
    except Exception as e:
        logger.warning(f"Ticker lookup failed for {name}: {e}")
    return None

def is_listed_equity(ticker):
    if ticker in _equity_cache:
        return _equity_cache[ticker]
    try:
        eq = yf.Ticker(ticker).info.get("quoteType") == "EQUITY"
    except Exception:
        eq = False
    _equity_cache[ticker] = eq
    return eq


