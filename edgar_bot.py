#!/usr/bin/env python3
import feedparser
import requests
import re
yfinance = None
import yfinance as yf
import time
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# === CONFIGURATION ===
BOT_TOKEN = "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI"
CHAT_ID = "687693382"

# SEC feeds for relevant forms
SEC_FEEDS = [
    {"type": "8-K",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"type": "S-4",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"type": "SC TO-C","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"type": "SC 13D","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"type": "DEFM14A","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"}
]

# PR Newswire M&A feed
PRN_RSS_URL = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/"
    "acquisitions-mergers-and-takeovers-list.rss"
)

# Semantic filters for SEC
SEC_ITEMS_MA = ["Item 1.01", "Item 2.01", "Item 3.02", "Item 5.03"]
SEC_KEYWORDS_POS = [r"\bacquisition\b", r"\bmerger\b", r"\bwill acquire\b", r"\btender offer\b", r"\bexchange offer\b"]
SEC_KEYWORDS_NEG = [r"\bcompleted\b", r"\bclosed\b", r"\beffective as of\b"]

# Filters for PR Newswire: require public ticker
TICKER_REGEX = re.compile(r"\b\((NYSE|NASDAQ|AMEX|TSX(?:V)?|TSXV|OTCQB|OTCQX):\s*([A-Z\.\-]+)\)\b")
pos_prn = [r"\b(completes acquisition of|to acquire|acquires|will acquire)\b"]
neg_prn = [r"\b(complet(?:ed|ion)|closed|rebalancing)\b"]

# State tracking
sent_links = set()
latest_pubdate = {f["type"]: None for f in SEC_FEEDS}
latest_pubdate["PRN"] = None

# Utility: send message to Telegram

def send_telegram_message(msg: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        'chat_id': CHAT_ID,
        'text': msg,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': False
    })
    if resp.status_code != 200:
        print(f"❌ Telegram error: {resp.status_code} – {resp.text}")

# Get current market price from yfinance

def get_current_price(ticker: str):
    try:
        info = yf.Ticker(ticker).info
        return info.get('regularMarketPrice')
    except Exception as e:
        print(f"Error fetching price for {ticker}: {e}")
        return None

# Extract ticker symbol from text

def extract_ticker(text: str):
    m = TICKER_REGEX.search(text)
    return m.group(2) if m else None

# Extract offer price from description

def extract_offer_price(text: str):
    patterns = [
        r"for\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
        r"at\s*\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
        r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s*per share"
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except:
                pass
    return None

# Initialize latest_pubdate to skip old entries

def initialize_pubdates():
    # SEC feeds
    for feed in SEC_FEEDS:
        f = feedparser.parse(feed["url"])
        if f.entries:
            e = f.entries[0]
            latest_pubdate[feed["type"]] = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
    # PR Newswire
    f = feedparser.parse(PRN_RSS_URL)
    if f.entries:
        e = f.entries[0]
        latest_pubdate["PRN"] = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)

# Parse SEC feeds

def parse_sec_feeds():
    for feed in SEC_FEEDS:
        ftype = feed["type"]
        f = feedparser.parse(feed["url"])
        for e in f.entries:
            link = e.link
            pub = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
            if (latest_pubdate[ftype] and pub <= latest_pubdate[ftype]) or link in sent_links:
                continue
            title = e.title or ""
            title_low = title.lower()
            if ftype == "8-K" and not any(item.lower() in title_low for item in SEC_ITEMS_MA):
                continue
            if not any(re.search(p, title_low) for p in SEC_KEYWORDS_POS):
                continue
            if any(re.search(p, title_low) for p in SEC_KEYWORDS_NEG):
                continue
            desc = BeautifulSoup(getattr(e, 'summary', ''), 'html.parser').get_text()
            ticker = extract_ticker(desc)
            if not ticker:
                continue
            current = get_current_price(ticker)
            offer = extract_offer_price(desc)
            premium = None
            if current and offer:
                try:
                    premium = (offer - current) / current * 100
                except:
                    premium = None
            msg = (
                f"📢 *New M&A (SEC - {ftype})!*\n"
                f"🏢 *Title:* {title}\n"
                f"🎯 *Ticker:* {ticker}\n"
            )
            if offer:
                msg += f"💰 *Offer Price:* ${offer:.2f}\n"
            if current:
                msg += f"📈 *Current Price:* ${current:.2f}\n"
            if premium is not None:
                msg += f"🔥 *Premium:* {premium:.1f}%\n"
            msg += f"📅 *Date:* {e.updated}\n🔗 [Open Filing]({link})"
            send_telegram_message(msg)
            sent_links.add(link)
            latest_pubdate[ftype] = pub

# Parse PR Newswire feed

def parse_prn_feed():
    f = feedparser.parse(PRN_RSS_URL)
    for e in f.entries:
        link = e.link
        pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        if (latest_pubdate["PRN"] and pub <= latest_pubdate["PRN"]) or link in sent_links:
            continue
        title = e.title or ""
        title_low = title.lower()
        if not any(re.search(p, title_low) for p in pos_prn):
            continue
        if any(re.search(p, title_low) for p in neg_prn):
            continue
        desc = BeautifulSoup(e.description, 'html.parser').get_text()
        ticker = extract_ticker(desc)
        if not ticker:
            continue
        current = get_current_price(ticker)
        offer = extract_offer_price(desc)
        premium = None
        if current and offer:
            try:
                premium = (offer - current) / current * 100
            except:
                premium = None
        msg = (
            "📢 *New M&A (PRNewswire)!*\n"
            f"🏢 *Title:* {title}\n"
            f"🎯 *Ticker:* {ticker}\n"
        )
        if offer:
            msg += f"💰 *Offer Price:* ${offer:.2f}\n"
        if current:
            msg += f"📈 *Current Price:* ${current:.2f}\n"
        if premium is not None:
            msg += f"🔥 *Premium:* {premium:.1f}%\n"
        msg += f"📅 *Date:* {e.published}\n🔗 [Read Article]({link})"
        send_telegram_message(msg)
        sent_links.add(link)
        latest_pubdate["PRN"] = pub

# Main monitoring loop

def run_monitor():
    initialize_pubdates()
    send_telegram_message("🟢 *Bot started*: monitoring SEC filings and PR Newswire M&A 🚀")
    while True:
        try:
            parse_sec_feeds()
            parse_prn_feed()
        except Exception as err:
            print(f"❌ Error: {err}")
        time.sleep(60)

if __name__ == "__main__":
    run_monitor()
