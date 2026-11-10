import html
import io
import socket
import time
import json
import os
import re
import sys
from datetime import datetime, timedelta
from http.client import RemoteDisconnected
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from pypdf import PdfReader
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Warsaw")
STATE_FILE = Path("paliwo_seen.json")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
API_BASE = "https://api.sejm.gov.pl/eli"


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        return " ".join(self.parts)


def fetch_bytes(url, accept="*/*"):
    """GET with retries for temporary disconnects and HTTP 5xx/429 responses."""
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(1, 6):
        request = Request(url, headers={"Accept": accept, "User-Agent": "PB95-price-notifier/1.0"})
        try:
            with urlopen(request, timeout=40) as response:
                return response.read()
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


def get_json(url):
    raw = fetch_bytes(url, "application/json")
    return json.loads(raw.decode("utf-8"))


def get_text(url):
    raw = fetch_bytes(url, "text/html,application/xhtml+xml,*/*")
    parser = TextExtractor()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return html.unescape(parser.get_text())


def get_pdf_text(url):
    raw = fetch_bytes(url, "application/pdf,*/*")
    reader = PdfReader(io.BytesIO(raw))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text.strip()) < 80:
        raise ValueError("PDF nie zawiera wystarczającej ilości tekstu do odczytu.")
    return text


def pdf_url_for_notice(year, position):
    # Oficjalny endpoint ELI dla tekstu aktu w PDF.
    return f"{API_BASE}/acts/MP/{year}/{position}/text.pdf"


def latest_todays_notice(today):
    # Najpierw sprawdzamy oficjalne API ELI po dacie publikacji.
    params = urlencode({
        "publisher": "MP",
        "year": today.year,
        "title": "maksymalnej ceny paliw",
        "pubDateFrom": today.isoformat(),
        "pubDateTo": today.isoformat(),
        "limit": 100,
    })
    items = []
    try:
        data = get_json(f"{API_BASE}/acts/search?{params}")
        items = data.get("items", [])
        print(f"ELI search: {len(items)} wyników.")
    except Exception as exc:
        print(f"ELI search chwilowo niedostępne: {exc}. Przechodzę do rocznej listy aktów.")

    def filter_today(records):
        return [
            item for item in records
            if "maksymalnej ceny paliw" in item.get("title", "").casefold()
            and item.get("promulgation") == today.isoformat()
        ]

    matches = filter_today(items)
    annual_items = []
    if not matches:
        print("Wyszukiwarka nie zwróciła dzisiejszego aktu – sprawdzam roczną listę MP.")
        try:
            annual = get_json(f"{API_BASE}/acts/MP/{today.year}")
            annual_items = annual.get("items", [])
            print(f"ELI annual list: {len(annual_items)} aktów; totalCount={annual.get('totalCount', '?')}.")
            matches = filter_today(annual_items)
        except Exception as exc:
            print(f"ELI annual list chwilowo niedostępna: {exc}. Spróbuję sprawdzić numerację PDF na podstawie ostatnio znanej pozycji.")
            annual_items = []

    if matches:
        matches.sort(key=lambda item: item.get("pos", 0), reverse=True)
        for item in matches:
            print(
                "Znalezione obwieszczenie:", item.get("displayAddress"),
                "| data publikacji:", item.get("promulgation"),
                "| data wydania:", item.get("announcementDate"),
            )
        return matches[0]

    # API sejm.gov.pl może mieć opóźnienie względem publikacji na stronie ELI.
    # Sprawdzamy kilka kolejnych numerów pozycji bezpośrednio w oficjalnym repozytorium PDF.
    latest_position = max((item.get("pos", 0) for item in annual_items), default=1120)
    polish_months = {
        1: "stycznia", 2: "lutego", 3: "marca", 4: "kwietnia",
        5: "maja", 6: "czerwca", 7: "lipca", 8: "sierpnia",
        9: "września", 10: "października", 11: "listopada", 12: "grudnia",
    }
    expected_date = f"z dnia {today.day} {polish_months[today.month]} {today.year} r."
    for candidate_pos in range(latest_position + 1, latest_position + 8):
        pdf_url = pdf_url_for_notice(today.year, candidate_pos)
        try:
            pdf_text = get_pdf_text(pdf_url)
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        except Exception as exc:
            print(f"Nie udało się sprawdzić PDF pozycji {candidate_pos}: {exc}")
            continue

        normalized = " ".join(pdf_text.casefold().split())
        if "maksymalnej ceny paliw ciekłych na stacji paliw" not in normalized:
            continue
        if expected_date not in normalized:
            print(f"Pozycja {candidate_pos} to obwieszczenie paliwowe, ale z inną datą niż {today.isoformat()}.")
            continue

        print(f"Znaleziono dzisiejsze obwieszczenie w oficjalnym PDF: M.P. {today.year} poz. {candidate_pos}.")
        return {
            "year": today.year,
            "pos": candidate_pos,
            "promulgation": today.isoformat(),
            "announcementDate": today.isoformat(),
            "title": "Obwieszczenie Ministra Energii w sprawie maksymalnej ceny paliw ciekłych na stacji paliw",
            "displayAddress": f"M.P. {today.year} poz. {candidate_pos}",
            "ELI": f"MP/{today.year}/{candidate_pos}",
            "_document_text": pdf_text,
        }

    print(f"Nie znaleziono obwieszczenia CPN z datą {today.isoformat()} w API ani w kolejnych pozycjach PDF.")
    return None


def extract_pb95_gross(document_text):
    normalized = " ".join(document_text.replace("\xa0", " ").split())
    # Wycinamy wyłącznie punkt dotyczący benzyny 95, aż do punktu dla PB98.
    start = re.search(
        r"benzyny?\s+silnikowej\s+bezołowiowej\s+95",
        normalized,
        flags=re.IGNORECASE,
    )
    if not start:
        raise ValueError("Nie znaleziono w obwieszczeniu sekcji dotyczącej benzyny 95.")

    rest = normalized[start.start():]
    next_section = re.search(
        r"\b2\s*[.)]\s*dla\s+benzyny?\s+silnikowej\s+bezołowiowej\s+98",
        rest,
        flags=re.IGNORECASE,
    )
    section = rest[:next_section.start()] if next_section else rest[:700]

    # W obwieszczeniu lit. a) to cena bez VAT, a lit. b) – cena brutto.
    gross = re.search(
        r"b\s*[.)].{0,180}?powiększona\s+o\s+podatek.{0,100}?wynosi\s+(\d+[,.]\d{2})\s*zł",
        section,
        flags=re.IGNORECASE,
    )
    if gross:
        return float(gross.group(1).replace(",", "."))

    # Awaryjnie: z dwóch kwot w punkcie PB95 wybierz drugą (brutto).
    prices = re.findall(r"(\d+[,.]\d{2})\s*zł", section, flags=re.IGNORECASE)
    if len(prices) >= 2:
        return float(prices[1].replace(",", "."))
    raise ValueError("Nie udało się odczytać ceny brutto PB95 z obwieszczenia.")

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
    import urllib.parse

    payload = urllib.parse.urlencode({
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

    # Dwa wywołania cron (14:30 i 15:30 UTC) obsługują czas letni i zimowy.
    # Pracujemy tylko o 16:30 czasu polskiego; ręczne uruchomienie omija ten warunek.
    if not manual and not (now.hour == 16 and 25 <= now.minute <= 59):
        print(f"Poza oknem wysyłki (teraz {now:%H:%M} Europe/Warsaw). Koniec.")
        return

    today = now.date()
    print(f"Sprawdzam obwieszczenie Monitora Polskiego z dnia {today.isoformat()}.")

    notice = latest_todays_notice(today)
    if not notice:
        print("Brak nowego obwieszczenia CPN z dzisiejszą datą.")
        if manual:
            # Ręczne uruchomienie służy też do sprawdzenia Telegrama i sekretów.
            send_telegram(
                "🧪 TEST TELEGRAMA – BOT PB95\n"
                "Skrypt uruchomił się poprawnie i dotarł do funkcji wysyłającej.\n"
                "Nie znaleziono jeszcze nowego obwieszczenia w Monitorze Polskim.\n"
                "To wiadomość diagnostyczna, nie powiadomienie o nowej cenie."
            )
            print("Wysłano diagnostyczny test Telegrama (uruchomienie ręczne).")
        else:
            print("Uruchomienie automatyczne: brak powiadomienia.")
        return

    year = notice["year"]
    position = notice["pos"]
    document_text = notice.get("_document_text")
    if not document_text:
        document_url = f"{API_BASE}/acts/MP/{year}/{position}/text.html"
        try:
            document_text = get_text(document_url)
        except Exception as exc:
            print(f"Nie udało się pobrać wersji HTML ({exc}); próbuję oficjalny PDF.")
            document_text = get_pdf_text(pdf_url_for_notice(year, position))
    price = extract_pb95_gross(document_text)

    state = load_state()
    previous_price = state.get("last_price")
    notice_id = notice.get("ELI") or notice.get("displayAddress")

    # Powiadamiamy o każdym nowym obwieszczeniu, nawet jeśli cena liczbowa się nie zmieniła.
    if notice_id and notice_id == state.get("last_notice"):
        print("To obwieszczenie zostało już obsłużone. Bez powiadomienia.")
        return

    # Nowe obwieszczenie z identyczną ceną nie wymaga alertu, ale zapisujemy jego ID.
    if previous_price is not None and round(float(previous_price), 2) == round(price, 2):
        state["last_notice"] = notice_id
        state["last_checked_at"] = now.isoformat()
        save_state(state)
        print(f"Nowe obwieszczenie, ale PB95 bez zmiany ({price:.2f} zł/l). Bez powiadomienia.")
        return
    effective_date = datetime.fromisoformat(notice["promulgation"]).date() + timedelta(days=1)
    if previous_price is None:
        change_line = "Pierwsza odczytana cena w monitoringu."
    else:
        difference = price - float(previous_price)
        if difference > 0:
            change_line = f"▲ Drożej o {difference:.2f} zł/l"
        elif difference < 0:
            change_line = f"▼ Taniej o {abs(difference):.2f} zł/l"
        else:
            change_line = "Cena bez zmian względem poprzedniego obwieszczenia."

    message = (
        "⛽ NOWA CENA PALIWA – PB95\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 Obowiązuje od: {effective_date:%d.%m.%Y}\n"
        f"💰 Cena maksymalna: {price:.2f} zł/l\n"
        f"{change_line}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Źródło: Monitor Polski – obwieszczenie Ministra Energii\n"
        f"https://www.monitorpolski.gov.pl/MP/{year}/{position}"
    )

    # Najpierw wysyłka, dopiero potem zapis stanu – awaria Telegrama nie zgubi powiadomienia.
    send_telegram(message)
    state = {
        "last_price": round(price, 2),
        "last_notice": notice_id,
        "sent_at": now.isoformat(),
        "effective_from": effective_date.isoformat(),
    }
    save_state(state)
    print(f"Wysłano PB95 {price:.2f} zł/l, obowiązuje od {effective_date.isoformat()}.")


if __name__ == "__main__":
    main()
