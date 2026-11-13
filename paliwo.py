import html
import json
import os
import re
import socket
import time
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")
STATE_FILE = Path("paliwo_seen.json")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
NEWS_URL = "https://www.gov.pl/web/energia/wiadomosci?size=50"
GOV_BASE = "https://www.gov.pl"


class LinkParser(HTMLParser):
    """Extract ordinary links from the Ministry's news listing."""
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href, " ".join(self.parts).strip()))
            self.href = None
            self.parts = []


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").casefold()
            if key in {
                "article:published_time", "date", "datepublished",
                "dcterms.created", "dcterms.date",
            } and attrs.get("content"):
                self.values.append(attrs["content"])
        elif tag == "time" and attrs.get("datetime"):
            self.values.append(attrs["datetime"])


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        return html.unescape(" ".join(self.parts))


def fetch_bytes(url, accept="text/html,application/xhtml+xml,*/*"):
    """GET with retries for transient disconnects and HTTP 5xx/429 responses."""
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(1, 6):
        request = Request(
            url,
            headers={
                "Accept": accept,
                "User-Agent": "Mozilla/5.0 (compatible; PB95-price-notifier/1.0)",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urlopen(request, timeout=40) as response:
                return response.read(), response.geturl()
        except HTTPError as exc:
            if exc.code not in retryable_statuses or attempt == 5:
                raise
            last_error = exc
        except (URLError, RemoteDisconnected, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt == 5:
                raise
            last_error = exc
        delay = min(2 ** (attempt - 1), 8)
        print(f"Tymczasowy błąd pobierania ({attempt}/5): {last_error}. Ponawiam za {delay} s.")
        time.sleep(delay)
    raise RuntimeError(f"Nie udało się pobrać {url}: {last_error}")


def parse_date(value):
    if not value:
        return None
    value = str(value).strip()
    iso = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})", value)
    if iso:
        try:
            return date.fromisoformat(iso.group(1))
        except ValueError:
            pass
    polish = re.search(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](20\d{2})(?!\d)", value)
    if polish:
        try:
            return date(int(polish.group(3)), int(polish.group(2)), int(polish.group(1)))
        except ValueError:
            pass
    return None


MONTHS = {
    "stycznia": 1,
    "lutego": 2,
    "marca": 3,
    "kwietnia": 4,
    "maja": 5,
    "czerwca": 6,
    "lipca": 7,
    "sierpnia": 8,
    "września": 9,
    "października": 10,
    "listopada": 11,
    "grudnia": 12,
}
MONTH_PATTERN = "(?:" + "|".join(re.escape(name) for name in MONTHS) + ")"


def parse_polish_month_date(day, month_name, year):
    return date(int(year), MONTHS[month_name.casefold()], int(day))


def extract_validity_period(text, url):
    """Read the explicit validity dates from the Ministry article headline/body."""
    normalized = " ".join(text.replace("\xa0", " ").split()).casefold()

    # Cross-month range, e.g. 31 października - 2 listopada 2026 r.
    cross_month = re.search(
        rf"(?<!\d)(\d{{1,2}})\s+({MONTH_PATTERN})\s*[-–—]\s*"
        rf"(\d{{1,2}})\s+({MONTH_PATTERN})\s+(20\d{{2}})",
        normalized,
    )
    if cross_month:
        start_day = parse_polish_month_date(cross_month.group(1), cross_month.group(2), cross_month.group(5))
        end_day = parse_polish_month_date(cross_month.group(3), cross_month.group(4), cross_month.group(5))
        return start_day, end_day

    # Same-month range, e.g. w dniach 11-12 listopada 2026 r.
    same_month = re.search(
        rf"(?<!\d)(\d{{1,2}})\s*[-–—]\s*(\d{{1,2}})\s+"
        rf"({MONTH_PATTERN})\s+(20\d{{2}})",
        normalized,
    )
    if same_month:
        start_day = parse_polish_month_date(same_month.group(1), same_month.group(3), same_month.group(4))
        end_day = parse_polish_month_date(same_month.group(2), same_month.group(3), same_month.group(4))
        return start_day, end_day

    # Single day, e.g. obowiązująca 10 listopada 2026 r.
    single_day = re.search(
        rf"obowiązuj[a-ząćęłńóśźż]*\s+(?:w\s+dniu\s+)?"
        rf"(\d{{1,2}})\s+({MONTH_PATTERN})\s+(20\d{{2}})",
        normalized,
    )
    if single_day:
        day = parse_polish_month_date(single_day.group(1), single_day.group(2), single_day.group(3))
        return day, day

    # Numeric ranges, e.g. 10.11-12.11 2026.
    numeric_range = re.search(
        r"(?<!\d)(\d{1,2})[./](\d{1,2})\s*[-–—]\s*"
        r"(\d{1,2})[./](\d{1,2})\s+(20\d{2})",
        normalized,
    )
    if numeric_range:
        start_day = date(int(numeric_range.group(5)), int(numeric_range.group(2)), int(numeric_range.group(1)))
        end_day = date(int(numeric_range.group(5)), int(numeric_range.group(4)), int(numeric_range.group(3)))
        return start_day, end_day

    raise ValueError(f"Nie udało się odczytać okresu obowiązywania ceny w komunikacie: {url}")

def parse_article(url):
    raw, final_url = fetch_bytes(url)
    source = raw.decode("utf-8", errors="replace")

    meta = MetadataParser()
    meta.feed(source)
    published = None
    for candidate in meta.values:
        published = parse_date(candidate)
        if published:
            break

    parser = TextParser()
    parser.feed(source)
    article_text = " ".join(parser.get_text().split())

    if not published:
        # On Gov.pl the publication date appears immediately below the article title.
        published = parse_date(article_text)
    if not published:
        # JSON-LD datePublished is used as a final date fallback.
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', source, flags=re.IGNORECASE)
        if match:
            published = parse_date(match.group(1))
    if not published:
        raise ValueError(f"Nie udało się ustalić daty publikacji artykułu: {final_url}")

    price_match = re.search(
        r"benzyna\s*95\s*[-–—:]\s*(\d{1,2}[,.]\d{2})\s*zł\s*/\s*l",
        article_text,
        flags=re.IGNORECASE,
    )
    if not price_match:
        raise ValueError(f"Artykuł z {published.isoformat()} nie zawiera czytelnej ceny PB95: {final_url}")

    price = float(price_match.group(1).replace(",", "."))
    if not (3.0 <= price <= 15.0):
        raise ValueError(f"Odczytana cena PB95 wygląda nieprawidłowo: {price:.2f} zł/l")

    effective_from, effective_to = extract_validity_period(article_text, final_url)

    return {
        "published": published,
        "price": round(price, 2),
        "effective_from": effective_from,
        "effective_to": effective_to,
        "url": final_url,
        "title": final_url.rstrip("/").rsplit("/", 1)[-1],
    }


def fetch_recent_price_announcements():
    raw, _ = fetch_bytes(NEWS_URL)
    listing = LinkParser()
    listing.feed(raw.decode("utf-8", errors="replace"))

    candidates = []
    seen = set()
    for href, label in listing.links:
        absolute = urljoin(GOV_BASE, href)
        if "/web/energia/" not in absolute:
            continue
        if "maksymalna-cena-detaliczna-paliw" not in absolute.casefold():
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        candidates.append(absolute)

    if not candidates:
        raise RuntimeError(
            "Strona aktualności Ministerstwa Energii nie zwróciła linków do komunikatów o maksymalnych cenach paliw."
        )

    announcements = []
    errors = []
    # The first few entries include the newest daily notice and previous price for comparison.
    for url in candidates[:8]:
        try:
            announcements.append(parse_article(url))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            print(f"Pomijam nieczytelny komunikat: {exc}")

    if not announcements:
        raise RuntimeError("Nie udało się odczytać żadnego komunikatu o cenie paliw. " + " | ".join(errors))

    announcements.sort(key=lambda item: item["published"], reverse=True)
    return announcements


def find_applicable_notice(announcements, target_date):
    applicable = [
        item for item in announcements
        if item["effective_from"] <= target_date <= item["effective_to"]
    ]
    if not applicable:
        return None
    # Prefer the newest official notice if the Ministry has published overlapping corrections.
    applicable.sort(key=lambda item: item["published"], reverse=True)
    return applicable[0]


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Brakuje sekretów BOT_TOKEN lub CHAT_ID w GitHub Actions.")
    from urllib.parse import urlencode

    payload = urlencode({
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram zwrócił błąd: {result}")


def main():
    now = datetime.now(TZ)
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    check_only = os.environ.get("CHECK_ONLY", "").casefold() == "true"

    today = now.date()
    tomorrow = today + timedelta(days=1)
    print(
        f"Sprawdzam oficjalne ceny PB95 na dziś ({today.isoformat()}) "
        f"i jutro ({tomorrow.isoformat()})."
    )

    announcements = fetch_recent_price_announcements()
    today_notice = find_applicable_notice(announcements, today)
    tomorrow_notice = find_applicable_notice(announcements, tomorrow)

    if today_notice is None:
        raise RuntimeError(
            f"Nie znalazłem oficjalnego komunikatu określającego cenę PB95 na dziś ({today.isoformat()}). "
            "Nie wysyłam raportu z niepotwierdzonym porównaniem."
        )
    if tomorrow_notice is None:
        print(
            f"Komunikat z ceną PB95 na jutro ({tomorrow.isoformat()}) nie jest jeszcze dostępny. "
            "Nie wysyłam raportu; kolejna zaplanowana próba sprawdzi to ponownie."
        )
        return

    today_price = today_notice["price"]
    tomorrow_price = tomorrow_notice["price"]
    difference = round(tomorrow_price - today_price, 2)

    print(
        f"PB95 dziś: {today_price:.2f} zł/l ({today_notice['effective_from']}–{today_notice['effective_to']}); "
        f"jutro: {tomorrow_price:.2f} zł/l "
        f"({tomorrow_notice['effective_from']}–{tomorrow_notice['effective_to']})."
    )

    if check_only:
        print(
            f"TEST BEZ WYSYŁKI: PB95 jutro {tomorrow_price:.2f} zł/l; "
            f"dziś {today_price:.2f} zł/l; zmiana {difference:+.2f} zł/l; "
            f"źródło: {tomorrow_notice['url']}"
        )
        return

    state = load_state()
    report_for = tomorrow.isoformat()
    if state.get("last_report_for") == report_for:
        print(f"Raport dla {tomorrow.isoformat()} został już wysłany. Pomijam duplikat.")
        return

    if difference > 0:
        change_line = f"📈 Wzrost o {difference:.2f} zł/l względem dzisiaj ({today_price:.2f} zł/l)."
    elif difference < 0:
        change_line = f"📉 Spadek o {abs(difference):.2f} zł/l względem dzisiaj ({today_price:.2f} zł/l)."
    else:
        change_line = f"➖ Bez zmian względem dzisiaj ({today_price:.2f} zł/l)."

    message = (
        "⛽ MAKSYMALNA CENA PALIWA – PB95\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 Cena na jutro: {tomorrow:%d.%m.%Y}\n"
        f"💰 {tomorrow_price:.2f} zł/l\n"
        f"{change_line}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Źródło: Ministerstwo Energii\n"
        f"{tomorrow_notice['url']}"
    )

    # Save the report date only after Telegram confirms delivery.
    send_telegram(message)
    save_state({
        "last_price": tomorrow_price,
        "last_notice": tomorrow_notice["url"],
        "last_report_for": report_for,
        "last_report_price": tomorrow_price,
        "reported_today_price": today_price,
        "sent_at": now.isoformat(),
        "effective_from": tomorrow_notice["effective_from"].isoformat(),
        "effective_to": tomorrow_notice["effective_to"].isoformat(),
    })
    print(
        f"Wysłano dzienny raport PB95 na {tomorrow.isoformat()}: "
        f"{tomorrow_price:.2f} zł/l; zmiana {difference:+.2f} zł/l."
    )

if __name__ == "__main__":
    main()
