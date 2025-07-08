# edgar_bot.py
import feedparser
import requests
import re
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

# === CONFIGURATION ===
BOT_TOKEN = "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI"
CHAT_ID = "687693382"

# PR Newswire M&A feed
PRN_RSS_URL = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/"
    "acquisitions-mergers-and-takeovers-list.rss"
)

# Regex patterns for ticker extraction and semantic filtering
TICKER_REGEX = re.compile(
    r"\b\((NYSE|NASDAQ|AMEX|TSX(?:V)?|TSXV|OTCQB|OTCQX):\s*([A-Z\.\-]+)\)\b"
)
POSITIVE_PRN = [
    r"\b(completes acquisition of|to acquire|acquires|will acquire)\b"
]
NEGATIVE_PRN = [
    r"\b(complet(?:ed|ion)|closed|rebalancing)\b"
]

# Time window: last 2 days
NOW = datetime.now(timezone.utc)
TWO_DAYS_AGO = NOW - timedelta(days=2)

# Utility: send Telegram messages

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

# Extract stock ticker from text

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

# Parse PR Newswire feed for entries in the last 2 days

def parse_prn_last_two_days():
    feed = feedparser.parse(PRN_RSS_URL)
    for entry in feed.entries:
        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if pub < TWO_DAYS_AGO:
            continue

        title = entry.title
        title_low = title.lower()
        # Semantic filters
        if not any(re.search(p, title_low) for p in POSITIVE_PRN):
            continue
        if any(re.search(p, title_low) for p in NEGATIVE_PRN):
            continue

        desc = BeautifulSoup(entry.description, 'html.parser').get_text()
        ticker = extract_ticker(desc)
        if not ticker:
            continue  # skip non-public targets

        offer_price = extract_offer_price(desc)

        # Compose and send message
        msg = (
            "📢 *New M&A (PRNewswire, last 2 days)*\n"
            f"🏢 *Title:* {title} ({ticker})\n"
        )
        if offer_price:
            msg += f"💰 *Offered price:* ${offer_price:.2f}\n"
        msg += (
            f"📅 *Date:* {entry.published}\n"
            f"🔗 [Read article]({entry.link})"
        )

        send_telegram_message(msg)

# Entry point
if __name__ == "__main__":
    parse_prn_last_two_days()
