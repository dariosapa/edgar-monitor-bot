import feedparser
import requests
import time
import yfinance as yf
import re
import json
from bs4 import BeautifulSoup

# === CONFIGURATION ===
BOT_TOKEN = "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI"
CHAT_ID = "687693382"

SEC_RSS_URL = "https://www.sec.gov/Archives/edgar/usgaap.rss.xml"
RELEVANT_FORMS = ["8-K", "SC TO-C", "S-4", "SC 13D", "DEFM14A"]

PRN_RSS_URL = "https://www.prnewswire.com/rss/finance-business-news.rss"
PRN_KEYWORDS = ["acquires", "acquisition", "buyout", "merger", "merging", "combine", "offer", "purchase"]

HEADERS = {'User-Agent': 'M&A Pulse Bot (email@example.com)'}
LINKS_FILE = "sent_links.json"
sent_links = set()

# === STORAGE ===
def load_sent_links():
    global sent_links
    try:
        with open(LINKS_FILE, "r") as f:
            sent_links.update(json.load(f))
    except FileNotFoundError:
        pass

def save_sent_links():
    with open(LINKS_FILE, "w") as f:
        json.dump(list(sent_links), f)

# === TELEGRAM ===
def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': True
    }
    resp = requests.post(url, data=payload)
    if resp.status_code != 200:
        print(f"❌ Telegram error: {resp.status_code} – {resp.text}")
    else:
        print("✅ Telegram message sent.")

# === PARSE FILING HTML ===
def extract_price_info(filing_url: str):
    try:
        html = requests.get(filing_url, headers=HEADERS, timeout=10).text
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text(separator=' ')

        price_match = re.search(r"(?:offer(?:s|ed)?|pay(?:s|ing)?|price(?:d)? at)\s+\$([\d\.]+)", text, re.IGNORECASE)
        price_offered = float(price_match.group(1)) if price_match else None

        premium_match = re.search(r"premium of\s+([\d\.]+)%", text, re.IGNORECASE)
        premium_pct = float(premium_match.group(1)) if premium_match else None

        return price_offered, premium_pct
    except Exception as e:
        print(f"❌ Error parsing filing: {e}")
        return None, None

# === YAHOO PRICE LOOKUP ===
def get_price_from_company_name(name: str):
    try:
        ticker_obj = yf.Ticker(name)
        price = ticker_obj.info.get('regularMarketPrice')
        symbol = ticker_obj.info.get('symbol')
        if price:
            return price, symbol
    except Exception:
        pass

    try:
        query_url = f"https://query2.finance.yahoo.com/v1/finance/search?q={name}&lang=en-US"
        data = requests.get(query_url, timeout=5).json()
        if data.get("quotes"):
            symbol = data["quotes"][0]["symbol"]
            price = yf.Ticker(symbol).info.get("regularMarketPrice")
            return price, symbol
    except Exception:
        pass

    return None, None

# === SEC MONITOR ===
def check_edgar_feed():
    try:
        feed = feedparser.parse(SEC_RSS_URL)
        for entry in feed.entries[:15]:
            title = entry.title
            link = entry.link
            published = entry.published

            if link in sent_links:
                continue

            for form in RELEVANT_FORMS:
                if form in title:
                    company = title.split(" - ")[0].strip()
                    price_offered, declared_premium = extract_price_info(link)
                    current_price, ticker = get_price_from_company_name(company)

                    msg = "📢 *New M&A announcement detected from SEC!*\n"
                    msg += f"📌 *Filing:* `{form}`\n"
                    msg += f"🏢 *Company:* {company}"
                    if ticker:
                        msg += f" ({ticker})"
                    msg += f"\n📅 *Date:* {published}"

                    if price_offered:
                        msg += f"\n💰 *Offer price:* ${price_offered:.2f}"
                    if current_price:
                        msg += f"\n📈 *Current price:* ${current_price:.2f}"
                    if declared_premium is not None:
                        msg += f"\n💹 *Stated premium:* +{declared_premium:.2f}%"
                    elif price_offered and current_price:
                        calc_premium = ((price_offered - current_price) / current_price) * 100
                        msg += f"\n💹 *Estimated premium:* +{calc_premium:.2f}%"

                    msg += f"\n🔗 [Open Filing]({link})"

                    send_telegram_message(msg)
                    sent_links.add(link)
                    save_sent_links()
                    print(f"✅ SEC alert sent: {company} – {form}")
                    break
    except Exception as e:
        print(f"❌ Error reading SEC feed: {e}")

# === PRNEWSWIRE MONITOR ===
def check_prn_feed():
    try:
        feed = feedparser.parse(PRN_RSS_URL)
        for entry in feed.entries[:20]:
            title = entry.title.lower()
            link = entry.link

            if link in sent_links:
                continue

            for keyword in PRN_KEYWORDS:
                if keyword in title:
                    msg = "📰 *Potential M&A news from PR Newswire!*\n"
                    msg += f"🔑 *Keyword:* `{keyword}`\n"
                    msg += f"🗞️ *Title:* {entry.title}\n"
                    msg += f"🔗 [Read More]({link})"

                    send_telegram_message(msg)
                    sent_links.add(link)
                    save_sent_links()
                    print(f"✅ PRNewswire alert sent: {entry.title}")
                    break
    except Exception as e:
        print(f"❌ Error reading PRNewswire feed: {e}")

# === MAIN LOOP ===
def run_monitor():
    while True:
        try:
            print("🔍 Checking SEC feed...")
            check_edgar_feed()
            print("🔍 Checking PRNewswire feed...")
            check_prn_feed()
        except Exception as e:
            print(f"❌ Unexpected error in main loop: {e}")
        time.sleep(60)

# === START ===
if __name__ == "__main__":
    print("🚀 M&A Pulse Bot is running...")
    load_sent_links()
    send_telegram_message("🟢 *Bot started: monitoring SEC + PRNewswire*")
    run_monitor()
