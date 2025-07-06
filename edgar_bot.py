import os
import time
import re
import feedparser
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

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

sent_links = set()

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    })
    if r.status_code != 200:
        print(f"[Telegram] Error {r.status_code}: {r.text}", flush=True)

def extract_price_info(url: str):
    try:
        html = requests.get(url, headers=HEADERS, timeout=10).text
        text = BeautifulSoup(html, "html.parser").get_text(" ")
        pm = re.search(r"(?:offer(?:s|ed)?|pay(?:s|ing)?|price(?:d)? at)\s+\$([\d\.]+)", text, re.IGNORECASE)
        dm = re.search(r"premium of\s+([\d\.]+)%", text, re.IGNORECASE)
        return (float(pm.group(1)), float(dm.group(1))) if pm and dm else (None, None)
    except Exception as e:
        print(f"[extract_price_info] {e}", flush=True)
        return None, None

def get_current_price_and_ticker(name: str):
    try:
        t = yf.Ticker(name)
        info = t.info
        p = info.get("regularMarketPrice"); s = info.get("symbol")
        if p: return p, s
    except: pass
    try:
        data = requests.get(
            f"https://query2.finance.yahoo.com/v1/finance/search?q={name}&lang=en-US",
            timeout=10
        ).json()
        if data.get("quotes"):
            sym = data["quotes"][0]["symbol"]
            p = yf.Ticker(sym).info.get("regularMarketPrice")
            return p, sym
    except: pass
    return None, None

def check_all_feeds():
    now = datetime.now(timezone.utc).isoformat()
    for market, cfg in FEEDS.items():
        try:
            feed = feedparser.parse(cfg["url"])
        except Exception as e:
            print(f"[{market}] Feed error: {e}", flush=True)
            continue

        for entry in (feed.entries or [])[:10]:
            link      = entry.link
            title     = entry.title
            published = getattr(entry, "published", "")

            if link in sent_links:
                continue

            block = title + " " + getattr(entry, "summary", "")
            if not any(kw.lower() in block.lower() for kw in cfg["keywords"]):
                continue

            price_offered, declared_premium = extract_price_info(link)
            company = re.split(r"\s[-–]\s", title)[0].strip()
            current_price, ticker = get_current_price_and_ticker(company)

            msg  = f"{now} 📢 *{market} M&A Alert!*\n"
            msg += f"📌 *Title:* {title}\n"
            msg += f"📅 *Date:* {published}\n"
            if price_offered is not None:
                msg += f"💰 *Offer price:* ${price_offered:.2f}\n"
            if current_price is not None:
                msg += f"📈 *Current price:* ${current_price:.2f}\n"
            if declared_premium is not None:
                msg += f"💹 *Stated premium:* +{declared_premium:.2f}%\n"
            elif price_offered and current_price:
                est = (price_offered - current_price) / current_price * 100
                msg += f"💹 *Estimated premium:* +{est:.2f}%\n"
            msg += f"🔗 [Open announcement]({link})"

            send_telegram_message(msg)
            sent_links.add(link)

        time.sleep(1)

def monitor_loop():
    while True:
        print(f"{datetime.now(timezone.utc).isoformat()} ▶️ Polling all feeds...", flush=True)
        try:
            check_all_feeds()
        except Exception as e:
            print(f"[monitor_loop] Error: {e}", flush=True)
        time.sleep(60)

# -- keep container alive with a simple HTTP server --

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server():
    port = int(os.getenv("PORT", "8000"))
    srv = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"🔌 Health server listening on port {port}", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    # start monitoring in a daemon thread
    t = Thread(target=monitor_loop, daemon=True)
    t.start()
    # start HTTP server (blocks main thread)
    start_health_server()
