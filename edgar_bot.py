import feedparser
import requests
import time
import yfinance as yf
import re
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# === CONFIGURATION ===
BOT_TOKEN    = "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI"
CHAT_ID      = "687693382"

# SEC feeds for relevant forms
SEC_FEEDS = [
    {"type": "8-K",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom"},
    {"type": "S-4",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=S-4&output=atom"},
    {"type": "SC TO-C","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-C&output=atom"},
    {"type": "SC 13D","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom"},
    {"type": "DEFM14A","url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=DEFM14A&output=atom"}
]

# PR Newswire M&A-specific feed
PRN_RSS_URL = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/"
    "acquisitions-mergers-and-takeovers-list.rss"
)

# Semantic filters
SEC_ITEMS_MA = ["Item 1.01", "Item 2.01", "Item 3.02", "Item 5.03"]
SEC_KEYWORDS_POS = [
    r"\bacquisition\b", r"\bmerger\b", r"\bwill acquire\b",
    r"\btender offer\b", r"\bexchange offer\b"
]
SEC_KEYWORDS_NEG = [r"\bcompleted\b", r"\bclosed\b", r"\beffective as of\b"]
POSITIVE_PRN = [
    r"\b(acquisition|acquire|merger)\b",
    r"\b(announc(?:e|ed) (?:deal|acquisition))\b"
]
NEGATIVE_PRN = [r"\b(complet(?:ed|ion)|closed|rebalancing)\b"]

# State
sent_links     = set()
latest_pubdate = {f["type"]: None for f in SEC_FEEDS}
latest_pubdate["PRN"] = None

# Utility functions

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


def get_price_from_ticker(ticker: str):
    try:
        return yf.Ticker(ticker).info.get("regularMarketPrice")
    except Exception:
        return None


def extract_ticker(text: str):
    m = re.search(r"\((NYSE|NASDAQ|AMEX):\s*([A-Z\.]+)\)", text)
    return m.group(2) if m else None


def extract_offer_price(text: str):
    # Cerca pattern come 'for $XX.XX', 'at $XX.XX', '$XX.XX per share'
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
                continue
    return None

# Parsing functions

def parse_sec_feed():
    print("🔍 Checking SEC feeds...")
    for feed_info in SEC_FEEDS:
        ftype, url = feed_info["type"], feed_info["url"]
        feed = feedparser.parse(url)
        for entry in feed.entries:
            link, title = entry.link, entry.title
            pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
            if link in sent_links or (latest_pubdate[ftype] and pub <= latest_pubdate[ftype]):
                continue
            title_low = title.lower()
            # 8-K items filter
            if ftype == "8-K" and not any(item.lower() in title_low for item in SEC_ITEMS_MA):
                continue
            # semantic filters
            if not any(re.search(p, title_low) for p in SEC_KEYWORDS_POS):
                continue
            if any(re.search(p, title_low) for p in SEC_KEYWORDS_NEG):
                continue

            # extract details
            company = title.split(" - ")[0].strip()
            # summary may contain price terms
            summary = getattr(entry, 'summary', '') or ''
            desc = BeautifulSoup(summary, 'html.parser').get_text()
            ticker = extract_ticker(desc)
            current_price = get_price_from_ticker(ticker) if ticker else None
            # offered price and premium
            offer_price = extract_offer_price(desc)
            premium = None
            if offer_price and current_price:
                premium = (offer_price - current_price) / current_price * 100
            if not ticker or current_price is None:
                continue

            # build message
            msg = (
                f"📢 *New M&A announcement detected (SEC - {ftype})!*\n"
                f"🏢 *Company:* {company} ({ticker})\n"
            )
            if offer_price:
                msg += f"💰 *Offered price:* ${offer_price:.2f}\n"
            if premium is not None:
                msg += f"🔥 *Premium:* {premium:.1f}%\n"
            msg += f"📈 *Current price:* ${current_price:.2f}\n"
            msg += f"📅 *Date:* {entry.updated}\n"
            msg += f"🔗 [Open Filing]({link})"

            send_telegram_message(msg)
            sent_links.add(link)
            latest_pubdate[ftype] = pub


def parse_prn_feed():
    print("🔍 Checking PR Newswire feed...")
    feed = feedparser.parse(PRN_RSS_URL)
    for entry in feed.entries:
        link, title = entry.link, entry.title
        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if link in sent_links or (latest_pubdate['PRN'] and pub <= latest_pubdate['PRN']):
            continue
        title_low = title.lower()
        if not any(re.search(p, title_low) for p in POSITIVE_PRN):
            continue
        if any(re.search(p, title_low) for p in NEGATIVE_PRN):
            continue

        desc = BeautifulSoup(entry.description, 'html.parser').get_text()
        ticker = extract_ticker(desc)
        if not ticker:
            continue
        current_price = get_price_from_ticker(ticker)
        offer_price = extract_offer_price(desc)
        premium = None
        if offer_price and current_price:
            premium = (offer_price - current_price) / current_price * 100
        if current_price is None:
            continue

        msg = (
            "📢 *New M&A announcement detected (PRNewswire)!*\n"
            f"🏢 *Title:* {title} ({ticker})\n"
        )
        if offer_price:
            msg += f"💰 *Offered price:* ${offer_price:.2f}\n"
        if premium is not None:
            msg += f"🔥 *Premium:* {premium:.1f}%\n"
        msg += f"📈 *Current price:* ${current_price:.2f}\n"
        msg += f"📅 *Date:* {entry.published}\n"
        msg += f"🔗 [Read article]({link})"

        send_telegram_message(msg)
        sent_links.add(link)
        latest_pubdate['PRN'] = pub

# Main loop: re-analizza ogni 60 secondi

def run_monitor():
    print("🚀 M&A Pulse Bot is running...")
    send_telegram_message("🟢 *Bot started: monitoring M&A with price, offer and premium*")
    while True:
        try:
            parse_sec_feed()
            parse_prn_feed()
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
        time.sleep(60)


if __name__ == "__main__":
    run_monitor()
