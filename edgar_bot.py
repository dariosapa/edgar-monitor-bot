#!/usr/bin/env python3
"""
Real-time M&A Monitor for SEC filings and PR Newswire.
Enhanced version for Railway.app deployment.
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
import signal
import sys

# === CONFIGURATION ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TEST_DATE = os.getenv("TEST_DATE", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Feeds to monitor
FEEDS = [
    {"name": "SEC 8-K", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"name": "SEC S-4", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"name": "SEC SC TO-C", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"name": "SEC SC 13D", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"name": "SEC DEFM14A", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"},
    {"name": "PR Newswire M&A", "url": "https://www.prnewswire.com/rss/Acquisitions-Mergers-and-Takeovers-list.rss"}
]

# Enhanced keywords for detection
POSITIVE_KEYWORDS = [
    r"\bacquisition\b", r"\bmerger\b", r"\bacqui(re|sition|ring)\b", 
    r"\bto acquire\b", r"\bacquires\b", r"\bbuyout\b", r"\btakeover\b",
    r"\bmerger of equals\b", r"\bstock[- ]for[- ]stock\b", r"\btender offer\b",
    r"\bexchange offer\b", r"\benters into (exclusive )?discussions\b",
    r"\bproposed acquisition\b", r"\bagreement to acquire\b", r"\bdefinitive agreement\b"
]
NEGATIVE_KEYWORDS = [
    r"\bcompleted\b", r"\bclosing(?: of)?\b", r"\beffective as of\b",
    r"\bfinalized\b", r"\bconcluded\b", r"\bsettled\b"
]

# Enhanced regex for ticker extraction
TICKER_REGEX = re.compile(
    r"(?:NYSE|NASDAQ|AMEX|OTC(?:QB|QX)?|TSX(?:V)?|NEO):?\s*([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\b",
    re.IGNORECASE
)

# State trackers
t_sent_links = set()
latest_dates = {}

# Logging setup
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)
logger = logging.getLogger(__name__)

# Precompile regex patterns for performance
POSITIVE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in POSITIVE_KEYWORDS]
NEGATIVE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_KEYWORDS]

# Graceful shutdown handler
def handle_shutdown(signum, frame):
    logger.info("🛑 Shutdown signal received. Exiting gracefully...")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)

def send_telegram_message(text: str):
    """Send message to Telegram with error handling"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Telegram credentials missing")
        return
        
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.debug("Telegram message sent successfully")
    except Exception as e:
        logger.error(f"Telegram API error: {e}")

def get_market_price(ticker: str):
    """Fetch market price from Yahoo Finance with fallbacks"""
    try:
        stock = yf.Ticker(ticker)
        return (
            stock.info.get("regularMarketPrice") or 
            stock.info.get("previousClose") or
            stock.info.get("currentPrice")
        )
    except Exception as e:
        logger.warning(f"Failed to fetch market price for {ticker}: {e}")
        return None

def extract_offer_price(text: str):
    """Extract offer price using multiple patterns"""
    patterns = [
        r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"for\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"at\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"per share\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
        r"consideration of\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
    ]
    
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(',', ''))
            except ValueError:
                continue
    return None

def extract_ticker(text: str):
    """Extract ticker symbol from text"""
    m = TICKER_REGEX.search(text)
    return m.group(1).upper().replace('.', '-') if m else None

def fetch_full_text_ticker(url: str):
    """Fetch full text content and extract ticker"""
    try:
        headers = {'User-Agent': 'M&A Monitor Bot (support@example.com)'}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        full_text = BeautifulSoup(r.text, "html.parser").get_text()
        return extract_ticker(full_text)
    except Exception as e:
        logger.warning(f"Fallback ticker fetch failed: {e}")
    return None

def lookup_ticker_by_name(name: str):
    """Search ticker by company name using Yahoo Finance"""
    if not name:
        return None
        
    try:
        query = urllib.parse.quote(name)
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        for item in data.get("quotes", []):
            symbol = item.get("symbol")
            if symbol and item.get("quoteType") in ["EQUITY", "ETF"]:
                return symbol
    except Exception as e:
        logger.warning(f"Ticker lookup failed for '{name}': {e}")
    return None

def extract_target_name(text: str):
    """Extract target company name from text"""
    patterns = [
        r"to\s+acquire\s+([\w\s&'\.-]{5,40}?)\b",
        r"acqui(sition|ring)\s+of\s+([\w\s&'\.-]{5,40}?)\b",
        r"merger\s+with\s+([\w\s&'\.-]{5,40}?)\b",
        r"agreement\s+to\s+acquire\s+([\w\s&'\.-]{5,40}?)\b",
        r"enters into discussions to acquire ([\w\s&'\.-]{5,40}?)\b"
    ]
    
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            target = m.group(1) if len(m.groups()) == 1 else m.group(2)
            clean_name = re.sub(r'\s*[.,;:]\s*$', '', target.strip())
            return clean_name
    return None

def init_latest_dates():
    """Initialize latest dates for each feed"""
    now = datetime.now(timezone.utc)
    for feed in FEEDS:
        latest_dates[feed['name']] = now

def process_entry(feed_name: str, entry):
    """Process a single feed entry"""
    link = entry.link
    
    # Handle date parsing
    pub_date = None
    for attr in ['updated_parsed', 'published_parsed', 'created_parsed']:
        if hasattr(entry, attr):
            pub_date = datetime(*getattr(entry, attr)[:6], tzinfo=timezone.utc)
            break
    
    if not pub_date:
        logger.warning(f"Skipping entry without date: {link}")
        return

    # Skip if already processed or too old
    if pub_date <= latest_dates[feed_name] or link in t_sent_links:
        return

    title = (entry.title or "").strip()
    logger.debug(f"Processing: {title}")
    
    # Get content safely
    try:
        if hasattr(entry, 'content') and entry.content:
            raw_content = entry.content[0].value
        else:
            raw_content = entry.get('summary', entry.get('description', ''))
        content = BeautifulSoup(raw_content, "html.parser").get_text()
    except Exception as e:
        logger.error(f"Error parsing content: {e}")
        content = ""

    text_to_check = f"{title}. {content}".lower()

    # Skip negative matches
    if any(pat.search(text_to_check) for pat in NEGATIVE_PATTERNS):
        logger.debug(f"Skipping: Negative keywords - {title}")
        latest_dates[feed_name] = max(latest_dates[feed_name], pub_date)
        return
        
    # Check for positive matches
    if not any(pat.search(text_to_check) for pat in POSITIVE_PATTERNS):
        logger.debug(f"Skipping: No M&A keywords - {title}")
        latest_dates[feed_name] = max(latest_dates[feed_name], pub_date)
        return

    # Extract ticker through multiple methods
    ticker = extract_ticker(content) or fetch_full_text_ticker(link)
    if not ticker:
        target_name = extract_target_name(title) or extract_target_name(content)
        ticker = lookup_ticker_by_name(target_name) if target_name else None
    if not ticker:
        ticker = lookup_ticker_by_name(title)

    # Prepare notification
    msg = [
        f"📢 *New M&A Alert ({feed_name})!*",
        f"🏢 *Title:* {title}",
        f"📅 *Date:* {pub_date.strftime('%Y-%m-%d %H:%M UTC')}",
        f"🔗 [Link]({link})"
    ]
    
    # Add ticker and pricing info
    if ticker:
        msg.insert(2, f"🎯 *Ticker:* {ticker}")
        market_price = get_market_price(ticker)
        offer_price = extract_offer_price(content)
        
        if offer_price is not None:
            msg.append(f"💰 *Offer Price:* ${offer_price:.2f}")
        if market_price is not None:
            msg.append(f"📈 *Market Price:* ${market_price:.2f}")
        if offer_price is not None and market_price is not None:
            try:
                premium_pct = (offer_price - market_price) / market_price * 100
                msg.append(f"🔥 *Premium:* {premium_pct:.1f}%")
            except ZeroDivisionError:
                pass
    else:
        msg.insert(2, "🎯 *Ticker:* 🔍 (Ricerca in corso)")
        if target_name:
            msg.append(f"🎯 *Potential Target:* {target_name}")

    send_telegram_message("\n".join(msg))
    t_sent_links.add(link)
    latest_dates[feed_name] = max(latest_dates[feed_name], pub_date)
    logger.info(f"Notification sent: {title}")

def test_for_date(date_str: str):
    """Test mode for specific date"""
    test_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for k in latest_dates:
        latest_dates[k] = test_dt - timedelta(seconds=1)
    
    logger.info(f"⏱️ Test mode for date: {date_str}")
    for feed in FEEDS:
        logger.info(f"Checking feed: {feed['name']}")
        try:
            data = feedparser.parse(feed['url'])
            logger.info(f"Found {len(data.entries)} entries")
            for entry in data.entries:
                process_entry(feed['name'], entry)
        except Exception as e:
            logger.error(f"Feed processing failed: {e}")
    logger.info("✅ Test completed")

def run_monitor():
    """Main monitoring loop"""
    init_latest_dates()
    send_telegram_message("🟢 *M&A Monitor started*: Watching SEC & PR Newswire 🚀")
    logger.info("Monitoring started")
    
    while True:
        try:
            for feed in FEEDS:
                logger.debug(f"Checking feed: {feed['name']}")
                try:
                    data = feedparser.parse(feed['url'])
                    for entry in data.entries:
                        process_entry(feed['name'], entry)
                except Exception as e:
                    logger.error(f"Feed error: {e}")
            time.sleep(60)
        except Exception as e:
            logger.critical(f"Critical error: {e}")
            time.sleep(300)

if __name__ == "__main__":
    # Validate credentials
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram credentials not set. Notifications disabled.")
    
    parser = argparse.ArgumentParser(description="M&A Monitor")
    parser.add_argument("--test-date", help="Test mode for specific date (YYYY-MM-DD)")
    args = parser.parse_args()
    
    if args.test_date:
        init_latest_dates()
        test_for_date(args.test_date)
    else:
        run_monitor()
