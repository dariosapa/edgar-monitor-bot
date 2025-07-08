import feedparser
import requests
import re
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

# === CONFIGURATION ===
BOT_TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"

# Feeds
PRN_RSS_URL = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/"
    "acquisitions-mergers-and-takeovers-list.rss"
)

# Patterns
TICKER_REGEX = re.compile(
    r"\b\((NYSE|NASDAQ|AMEX|TSX(?:V)?|TSXV|OTCQB|OTCQX):\s*([A-Z\.\-]+)\)\b"
)
POSITIVE_PRN = [
    r"\b(completes acquisition of|to acquire|acquires|will acquire)\b"
]
NEGATIVE_PRN = [r"\b(complet(?:ed|ion)|closed|rebalancing)\b"]

# Time window
NOW = datetime.now(timezone.utc)
TWO_DAYS_AGO = NOW - timedelta(days=2)

# Utility
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


def extract_ticker(text: str):
    m = TICKER_REGEX.search(text)
    return m.group(2) if m else None


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

# Parse PR Newswire feed for last 2 days

def parse_prn_last_two_days():
    print("🔍 Parsing PR Newswire feed (last 2 days)...")
    feed = feedparser.parse(PRN_RSS_URL)
    for entry in feed.entries:
        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if pub < TWO_DAYS_AGO:
            continue

        title = entry.title
        print(f"Checking entry: {title}")
        title_low = title.lower()
        if not any(re.search(p, title_low) for p in POSITIVE_PRN):
            continue
        if any(re.search(p, title_low) for p in NEGATIVE_PRN):
            continue

        desc = BeautifulSoup(entry.description, 'html.parser').get_text()
        ticker = extract_ticker(desc)
        if not ticker:
            continue

        offer_price = extract_offer_price(desc)
        msg = (
            "📢 *New M&A (PRNewswire, last 2 days)*\n"
            f"🏢 *Title:* {title} ({ticker})\n"
        )
        if offer_price:
            msg += f"💰 *Offered price:* ${offer_price:.2f}\n"
        msg += f"📅 *Date:* {entry.published}\n🔗 [Read article]({entry.link})"

        send_telegram_message(msg)

if __name__ == "__main__":
    parse_prn_last_two_days()
