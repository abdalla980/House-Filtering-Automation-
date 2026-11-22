"""Berlin housing alert: watches Kleinanzeigen and pushes new
listings under the price limit to Telegram.

Config lives in a .env file next to this script (see .env.example).
Without a Telegram token it runs in console-only mode (prints alerts).
"""
import os
import random
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent


# --- CONFIG ------------------------------------------------------------------
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "180"))
MIN_PRICE = int(os.environ.get("MIN_PRICE", "150"))
MAX_PRICE = int(os.environ.get("MAX_PRICE", "500"))
# true = first run only records current listings, alerts start with new ones
SEED_SILENTLY = os.environ.get("SEED_SILENTLY", "false").lower() == "true"

DEFAULT_URLS = [
    # Mietwohnungen (apartments), offers only
    f"https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/anzeige:angebote/preis:{MIN_PRICE}:{MAX_PRICE}/c203l3331",
    # Auf Zeit & WG (rooms / sublets), offers only
    f"https://www.kleinanzeigen.de/s-auf-zeit-wg/berlin/anzeige:angebote/preis:{MIN_PRICE}:{MAX_PRICE}/c199l3331",
]
# 1-Zimmer flats may cost a bit more (own price cap)
ONE_ROOM_MAX_PRICE = int(os.environ.get("ONE_ROOM_MAX_PRICE", "550"))
ONE_ROOM_URL = (f"https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/anzeige:angebote/"
                f"preis:{MIN_PRICE}:{ONE_ROOM_MAX_PRICE}/c203l3331+wohnung_mieten.zimmer_d:1,1")
DEFAULT_URLS.insert(0, ONE_ROOM_URL)  # first, so a 501-550 1-Zimmer isn't marked seen by another search
PRICE_CAPS = {ONE_ROOM_URL: ONE_ROOM_MAX_PRICE}  # every other search uses MAX_PRICE
# Override with your own search links: comma-separated in .env
KLEINANZEIGEN_URLS = [u.strip() for u in os.environ.get("KLEINANZEIGEN_URLS", "").split(",")
                      if u.strip()] or DEFAULT_URLS

BLOCKED_KEYWORDS = [
    "tausch", "swap", "suche wohnung", "suche wg", "suche zimmer", "suche ein",
    "stellplatz", "garage", "keller", "lager", "nur für frauen",
]
# No short-term lets (careful: must not hit "Etage" or "unbefristet")
SHORT_TERM_RE = re.compile(
    r"zwischenmiet|\bauf zeit\b|(?<!un)befristet|sublet|short[- ]?term|kurzzeit|temporär|temporary"
    r"|wochenende|weekend|ferienwohnung|holiday|monteur|winterschlaf|über den winter|for the winter"
    r"|\b\d+\s*(?:tage|wochen|days|weeks)\b|\b(?:für|for)\s+\d+\s*(?:monate|months)\b"
    r"|\b\d{1,2}\.(?:\d{1,2}\.?)?(?:\d{2,4})?\s*(?:-|–|bis)\s*\d{1,2}\.\d{1,2}"
    r"|\b(?:jan|feb|m[äa]r|apr|ma[iy]|jun|jul|aug|sep|o[ck]t|nov|de[cz])[a-zä]*\.?\s*(?:\d{4}\s*)?"
    r"(?:&|and|und|-|–|bis|to|until)\s*(?:jan|feb|m[äa]r|apr|ma[iy]|jun|jul|aug|sep|o[ck]t|nov|de[cz])", re.I)
# No women-only offers
WOMEN_ONLY_RE = re.compile(
    r"nur\s+(?:für\s+|an\s+)?(?:frauen|frau|mädchen|studentinnen)|an eine frau|mitbewohnerin|weiblich"
    r"|female only|women only|only female|only women|flinta|frauen[- ]?wg", re.I)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*€")

# Windows consoles choke on emoji otherwise
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --- STORAGE -----------------------------------------------------------------
conn = sqlite3.connect(BASE_DIR / "seen_listings.db")
conn.execute("CREATE TABLE IF NOT EXISTS seen (ad_id TEXT PRIMARY KEY, seen_at INTEGER)")
conn.commit()


def is_seen(key: str) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE ad_id = ?", (key,)).fetchone() is not None


def mark_seen(key: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (key, int(time.time())))
    conn.commit()


def db_is_empty() -> bool:
    return conn.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is None


# --- HELPERS -----------------------------------------------------------------
LOG_FILE = BASE_DIR / "monitor.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if sys.stdout:  # None when running headless via pythonw
        print(line)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_price(text: str):
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    return float(m.group(1).replace(".", "").replace(",", "."))


def is_clean(*texts: str) -> bool:
    combined = " ".join(texts).lower()
    return not (any(word in combined for word in BLOCKED_KEYWORDS)
                or SHORT_TERM_RE.search(combined) or WOMEN_ONLY_RE.search(combined))


def fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def send_alert(text: str) -> bool:
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print("\n" + text + "\n" + "-" * 40)
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False},
            timeout=15,
        )
        if r.status_code == 429:  # Telegram rate limit
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
            return False
        if not r.ok:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        log(f"Telegram send failed: {e}")
        return False


def setup_chat_id() -> None:
    """Read the chat id of whoever last messaged the bot and store it in .env."""
    if not TELEGRAM_TOKEN:
        sys.exit("Put TELEGRAM_TOKEN in .env first.")
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=15)
    r.raise_for_status()
    chats = [u["message"]["chat"] for u in r.json().get("result", []) if "message" in u]
    if not chats:
        sys.exit("No messages found. Send your bot any message in Telegram, then rerun.")
    chat_id = str(chats[-1]["id"])
    env = BASE_DIR / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith("TELEGRAM_CHAT_ID=")]
    lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved TELEGRAM_CHAT_ID={chat_id} ({chats[-1].get('first_name', '')}) to .env")


# --- SCRAPERS (each yields dicts) -------------------------------------------
def scrape_kleinanzeigen(url: str):
    soup = fetch(url)
    for art in soup.select("article[data-adid]"):
        ad_id = art.get("data-adid")
        href = art.get("data-href")
        title_el = art.find("h3")
        if not (ad_id and href and title_el):
            continue
        location = next((s.get_text(" ", strip=True) for s in art.find_all("span")
                         if re.match(r"\d{5}\b", s.get_text(strip=True))), "Berlin")
        paras = [p.get_text(" ", strip=True) for p in art.find_all("p")]
        price_text = next((p for p in paras if PRICE_RE.search(p)), "")
        size = next((p for p in paras if "m²" in p and len(p) < 40), "")
        desc = max(paras, key=len, default="")
        yield {
            "key": f"ka:{ad_id}",
            "source": "Kleinanzeigen",
            "title": title_el.get_text(" ", strip=True),
            "price_text": price_text or "N/A",
            "price": parse_price(price_text),
            "location": location,
            "size": size,
            "desc": desc,
            "link": "https://www.kleinanzeigen.de" + href,
        }


# --- MAIN LOOP ---------------------------------------------------------------
def format_msg(item: dict) -> str:
    lines = [f"🏠 New {item['source']} listing", "", f"📌 {item['title']}",
             f"💶 {item['price_text']}", f"📍 {item['location']}"]
    if item["size"]:
        lines.append(f"📐 {item['size']}")
    if item["desc"]:
        lines.append(f"📝 {item['desc'][:180].rstrip('.… ')}…")
    lines += ["", f"🔗 {item['link']}"]
    return "\n".join(lines)


def run_once(silent: bool = False) -> int:
    sources = [(u, lambda u=u: scrape_kleinanzeigen(u)) for u in KLEINANZEIGEN_URLS]
    sent = 0
    for name, scraper in sources:
        try:
            items = list(scraper())
        except Exception as e:
            log(f"Error fetching {name}: {e}")
            continue
        for item in items:
            if is_seen(item["key"]):
                continue
            price = item["price"]
            cap = PRICE_CAPS.get(name, MAX_PRICE)
            wanted = (price is None or MIN_PRICE <= price <= cap) and \
                is_clean(item["title"], item["desc"])
            if not wanted or silent:
                mark_seen(item["key"])
                continue
            if send_alert(format_msg(item)):  # only mark seen once delivered
                mark_seen(item["key"])
                sent += 1
                time.sleep(1)
    return sent


def main():
    if "--setup-chat" in sys.argv:
        setup_chat_id()
        return
    telegram = bool(TELEGRAM_TOKEN and CHAT_ID)
    mode = "Telegram" if telegram else "console only (no TELEGRAM_TOKEN/CHAT_ID set)"
    log(f"Monitoring {len(KLEINANZEIGEN_URLS)} Kleinanzeigen search(es), €{MIN_PRICE}-{MAX_PRICE}, "
        f"every ~{CHECK_INTERVAL_SECONDS}s → {mode}")
    if "--once" in sys.argv:
        if os.environ.get("GITHUB_ACTIONS") and not telegram:
            sys.exit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID secrets missing - refusing to run.")
        log(f"Sent {run_once()} alert(s).")
        return
    global _lock  # keep the socket alive for the whole process
    _lock = socket.socket()
    try:
        _lock.bind(("127.0.0.1", 47831))
    except OSError:
        log("Another monitor instance is already running. Exiting.")
        sys.exit(0)
    if not telegram and not sys.stdout:
        log("Headless run without Telegram credentials: nothing would reach you. Exiting.")
        sys.exit(1)
    if SEED_SILENTLY and db_is_empty():
        run_once(silent=True)
        log("First run: recorded current listings silently; alerts start with new ones.")
    if telegram:
        send_alert(f"✅ Berlin housing monitor started — watching {len(KLEINANZEIGEN_URLS)} "
                   f"search(es), €{MIN_PRICE}-{MAX_PRICE}.")
    while True:
        try:
            n = run_once()
            log(f"check done, {n} new alert(s)")
        except Exception as e:  # never let one bad check kill the 24/7 loop
            log(f"Unexpected error during check: {e!r}")
        # jitter so requests don't arrive on an exact clock
        time.sleep(max(60, CHECK_INTERVAL_SECONDS + random.randint(-30, 30)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
