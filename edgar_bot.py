#!/usr/bin/env python3
import time
import re
import os
import requests
import feedparser
from bs4 import BeautifulSoup
import yfinance as yf
import logging
from datetime import datetime, timezone

# === CONFIGURATION ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI")
CHAT_ID   = os.getenv("CHAT_ID",   "687693382")

SEC_FEEDS = [
    {"type": "8-K",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"type": "S-4",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"type": "SC TO-C","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"type": "SC 13D","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"type": "DEFM14A","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"},
]

PRN_RSS_URL = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/"
    "acquisitions-mergers-and-takeovers-list.rss"
)

POSITIVE_KEYWORDS = [
    r"\bacquisition\b", r"\bmerger\b", r"\bwill acquire\b", r"\bto acquire\b",
    r"\bacquires\b", r"\bbuyout\b", r"\btakeover\b", r"\bmerger of equals\b",
    r"\bstock[- ]for[- ]stock\b", r"\btender offer\b", r"\bexchange offer\b"
]
NEGATIVE_TITLE_KEYWORDS = [
    r"\bcompleted\b", r"\bcompletion\b", r"\bclosed\b", r"\beffective as of\b",
    r"\bsubject to closing conditions\b", r"\bdefinitive agreement\b"
]

PRN_POSITIVE = POSITIVE_KEYWORDS
PRN_NEGATIVE = NEGATIVE_TITLE_KEYWORDS

TICKER_REGEX = re.compile(
    r"\b\((NYSE|NASDAQ|AMEX|TSX(?:V)?|TSXV|OTCQB|OTCQX):\s*([A-Z\.\-]+)\)\b"
)

PRICE_PATTERNS = [
    r"for\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
    r"at\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
    r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s*per share",
    r"consideration of\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)"
]

sent_links = set()
latest_pubdate = {}

# === LOGGING SETUP ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ"
)

# === UTILITIES ===

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    resp = requests.post(url, data=payload)
    if resp.status_code != 200:
        logging.error(f"Telegram error {resp.status_code}: {resp.text}")

def get_current_price(ticker: str):
    try:
        return yf.Ticker(ticker).info.get("regularMarketPrice")
    except Exception as e:
        logging.warning(f"Error fetching price for {ticker}: {e}")
        return None

def extract_ticker(text: str):
    m = TICKER_REGEX.search(text)
    return m.group(2) if m else None

def extract_offer_price(text: str):
    for pat in PRICE_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None

def initialize_pubdates():
    now = datetime.now(timezone.utc)
    for feed in SEC_FEEDS:
        latest_pubdate[feed["type"]] = now
    latest_pubdate["PRN"] = now

# === PARSING ===

def parse_sec_feeds():
    for feed in SEC_FEEDS:
        ftype = feed["type"]
        logging.info(f"Checking SEC feed: {ftype}")
        data = feedparser.parse(feed["url"])
        for entry in data.entries:
            link = entry.link
            pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
            if pub <= latest_pubdate[ftype] or link in sent_links:
                continue

            title = (entry.title or "").strip()
            tl = title.lower()
            if any(re.search(p, tl) for p in NEGATIVE_TITLE_KEYWORDS):
                continue
            if not any(re.search(p, tl) for p in POSITIVE_KEYWORDS):
                continue

            desc = BeautifulSoup(getattr(entry, "summary", ""), "html.parser").get_text()
            ticker = extract_ticker(desc)
            if not ticker:
                continue

            current_price = get_current_price(ticker)
            offer_price = extract_offer_price(desc)
            premium = None
            if current_price and offer_price:
                try:
                    premium = (offer_price - current_price) / current_price * 100
                except ZeroDivisionError:
                    pass

            msg = [
                f"📢 *New M&A Announcement (SEC – {ftype})!*",
                f"🏢 *Title:* {title}",
                f"🎯 *Ticker:* {ticker}"
            ]
            if offer_price:
                msg.append(f"💰 *Offer Price:* ${offer_price:.2f}")
            if current_price:
                msg.append(f"📈 *Current Price:* ${current_price:.2f}")
            if premium is not None:
                msg.append(f"🔥 *Premium:* {premium:.1f}%")
            msg.extend([
                f"📅 *Date:* {pub.isoformat()}",
                f"🔗 [View Filing]({link})"
            ])
            send_telegram_message("\n".join(msg))

            sent_links.add(link)
            latest_pubdate[ftype] = pub

def parse_prn_feed():
    logging.info("Checking PR Newswire feed")
    data = feedparser.parse(PRN_RSS_URL)
    for entry in data.entries:
        link = entry.link
        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if pub <= latest_pubdate["PRN"] or link in sent_links:
            continue

        title = (entry.title or "").strip()
        tl = title.lower()
        if any(re.search(p, tl) for p in PRN_NEGATIVE):
            continue
        if not any(re.search(p, tl) for p in PRN_POSITIVE):
            continue

        desc = BeautifulSoup(entry.description, "html.parser").get_text()
        ticker = extract_ticker(desc)
        if not ticker:
            continue

        current_price = get_current_price(ticker)
        offer_price = extract_offer_price(desc)
        premium = None
        if current_price and offer_price:
            try:
                premium = (offer_price - current_price) / current_price * 100
            except ZeroDivisionError:
                pass

        msg = [
            "📢 *New M&A Announcement (PR Newswire)!*",
            f"🏢 *Title:* {title}",
            f"🎯 *Ticker:* {ticker}"
        ]
        if offer_price:
            msg.append(f"💰 *Offer Price:* ${offer_price:.2f}")
        if current_price:
            msg.append(f"📈 *Current Price:* ${current_price:.2f}")
        if premium is not None:
            msg.append(f"🔥 *Premium:* {premium:.1f}%")
        msg.extend([
            f"📅 *Date:* {pub.isoformat()}",
            f"🔗 [Read Article]({link})"
        ])
        send_telegram_message("\n".join(msg))

        sent_links.add(link)
        latest_pubdate["PRN"] = pub

# === MAIN LOOP ===

def run_monitor():
    initialize_pubdates()
    logging.info("Bot initialized, sending start notification")
    send_telegram_message("🟢 *Bot started*: monitoring SEC & PR Newswire every 60s 🚀")
    while True:
        try:
            parse_sec_feeds()
            parse_prn_feed()
        except Exception:
            logging.exception("Error during feed parsing")
        logging.info("Sleeping for 60 seconds")
        time.sleep(60)

if __name__ == "__main__":
    run_monitor()
