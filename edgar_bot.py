import feedparser
import requests
import time
import yfinance as yf
import re
from bs4 import BeautifulSoup

# === CONFIGURATION ===
BOT_TOKEN = "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI"
CHAT_ID   = "687693382"
HEADERS   = {'User-Agent': 'M&A Pulse Bot (email@example.com)'}

# Feeds and their keywords/forms to filter M&A announcements
FEEDS = {
    "USA (SEC EDGAR)": {
        "url": "https://www.sec.gov/Archives/edgar/usgaap.rss.xml",
        "keywords": ["8-K", "SC TO-C", "S-4", "SC 13D", "DEFM14A"]
    },
    "Canada (SEDAR+)": {
        "url": "https://www.sedarplus.ca/files?format=rss",
        "keywords": ["acquisition", "merger", "offer"]
    },
    "UK (LSE RNS)": {
        "url": "https://www.londonstockexchange.com/rss/news.rss",
        "keywords": ["acquisition", "merger", "offer"]
    },
    "Australia (ASX)": {
        "url": "https://www.asx.com.au/asx/statistics/rssAnnounce.xml",
        "keywords": ["acquisition", "merger", "offer"]
    },
    "Hong Kong (HKEX)": {
        "url": "https://www1.hkexnews.hk/rss/ListedCompanyNew.xml",
        "keywords": ["acquisition", "merger", "offer"]
    },
    "PR Newswire": {
        "url": "https://www.prnewswire.com/rss/news-releases/finance-and-business",
        "keywords": ["acquisition", "merger", "offer"]
    },
    "BusinessWire": {
        "url": "https://www.businesswire.com/portal/site/home/rss/announcements.xml",
        "keywords": ["acquisition", "merger", "offer"]
    }
}

# track links already notified
sent_links = set()

def send_telegram_message(text: str):
    """Send a Markdown-formatted message via Telegram."""
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(api_url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    })
    if resp.status_code != 200:
        print(f"Telegram error {resp.status_code}: {resp.text}", flush=True)

def extract_price_info(url: str):
    """
    Download the announcement page and extract:
      - offer price per share
      - declared premium percentage
    Returns (price_offered, declared_premium) or (None, None).
    """
    try:
        html = requests.get(url, headers=HEADERS, timeout=10).text
        text = BeautifulSoup(html, "html.parser").get_text(separator=" ")
        price_m = re.search(r"(?:offer(?:s|ed)?|pay(?:s|ing)?|price(?:d)? at)\s+\$([\d\.]+)", text, re.IGNORECASE)
        premium_m = re.search(r"premium of\s+([\d\.]+)%", text, re.IGNORECASE)
        price_offered = float(price_m.group(1)) if price_m else None
        declared_premium = float(premium_m.group(1)) if premium_m else None
        return price_offered, declared_premium
    except Exception as e:
        print(f"[extract_price_info] Error: {e}", flush=True)
        return None, None

def get_current_price_and_ticker(company_name: str):
    """
    Resolve ticker by name and fetch current market price.
    Returns (current_price, ticker) or (None, None).
    """
    # try direct ticker lookup
    try:
        t = yf.Ticker(company_name)
        info = t.info
        price = info.get("regularMarketPrice")
        symbol = info.get("symbol")
        if price:
            return price, symbol
    except Exception:
        pass

    # fallback to Yahoo search API
    try:
        resp = requests.get(
            f"https://query2.finance.yahoo.com/v1/finance/search?q={company_name}&lang=en-US",
            timeout=10
        ).json()
        if resp.get("quotes"):
            symbol = resp["quotes"][0]["symbol"]
            price = yf.Ticker(symbol).info.get("regularMarketPrice")
            return price, symbol
    except Exception:
        pass

    return None, None

def check_all_feeds():
    """Poll each RSS feed, filter M&A announcements, extract data, and notify."""
    for market, cfg in FEEDS.items():
        try:
            feed = feedparser.parse(cfg["url"])
            entries = getattr(feed, "entries", [])
        except Exception as e:
            print(f"[{market}] Failed to fetch or parse feed: {e}", flush=True)
            continue

        for entry in entries[:10]:
            link = entry.link
            title = entry.title
            published = getattr(entry, "published", "")

            if link in sent_links:
                continue

            text_block = title + " " + getattr(entry, "summary", "")
            if not any(kw.lower() in text_block.lower() for kw in cfg["keywords"]):
                continue

            # We have a potential M&A announcement
            price_offered, declared_premium = extract_price_info(link)
            company_name = re.split(r"\s[-–]\s", title)[0].strip()
            current_price, ticker = get_current_price_and_ticker(company_name)

            # build the Telegram message
            msg  = f"📢 *{market} M&A Alert!* \n"
            msg += f"📌 *Title:* {title}\n"
            msg += f"📅 *Date:* {published}\n"
            if price_offered is not None:
                msg += f"💰 *Offer price:* ${price_offered:.2f}\n"
            if current_price is not None:
                msg += f"📈 *Current price:* ${current_price:.2f}\n"
            if declared_premium is not None:
                msg += f"💹 *Stated premium:* +{declared_premium:.2f}%\n"
            elif price_offered is not None and current_price is not None:
                est = (price_offered - current_price) / current_price * 100
                msg += f"💹 *Estimated premium:* +{est:.2f}%\n"
            msg += f"🔗 [Open announcement]({link})"

            send_telegram_message(msg)
            sent_links.add(link)

        time.sleep(1)  # throttle between feeds

def run_monitor():
    """Main loop: poll all feeds every 60 seconds, with error isolation."""
    while True:
        try:
            print("▶️ Polling all configured feeds...", flush=True)
            check_all_feeds()
        except Exception as e:
            print(f"[run_monitor] Unexpected error: {e}", flush=True)
        time.sleep(60)

if __name__ == "__main__":
    run_monitor()
