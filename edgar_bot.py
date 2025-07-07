import feedparser
import requests
import time
import yfinance as yf
import re
from bs4 import BeautifulSoup

# === CONFIGURATION ===
BOT_TOKEN = "7210512521:AAHMMoqnVfGP-3T2drsOvUi_FgXmxfTiNgI"
CHAT_ID = "687693382"
RELEVANT_FORMS = ["8-K", "SC TO-C", "S-4", "SC 13D", "DEFM14A"]
SEC_RSS_URL = "https://www.sec.gov/Archives/edgar/usgaap.rss.xml"
PRN_RSS_URL = "https://www.prnewswire.com/rss/news-releases-list.rss"
HEADERS = {'User-Agent': 'M&A Pulse Bot (email@example.com)'}

sent_links = set()

def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    resp = requests.post(url, data=payload)
    if resp.status_code != 200:
        print(f"Telegram error: {resp.status_code} – {resp.text}")

def extract_price_info(filing_url: str):
    try:
        html = requests.get(filing_url, headers=HEADERS).text
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text(separator=' ')
        price_match = re.search(r"(?:offer(?:s|ed)?|pay(?:s|ing)?|price(?:d)? at)\s+\$([\d\.]+)", text, re.IGNORECASE)
        price_offered = float(price_match.group(1)) if price_match else None
        premium_match = re.search(r"premium of\s+([\d\.]+)%", text, re.IGNORECASE)
        premium_pct = float(premium_match.group(1)) if premium_match else None
        return price_offered, premium_pct
    except Exception as e:
        print(f"Error parsing filing: {e}")
        return None, None

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
        data = requests.get(query_url).json()
        if data.get("quotes"):
            symbol = data["quotes"][0]["symbol"]
            price = yf.Ticker(symbol).info.get("regularMarketPrice")
            return price, symbol
    except Exception:
        pass
    return None, None

def check_edgar_feed():
    print("🔎 Checking SEC feed...")
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
                msg = "📢 *New M&A announcement detected (SEC)!*\n"
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
                break

def check_prnewswire_feed():
    print("🔎 Checking PRNewswire feed...")
    feed = feedparser.parse(PRN_RSS_URL)
    for entry in feed.entries[:20]:
        title = entry.title.lower()
        summary = entry.get("summary", "").lower()
        link = entry.link
        published = entry.published
        if link in sent_links:
            continue
        if any(kw in title or kw in summary for kw in ["acquisition", "merger", "buyout", "takeover", "to acquire"]):
            msg = "📢 *New M&A announcement detected (PRNewswire)!*\n"
            msg += f"📰 *Title:* {entry.title}\n"
            msg += f"📅 *Date:* {published}\n"
            msg += f"🔗 [Read article]({link})"
            send_telegram_message(msg)
            sent_links.add(link)

def run_monitor():
    print("🚀 M&A Pulse Bot is running...")
    send_telegram_message("✅ *Bot avviato con successo!*")
    while True:
        check_edgar_feed()
        check_prnewswire_feed()
        time.sleep(60)

if __name__ == "__main__":
    run_monitor()
