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
HEADERS = {'User-Agent': 'M&A Pulse Bot (email@example.com)'}

# memory of already sent links to avoid duplicates
sent_links = set()

def send_telegram_message(message: str):
    """Send a Markdown-formatted message to the Telegram chat."""
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
    """
    Download the filing HTML and extract:
    - offer price per share (e.g. "$54.00")
    - declared premium percentage (e.g. "22%")
    """
    try:
        html = requests.get(filing_url, headers=HEADERS).text
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text(separator=' ')

        # Search for patterns like "offer price at $XX.XX" or "will pay $XX.XX"
        price_match = re.search(r"(?:offer(?:s|ed)?|pay(?:s|ing)?|price(?:d)? at)\s+\$([\d\.]+)", text, re.IGNORECASE)
        price_offered = float(price_match.group(1)) if price_match else None

        # Search for "premium of XX%"
        premium_match = re.search(r"premium of\s+([\d\.]+)%", text, re.IGNORECASE)
        premium_pct = float(premium_match.group(1)) if premium_match else None

        return price_offered, premium_pct
    except Exception as e:
        print(f"Error parsing filing: {e}")
        return None, None

def get_price_from_company_name(name: str):
    """
    Try to resolve the company's ticker from its name and fetch current market price.
    Returns (current_price, ticker) or (None, None).
    """
    try:
        # Directly try name as ticker
        ticker_obj = yf.Ticker(name)
        price = ticker_obj.info.get('regularMarketPrice')
        symbol = ticker_obj.info.get('symbol')
        if price:
            return price, symbol
    except Exception:
        pass

    try:
        # Fallback: search via Yahoo Finance search API
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
    """Check the EDGAR RSS feed for relevant M&A filings."""
    feed = feedparser.parse(SEC_RSS_URL)
    for entry in feed.entries[:15]:
        title = entry.title
        link = entry.link
        published = entry.published

        # Skip if already sent
        if link in sent_links:
            continue

        for form in RELEVANT_FORMS:
            if form in title:
                company = title.split(" - ")[0].strip()
                price_offered, declared_premium = extract_price_info(link)
                current_price, ticker = get_price_from_company_name(company)

                # Build the message
                msg = "📢 *New M&A announcement detected!*\n"
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

def run_monitor():
    """Main loop: check EDGAR every 60 seconds."""
    while True:
        print("🔍 Checking EDGAR feed...")
        check_edgar_feed()
        time.sleep(60)

if __name__ == "__main__":
    run_monitor()

