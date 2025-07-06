import os
import time
import threading
import re
import feedparser
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime
from fastapi import FastAPI
import uvicorn

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
    """Send a Markdown-formatted message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    })
    if resp.status_code != 200:
        print(f"[Telegram] Error {resp.status_code}: {resp.text}", flush=True)

def extract_price_info(url: str):
    """Download page and extract offered price and declared premium, if present."""
    try:
        html = requests.get(url, headers=HEADERS, timeout=10).text
        text = BeautifulSoup(html, "html.parser").get_text(" ")
        price_m = re.search(r"(?:offer(?:s|ed)?|pay(?:s|ing)?|price(?:d)? at)\s+\$([\d\.]+)", text, re.IGNORECASE)
        prem_m  = re.search(r"premium of\s+([\d\.]+)%", text, re.IGNORECASE)
        price_offered   = float(price_m.group(1)) if price_m else None
        declared_premium = float(prem_m.group(1)) if prem_m else None
        return price_offered, declared_premium
    except Exception as e:
        print(f"[extract_price_info] {e}", flush=True)
        return None, None

def get_current_price_and_ticker(name: str):
    """Resolve ticker by name via yfinance and fetch current market price."""
    try:
        t = yf.Ticker(name)
        info = t.info
        p = info.get("regularMarketPrice")
        s = info.get("symbol")
        if p: return p, s
    except: pass
    try:
        resp = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={name}", timeout=10).json()
        if resp.get("quotes"):
            sym = resp["quotes"][0]["symbol"]
            p = yf.Ticker(sym).info.get("regularMarketPrice")
            return p, sym
    except: pass
    return None, None

def check_all_feeds():
    """Poll each feed, filter for M&A, extract data, and send Telegram alerts."""
    now = datetime.utcnow().isoformat() + "Z"
    for market, cfg in FEEDS.items():
        try:
            feed = feedparser.parse(cfg["url"])
            entries = getattr(feed, "entries", [])
        except Exception as e:
            print(f"[{market}] Feed error: {e}", flush=True)
            continue

        for entry in entries[:10]:
            link      = entry.link
            title     = entry.title
            published = getattr(entry, "published", "")

            if link in sent_links:
                continue
            text_block = title + " " + getattr(entry, "summary", "")
            if not any(kw.lower() in text_block.lower() for kw in cfg["keywords"]):
                continue

            price_offered, declared_premium = extract_price_info(link)
            company = re.split(r"\s[-–]\s", title)[0].strip()
            current_price, ticker = get_current_price_and_ticker(company)

            msg  = f"{now} 📢 *{market} M&A Alert!* \n"
            msg += f"📌 *Title:* {title}\n"
            msg += f"📅 *Date:* {published}\n"
            if price_offered   is not None: msg += f"💰 *Offer price:* ${price_offered:.2f}\n"
            if current_price   is not None: msg += f"📈 *Current price:* ${current_price:.2f}\n"
            if declared_premium is not None:
                msg += f"💹 *Stated premium:* +{declared_premium:.2f}%\n"
            elif price_offered and current_price:
                est = (price_offered - current_price) / current_price * 100
                msg += f"💹 *Estimated premium:* +{est:.2f}%\n"
            msg += f"🔗 [Open announcement]({link})"

            send_telegram_message(msg)
            sent_links.add(link)

        time.sleep(1)  # throttle between feeds

def monitor_loop():
    """Continuously poll all feeds every minute."""
    while True:
        print(f"{datetime.utcnow().isoformat()}Z ▶️ Polling all feeds...", flush=True)
        try:
            check_all_feeds()
        except Exception as e:
            print(f"[monitor_loop] Error: {e}", flush=True)
        time.sleep(60)

app = FastAPI()

@app.get("/")
async def health_check():
    return {"status": "running"}

if __name__ == "__main__":
    # run monitor in background thread
    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()
    # serve a simple HTTP endpoint to keep container alive
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
