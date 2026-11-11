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

    return {
        "published": published,
        "price": round(price, 2),
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


def latest_todays_notice(today):
    announcements = fetch_recent_price_announcements()
    eligible = [item for item in announcements if item["published"] <= today]
    if not eligible:
        print(f"Nie znaleziono komunikatu paliwowego opublikowanego najpóźniej {today.isoformat()}.")
        return None

    # Process the newest available official notice even when GitHub starts the job late.
    # This lets a delayed scheduled run catch up instead of silently missing a price change.
    current = eligible[0]
    previous = eligible[1] if len(eligible) > 1 else None
    current["previous_price"] = previous["price"] if previous else None
    current["promulgation"] = current["published"].isoformat()
    current["year"] = current["published"].year
    current["pos"] = None
    print(
        f"Najnowszy oficjalny komunikat Ministerstwa Energii: {current['published'].isoformat()}, "
        f"PB95 {current['price']:.2f} zł/l."
    )
    if previous:
        print(
            f"Poprzedni komunikat ({previous['published'].isoformat()}): "
            f"PB95 {previous['price']:.2f} zł/l."
        )
    return current


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
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    check_only = os.environ.get("CHECK_ONLY", "").casefold() == "true"
    manual = event == "workflow_dispatch"

    # Run no earlier than 16:25 in Warsaw. If GitHub delays the scheduled job,
    # later runs may still process the newest notice and catch up without duplicates.
    if not (manual or check_only) and (now.hour < 16 or (now.hour == 16 and now.minute < 25)):
        print(f"Za wcześnie na sprawdzenie (teraz {now:%H:%M} Europe/Warsaw). Koniec.")
        return

    today = now.date()
    print(f"Sprawdzam oficjalny komunikat Ministerstwa Energii z dnia {today.isoformat()}.")
    notice = latest_todays_notice(today)
    if not notice:
        return

    price = notice["price"]
    notice_id = notice["url"]
    effective_date = notice["published"] + timedelta(days=1)

    if check_only:
        print(
            f"TEST BEZ WYSYŁKI: PB95 {price:.2f} zł/l; "
            f"poprzednia cena: {notice.get('previous_price')}; "
            f"obowiązuje od {effective_date.isoformat()}; źródło: {notice_id}"
        )
        return

    state = load_state()
    previous_price = state.get("last_price")
    if previous_price is None:
        previous_price = notice.get("previous_price")

    if notice_id == state.get("last_notice"):
        print("Ten komunikat został już obsłużony. Bez powiadomienia.")
        return

    if previous_price is None:
        # First successful run: establish a safe baseline rather than sending a false alert.
        save_state({
            "last_price": price,
            "last_notice": notice_id,
            "last_checked_at": now.isoformat(),
            "effective_from": effective_date.isoformat(),
        })
        print(f"Zapisano cenę bazową PB95 {price:.2f} zł/l. Bez powiadomienia.")
        return

    difference = round(price - float(previous_price), 2)
    if difference == 0:
        save_state({
            "last_price": price,
            "last_notice": notice_id,
            "last_checked_at": now.isoformat(),
            "effective_from": effective_date.isoformat(),
        })
        print(f"PB95 bez zmiany ({price:.2f} zł/l). Bez powiadomienia.")
        return

    if difference > 0:
        change_line = f"▲ Drożej o {difference:.2f} zł/l"
    else:
        change_line = f"▼ Taniej o {abs(difference):.2f} zł/l"

    message = (
        "⛽ ZMIANA MAKSYMALNEJ CENY PALIWA – PB95\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 Obowiązuje od: {effective_date:%d.%m.%Y}\n"
        f"💰 Nowa cena maksymalna: {price:.2f} zł/l\n"
        f"{change_line} (poprzednio {float(previous_price):.2f} zł/l)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Źródło: Ministerstwo Energii\n"
        f"{notice_id}"
    )

    # Save only after Telegram confirms delivery, so a temporary Telegram outage cannot lose an alert.
    send_telegram(message)
    save_state({
        "last_price": price,
        "last_notice": notice_id,
        "sent_at": now.isoformat(),
        "effective_from": effective_date.isoformat(),
    })
    print(f"Wysłano powiadomienie: PB95 {price:.2f} zł/l, od {effective_date.isoformat()}.")


if __name__ == "__main__":
    main()
