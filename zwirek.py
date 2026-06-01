import os
import re
import json
import hashlib
import requests
from bs4 import BeautifulSoup

# 🔐 Zmienne środowiskowe (GitHub Secrets)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

URL = "https://twoja-gazetka.pl/produkty/zwirek"
MAX_PRICE = 24.99

STATE_FILE = "seen.json"

# 🔥 Śmieci do usunięcia z tekstu wiadomości (ZAWSZE MAŁYMI LITERAMI)
BAD_WORDS = [
    "reklama", 
    "zobacz ulotkę", 
    "przegląd cen", 
    "sprzedawca", 
    "opis ważność cena"
]

# 🏪 Znane sieci handlowe
STORES = ["dino", "netto", "biedronka", "lidl", "aldi", "kaufland", "carrefour", "auchan"]


def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ Brak BOT_TOKEN lub CHAT_ID")
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown"
            },
            timeout=20
        )
    except Exception as e:
        print(f"❌ Błąd Telegram: {e}")


def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, indent=2)
        print("💾 Pomyślnie zaktualizowano bazę danych seen.json")
    except Exception as e:
        print(f"⚠️ Błąd zapisu pliku stanu: {e}")


def make_id(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def clean_text(text):
    text = " ".join(text.split())
    for bad in BAD_WORDS:
        text = text.replace(bad, "")
    return " ".join(text.split())


def main():
    print("Pobieranie strony...")
    r = requests.get(URL, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    raw_text = soup.get_text(separator=" ")
    
    # Zamieniamy na małe litery i czyścimy wielokrotne spacje
    clean_text_raw = " ".join(raw_text.lower().split())

    seen = load_seen()
    has_updates = False

    # 1. Szukamy wszystkich dat (punkt startowy każdej promocji)
    date_matches = list(re.finditer(r"(\d{1,2}\.\d{2}\s*-\s*\d{1,2}\.\d{2}(?:\.\d{4})?)", clean_text_raw))
    
    print(f"Znaleziono dat w tekście: {len(date_matches)}")

    for i, date_match in enumerate(date_matches):
        start_pos = date_match.start()
        
        # Ustalamy koniec bloku - albo do następnej daty, albo max 300 znaków w przód
        if i + 1 < len(date_matches):
            end_pos = min(date_matches[i+1].start(), start_pos + 300)
        else:
            end_pos = start_pos + 300

        # Wycinamy fragment tekstu dla jednego produktu
        chunk = clean_text_raw[start_pos:end_pos]

        # 2. Szukamy ceny w tym konkretnym wycinku
        price_match = re.search(r"(\d+,\d+)\s*zł", chunk)
        if not price_match:
            continue

        price = float(price_match.group(1).replace(",", "."))
        if price > MAX_PRICE:
            continue

        # 🛑 POTĘŻNA BLOKADA ANTY-SEO/STOPKA 🛑
        # Jeśli w wycinku tekstu są te frazy, to na 100% śmieć z dołu strony, a nie produkt!
        if "cena może nie" in chunk or "bez vat" in chunk or "najniższą cenę" in chunk or "w obecnej promocji" in chunk:
            print("⏭️ Pominięto sekcję SEO / podsumowanie z dołu strony.")
            continue

        # 3. Wycinamy opis, który znajduje się PO cenie
        price_end_pos = price_match.end()
        raw_description = chunk[price_end_pos:].strip()

        # Obcinamy opis na pierwszej lepszej nazwie kolejnego sklepu
        clean_description = raw_description
        for store in STORES:
            if store in clean_description:
                clean_description = clean_description.split(store)[0].strip()

        # 🔥 WYWOŁANIE TWOJEGO CZYSZCZENIA REKLAM I ULOTEK
        clean_description = clean_text(clean_description)

        # 4. Ustalamy jaki to sklep, cofając się w tekście przed datę
        text_before_date = clean_text_raw[max(0, start_pos - 60):start_pos]
        
        store_found = "NIEZNANY SKLEP"
        closest_index = -1
        for store in STORES:
            idx = text_before_date.rfind(store)
            if idx > closest_index:
                closest_index = idx
                store_found = store.upper()

        # Jeśli po wyczyszczeniu opis jest za krótki, ignorujemy
        if len(clean_description) < 3:
            continue

        # Czyścimy końcówki opisu z przecinków i spacji
        clean_description = clean_description.strip(",. -*")

        # Generujemy unikalne ID (sklep + cena + opis)
        product_id = make_id(f"{store_found}_{price}_{clean_description}")

        if product_id in seen:
            continue

        # Formatowanie wiadomości na Telegram
        msg = (
            f"🐱 *PROMOCJA ŻWIREK*\n"
            f"------------------------\n"
            f"🏪 *Sklep:* {store_found}\n"
            f"📅 *Ważność:* {date_match.group(1)}\n"
            f"📝 *Produkt:* {clean_description.capitalize()}\n"
            f"💰 *Cena:* {price:.2f} zł\n"
            f"------------------------"
        )

        send_telegram(msg)
        seen.add(product_id)
        has_updates = True
        
        print(f"✅ Wysłano ({store_found}): {clean_description} za {price} zł")

    if has_updates:
        save_seen(seen)
    else:
        print("Brak nowych, unikalnych promocji.")


if __name__ == "__main__":
    main()
