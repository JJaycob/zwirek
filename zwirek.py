import requests
from bs4 import BeautifulSoup
import json
import os
import re

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://twoja-gazetka.pl/produkty/zwirek?store=kaufland"

MAX_PRICE = 24.99

STATE_FILE = "state.json"


def send_telegram(message):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def parse_price(price_text):

    price_text = price_text.replace(",", ".")

    match = re.search(r"(\d+\.\d+)", price_text)

    if not match:
        return None

    return float(match.group(1))


def main():

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(URL, headers=headers, timeout=30)

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    products = soup.select(".product-card-grid-wrapper a")
    
    print("Liczba znalezionych produktów:", len(products))
    
    for p in products[:5]:
        print("----")
        print(p.get_text(" ", strip=True))

    state = load_state()

    last_seen = state.get("last_seen")

    for product in products:

        text = product.get_text(" ", strip=True)

        price_match = re.search(
            r"(\d+,\d+)\s*zł",
            text,
            re.IGNORECASE
        )

        if not price_match:
            continue

        price = float(
            price_match.group(1).replace(",", ".")
        )

        if price > MAX_PRICE:
            continue

        product_id = f"{text}_{price}"

        if product_id == last_seen:
            print("Ta promocja była już zgłoszona.")
            return

        discount = round(
            (1 - price / 24.99) * 100,
            1
        )

        msg = (
            f"🐱 WYKRYTO ŻWIREK W KAUFLANDZIE\n\n"
            f"{text}\n\n"
            f"Cena: {price:.2f} zł\n"
            f"Rabat względem 24.99 zł: {discount}%"
        )

        send_telegram(msg)

        save_state({
            "last_seen": product_id
        })

        return


if __name__ == "__main__":
    main()
