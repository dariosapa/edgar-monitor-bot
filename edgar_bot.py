import feedparser
import requests
import time
import yfinance as yf
import re
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# === CONFIGURATION ===
BOT_TOKEN       = "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI"
CHAT_ID         = "687693382"
SEC_RSS_URL     = "https://www.sec.gov/Archives/edgar/usgaap.rss.xml"
PRN_RSS_URL     = "https://www.prnewswire.com/rss/news-releases-list.rss"
HEADERS         = {'User-Agent': 'M&A Pulse Bot (email@example.com)'}
RELEVANT_FORMS  = ["8-K", "SC TO-C", "S-4", "SC 13D", "DEFM14A"]
PRN_KEYWORDS    = ["acquire", "acquisition", "merger", "buyout", "takeover"]

sent_links      = set()
latest_pubdate  = {"SEC": None, "PRN": None}


def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        'chat_id': CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': False
    })
    if resp.status_code != 200:
        print(f"❌ Telegram error: {resp.status_code} – {resp.text}")


def get_price_from_ticker(ticker: str):
    """ Fetch current market price for given ticker """
    try:
        tk = yf.Ticker(ticker)
        price = tk.info.get("regularMarketPrice")
        return price
    except Exception:
        return None


def extract_ticker(text: str):
    """
    Extract a stock ticker in format (NYSE:XYZ) or (NASDAQ:ABC) from text
    """
    m = re.search(r"\((NYSE|NASDAQ|AMEX):\s*([A-Z\.]+)\)", text)
    return m.group(2) if m else None


def parse_sec_feed():
    global latest_pubdate
    print("🔍 Checking SEC feed...")
    feed = feedparser.parse(SEC_RSS_URL)
    for entry in feed.entries[:15]:
        link, title = entry.link, entry.title
        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        # skip old or duplicate
        if link in sent_links or any(latest_pubdate["SEC"] and pub <= latest_pubdate["SEC"] for _ in (0,)):
            continue
        # only M&A forms
        if not any(f in title for f in RELEVANT_FORMS):
            continue

        # extract company name
        company = title.split(" - ")[0].strip()
        # try to find ticker via company name
        ticker, price = None, None
        # SEC filings seldom include ticker in title, so fallback on yahoo search
        try:
            tk_obj = yf.Ticker(company)
            price = tk_obj.info.get("regularMarketPrice")
            ticker = tk_obj.info.get("symbol")
        except Exception:
            pass
        if not ticker or not price:
            # skip if not a listed company
            continue

        # build message
        msg  = "📢 *New M&A announcement detected (SEC)!*\n"
        msg += f"🏢 *Company:* {company} ({ticker})\n"
        msg += f"📅 *Date:* {entry.published}\n"
        msg += f"📈 *Current price:* ${price:.2f}\n"
        msg += f"🔗 [Open Filing]({link})"

        send_telegram_message(msg)
        sent_links.add(link)
        latest_pubdate["SEC"] = pub


def parse_prn_feed():
    global latest_pubdate
    print("🔍 Checking PRNewswire feed...")
    feed = feedparser.parse(PRN_RSS_URL)
    for entry in feed.entries[:15]:
        link, title = entry.link, entry.title
        pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        # skip old or duplicate
        if link in sent_links or any(latest_pubdate["PRN"] and pub <= latest_pubdate["PRN"] for _ in (0,)):
            continue
        # must include M&A keyword
        low = title.lower() + " " + entry.get("description","").lower()
        if not any(k in low for k in PRN_KEYWORDS):
            continue

        # extract ticker from description
        desc   = BeautifulSoup(entry.description, "html.parser").get_text()
        ticker = extract_ticker(desc)
        if not ticker:
            # skip private deals / non‐listed
            continue

        price = get_price_from_ticker(ticker)
        if not price:
            continue

        # build message
        msg  = "📢 *New M&A announcement detected (PRNewswire)!*\n"
        msg += f"🏢 *Title:* {title} ({ticker})\n"
        msg += f"📅 *Date:* {entry.published}\n"
        msg += f"📈 *Current price:* ${price:.2f}\n"
        msg += f"🔗 [Read article]({link})"

        send_telegram_message(msg)
        sent_links.add(link)
        latest_pubdate["PRN"] = pub


def run_monitor():
    print("🚀 M&A Pulse Bot is running...")
    send_telegram_message("🟢 *Bot started: monitoring only listed‐company M&A announcements*")
    while True:
        try:
            parse_sec_feed()
            parse_prn_feed()
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
        time.sleep(60)


if __name__ == "__main__":
    run_monitor()
