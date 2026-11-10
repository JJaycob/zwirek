import html
import json
import os
import re
import sys
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
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


def get_json(url):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "PB95-price-notifier/1.0"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url):
    request = Request(url, headers={"User-Agent": "PB95-price-notifier/1.0"})
    with urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8")
    parser = TextExtractor()
    parser.feed(raw)
    return html.unescape(parser.get_text())


def latest_todays_notice(today):
    params = urlencode({
        "publisher": "MP",
        "year": today.year,
        "title": "maksymalnej ceny paliw",
        "dateFrom": today.isoformat(),
        "dateTo": today.isoformat(),
        "limit": 100,
    })
    data = get_json(f"{API_BASE}/acts/search?{params}")
    items = data.get("items", [])
    matches = [
        item for item in items
        if "maksymalnej ceny paliw" in item.get("title", "").casefold()
        and item.get("promulgation") == today.isoformat()
    ]
    return matches[0] if matches else None


def extract_pb95_gross(document_text):
    normalized = " ".join(document_text.split())
    section = re.search(
        r"benzyny?\s+silnikowej\s+bezołowiowej\s+95(.*?)(?=2\s*[.)])",
        normalized,
        flags=re.IGNORECASE,
    )
    if not section:
        raise ValueError("Nie znaleziono sekcji dotyczącej benzyny 95 w obwieszczeniu.")

    prices = re.findall(r"(\d+[,.]\d{2})\s*zł", section.group(1), flags=re.IGNORECASE)
    if len(prices) < 2:
        raise ValueError("Nie udało się odczytać ceny brutto PB95 z obwieszczenia.")

    # Pierwsza wartość jest przed VAT, druga to cena detaliczna brutto.
    return float(prices[1].replace(",", "."))


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
        print("Brak nowego obwieszczenia CPN z dzisiejszą datą. Nie wysyłam wiadomości.")
        return

    year = notice["year"]
    position = notice["pos"]
    document_url = f"{API_BASE}/acts/MP/{year}/{position}/text.html"
    document_text = get_text(document_url)
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
        f"https://monitorpolski.gov.pl/{notice.get('address', '')}.pdf"
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
