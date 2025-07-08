#!/usr/bin/env python3
import time
import re
import os
import requests
import feedparser
from bs4 import BeautifulSoup
import yfinance as yf
from datetime import datetime, timezone

# === CONFIGURATION ===
# Prefer environment variables for secrets in production
BOT_TOKEN = os.getenv("BOT_TOKEN", "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI")
CHAT_ID   = os.getenv("CHAT_ID",   "687693382")

# SEC Atom feeds for relevant filings
SEC_FEEDS = [
    {"type": "8-K",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"type": "S-4",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"type": "SC TO-C","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"type": "SC 13D","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"type": "DEFM14A","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"},
]

# PR Newswire RSS for M&A announcements
PRN_RSS_URL = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/"
    "acquisitions-mergers-and-takeovers-list.rss"
)

# === PATTERN CONFIGURATION ===
# Positive keywords for deal announcements
POSITIVE_KEYWORDS = [
    r"\bacquisition\b", r"\bmerger\b", r"\bwill acquire\b", r"\bto acquire\b",
    r"\bacquires\b", r"\bbuyout\b", r"\btakeover\b", r"\bmerger of equals\b",
    r"\bstock[- ]for[- ]stock\b", r"\btender offer\b", r"\bexchange offer\b"
]
# Negative keywords to filter out completed or closed deals (apply to titles only)
NEGATIVE_TITLE_KEYWORDS = [
    r"\bcompleted\b", r"\bcompletion\b", r"\bclosed\b", r"\beffective as of\b",
    r"\bsubject to closing conditions\b", r"\bdefinitive agreement\b"
]

# For PR Newswire, use the same sets
PRN_POSITIVE = POSITIVE_KEYWORDS
PRN_NEGATIVE = NEGATIVE_TITLE_KEYWORDS

# Regex to extract an exchange-listed ticker symbol from text
TICKER_REGEX = re.compile(
    r"\b\((NYSE|NASDAQ|AMEX|TSX(?:V)?|TSXV|OTCQB|OTCQX):\s*([A-Z\.\-]+)\)\b"
)

# Patterns to extract an offer price from free text
PRICE_PATTERNS = [
    r"for\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
    r"at\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
    r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s*per share",
    r"consideration of\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)"
]

# === STATE TRACKING ===
sent_links = set()
latest_pubdate = {}  # will store the most recent publication datetime per feed

# === UTILITIES ===

def send_telegram_message(text: str):
    """Send a message through the Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    resp = requests.post(url, data=payload)
    if resp.status_code != 200:
        print(f"❌ Telegram error {resp.status_code}: {resp.text}")

def get_current_price(ticker: str):
    """Fetch the current market price for the given ticker via yfinance."""
    try:
        info = yf.Ticker(ticker).info
        return info.get("regularMarketPrice")
    except Exception as e:
        print(f"⚠️ Error fetching price for {ticker}: {e}")
        return None

def extract_ticker(text: str):
    """Extract the ticker symbol (e.g., AAPL) from the given text."""
    match = TICKER_REGEX.search(text)
    return match.group(2) if match else None

def extract_offer_price(text: str):
    """Extract the offer price from text using predefined regex patterns."""
    for pattern in PRICE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None

def initialize_pubdates():
    """Initialize cutoff timestamps so we only process new entries from now on."""
    now = datetime.now(timezone.utc)
    for feed in SEC_FEEDS:
        latest_pubdate[feed["type"]] = now
    latest_pubdate["PRN"] = now

# === FEED PARSING ===

def parse_sec_feeds():
    """Parse all configured SEC feeds and send Telegram alerts for new deals."""
    for feed in SEC_FEEDS:
        ftype = feed["type"]
        data = feedparser.parse(feed["url"])
        for entry in data.entries:
            link = entry.link
            pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
            if pub <= latest_pubdate[ftype] or link in sent_links:
                continue

            title = (entry.title or "").strip()
            title_lower = title.lower()
            # Skip if title contains negative keywords
            if any(re.search(p, title_lower) for p in NEGATIVE_TITLE_KEYWORDS):
                continue
            # Require at least one positive keyword in the title
            if not any(re.search(p, title_lower) for p in POSITIVE_KEYWORDS):
                continue

            # Extract data from the summary/description
            description = BeautifulSoup(getattr(entry, "summary", ""), "html.parser").get_text()
            ticker = extract_ticker(description)
            if not ticker:
                continue

            current_price = get_current_price(ticker)
            offer_price = extract_offer_price(description)
            premium = None
            if current_price and offer_price:
                try:
                    premium = (offer_price - current_price) / current_price * 100
                except ZeroDivisionError:
                    premium = None

            # Build notification message
            lines = [
                f"📢 *New M&A Announcement (SEC – {ftype})!*",
                f"🏢 *Title:* {title}",
                f"🎯 *Ticker:* {ticker}"
            ]
            if offer_price:
                lines.append(f"💰 *Offer Price:* ${offer_price:.2f}")
            if current_price:
                lines.append(f"📈 *Current Price:* ${current_price:.2f}")
            if premium is not None:
                lines.append(f"🔥 *Premium:* {premium:.1f}%")
            lines += [
                f"📅 *Date:* {pub.isoformat()}",
                f"🔗 [View Filing]({link})"
            ]
            send_telegram_message("\n".join(lines))

            # Update state
            sent_links.add(link)
            latest_pubdate[ftype] = pub

def parse_prn_feed():
    """Parse the PR Newswire RSS feed and send Telegram alerts for new deals."""
    data = feedparser.parse(PRN_RSS_URL)
    for entry in data.entries:
        link = entry.link
        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if pub <= latest_pubdate["PRN"] or link in sent_links:
            continue

        title = (entry.title or "").strip()
        title_lower = title.lower()
        if any(re.search(p, title_lower) for p in PRN_NEGATIVE):
            continue
        if not any(re.search(p, title_lower) for p in PRN_POSITIVE):
            continue

        description = BeautifulSoup(entry.description, "html.parser").get_text()
        ticker = extract_ticker(description)
        if not ticker:
            continue

        current_price = get_current_price(ticker)
        offer_price = extract_offer_price(description)
        premium = None
        if current_price and offer_price:
            try:
                premium = (offer_price - current_price) / current_price * 100
            except ZeroDivisionError:
                premium = None

        lines = [
            "📢 *New M&A Announcement (PR Newswire)!*",
            f"🏢 *Title:* {title}",
            f"🎯 *Ticker:* {ticker}"
        ]
        if offer_price:
            lines.append(f"💰 *Offer Price:* ${offer_price:.2f}")
        if current_price:
            lines.append(f"📈 *Current Price:* ${current_price:.2f}")
        if premium is not None:
            lines.append(f"🔥 *Premium:* {premium:.1f}%")
        lines += [
            f"📅 *Date:* {pub.isoformat()}",
            f"🔗 [Read Article]({link})"
        ]
        send_telegram_message("\n".join(lines))

        sent_links.add(link)
        latest_pubdate["PRN"] = pub

# === MAIN LOOP ===

def run_monitor():
    initialize_pubdates()
    send_telegram_message("🟢 *Bot started*: monitoring SEC filings and PR Newswire M&A in real time 🚀")
    while True:
        try:
            parse_sec_feeds()
            parse_prn_feed()
        except Exception as err:
            print(f"❌ Unexpected error: {err}")
        time.sleep(60)

if __name__ == "__main__":
    run_monitor()
