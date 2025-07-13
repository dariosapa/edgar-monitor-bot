#!/usr/bin/env python3
"""
Real-time M&A Monitor for SEC filings and PR Newswire.
Enhanced version with improved ticker extraction and deal detection.
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI")
CHAT_ID = os.getenv("CHAT_ID", "687693382")
TEST_DATE = os.getenv("TEST_DATE", "2025-07-11")

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

# Improved patterns for offer price extraction
PRICE_PATTERNS = [
    r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    r"for\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    r"at\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    r"per share\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    r"consideration of\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)",
    r"price of\s*\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
]

# State trackers
t_sent_links = set()
latest_dates = {}

# Logging setup
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)

# Precompile regex patterns for performance
POSITIVE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in POSITIVE_KEYWORDS]
NEGATIVE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_KEYWORDS]
PRICE_REGEXES = [re.compile(p, re.IGNORECASE) for p in PRICE_PATTERNS]

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=payload, timeout=10)
        if resp.status_code != 200:
            logging.error(f"Telegram API error {resp.status_code}: {resp.text}")
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

def get_market_price(ticker: str):
    try:
        stock = yf.Ticker(ticker)
        # Try regular market price first, then previous close
        return stock.info.get("regularMarketPrice") or stock.info.get("previousClose")
    except Exception as e:
        logging.warning(f"Failed to fetch market price for {ticker}: {e}")
        return None

def extract_offer_price(text: str):
    for regex in PRICE_REGEXES:
        matches = regex.findall(text)
        if matches:
            try:
                # Take the first match and remove commas
                price_str = matches[0].replace(',', '')
                return float(price_str)
            except (ValueError, IndexError):
                continue
    return None

def extract_ticker(text: str):
    m = TICKER_REGEX.search(text)
    return m.group(1).upper().replace('.', '-') if m else None

def fetch_full_text_ticker(url: str):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
        r = requests.get(url, headers=headers, timeout=15)
        if r.ok:
            full_text = BeautifulSoup(r.text, "html.parser").get_text()
            return extract_ticker(full_text)
    except Exception as e:
        logging.warning(f"Fallback ticker fetch failed for {url}: {e}")
    return None

def lookup_ticker_by_name(name: str):
    if not name:
        return None
        
    try:
        query = urllib.parse.quote(name)
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}"
        r = requests.get(url, timeout=10)
        data = r.json()
        
        # First try exact match
        for item in data.get("quotes", []):
            if item.get("longname", "").lower() == name.lower():
                return item.get("symbol")
                
        # Then try close matches
        for item in data.get("quotes", []):
            symbol = item.get("symbol")
            exch = item.get("exchange")
            # Validate exchange and symbol format
            if symbol and len(symbol) <= 5 and exch in {"NMS", "NYQ", "ASE", "NCM", "TSX", "TSXV"}:
                return symbol
    except Exception as e:
        logging.warning(f"Name lookup failed for '{name}': {e}")
    return None

def extract_target_name(text: str):
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
            # Handle patterns with different group numbers
            target = m.group(1) if len(m.groups()) == 1 else m.group(2)
            # Clean up extracted name
            clean_name = re.sub(r'\s*[.,;:]\s*$', '', target.strip())
            clean_name = re.sub(r'\b(?:llc|inc|plc|corp|co|company)\b', '', clean_name, flags=re.IGNORECASE).strip()
            return clean_name
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
    text_to_check = f"{title}. {content}".lower()

    # Skip if any negative keyword is found
    if any(pat.search(text_to_check) for pat in NEGATIVE_PATTERNS):
        logging.debug(f"Skipping due to negative keyword: {title}")
        latest_dates[feed_name] = max(latest_dates[feed_name], pub)
        return
        
    # Check for positive keywords
    if not any(pat.search(text_to_check) for pat in POSITIVE_PATTERNS):
        logging.debug(f"No positive keywords found: {title}")
        latest_dates[feed_name] = max(latest_dates[feed_name], pub)
        return

    # Extract ticker through multiple methods
    ticker = extract_ticker(content)
    if not ticker:
        ticker = fetch_full_text_ticker(link)
    if not ticker:
        target = extract_target_name(title) or extract_target_name(content)
        if target:
            ticker = lookup_ticker_by_name(target)
    if not ticker:
        ticker = lookup_ticker_by_name(title)

    # Prepare notification
    msg = [
        f"📢 *New M&A Alert ({feed_name})!*",
        f"🏢 *Title:* {title}",
        f"📅 *Date:* {pub.strftime('%Y-%m-%d %H:%M UTC')}",
        f"🔗 [Link]({link})"
    ]
    
    # Add ticker information
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
        
        # Try to extract target name for additional context
        target = extract_target_name(title) or extract_target_name(content)
        if target:
            msg.append(f"🎯 *Potential Target:* {target}")

    send_telegram_message("\n".join(msg))
    t_sent_links.add(link)
    latest_dates[feed_name] = max(latest_dates[feed_name], pub)
    logging.info(f"Notification sent for: {title}")

def test_for_date(date_str: str):
    test_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for k in latest_dates:
        latest_dates[k] = test_dt - timedelta(seconds=1)
    global send_telegram_message
    send_telegram_message = lambda text: print(f"[TEST NOTIFICATION]\n{text}\n")
    for feed in FEEDS:
        logging.info(f"Processing feed: {feed['name']}")
        data = feedparser.parse(feed['url'])
        logging.info(f"Found {len(data.entries)} entries")
        for entry in data.entries:
            process_entry(feed['name'], entry)

def run():
    init_latest_dates()
    send_telegram_message("🟢 *M&A Monitor started*: watching SEC & PR Newswire 🚀")
    logging.info("Monitoring started")
    while True:
        try:
            for feed in FEEDS:
                logging.debug(f"Checking feed: {feed['name']}")
                data = feedparser.parse(feed['url'])
                for entry in data.entries:
                    process_entry(feed['name'], entry)
            time.sleep(60)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(300)  # Wait longer on error

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
