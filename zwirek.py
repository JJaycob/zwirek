import requests
from bs4 import BeautifulSoup
import json
import os
import re
import hashlib

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://twoja-gazetka.pl/produkty/zwirek?store=kaufland"
MAX_PRICE = 24.99

STATE_FILE = "seen.json"


def send_telegram(message):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )


def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except:
        return set()


def save_seen(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, indent=2)


def make_product_id(text, price):
    clean_text = " ".join(text.split())  # usuwa nadmiarowe spacje
    raw = f"{clean_text}|{price}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def main():
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(URL, headers=headers, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    products = soup.select(".product-card-grid-wrapper a")

    print("Liczba znalezionych produktów:", len(products))

    seen = load_seen()

    for product in products:
        text = product.get_text(" ", strip=True)

        price_match = re.search(r"(\d+,\d+)\s*zł", text, re.IGNORECASE)
        if not price_match:
            continue

        price = float(price_match.group(1).replace(",", "."))

        if price > MAX_PRICE:
            continue

        product_id = make_product_id(text, price)

        #  ANTY-DUPLIKAT
        if product_id in seen:
            continue

        discount = round((1 - price / MAX_PRICE) * 100, 1)

        msg = (
            f"🐱 WYKRYTO ŻWIREK W KAUFLANDZIE\n\n"
            f"{text}\n\n"
            f"Cena: {price:.2f} zł\n"
            f"Rabat względem {MAX_PRICE} zł: {discount}%"
        )

        send_telegram(msg)

        seen.add(product_id)
        save_seen(seen)

        print("Wysłano alert:", product_id)

        return  # tylko 1 alert na uruchomienie


if __name__ == "__main__":
    main()
