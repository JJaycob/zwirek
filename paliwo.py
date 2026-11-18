import html
import json
import os
import re
import socket
import time
from datetime import date, datetime, timedelta
from io import BytesIO
from http.client import RemoteDisconnected
from html.parser import HTMLParser
from pathlib import Path
from pypdf import PdfReader
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlencode
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


def urlopen_ipv4(request, timeout):
    """Open a URL over IPv4; GitHub runners may have no working IPv6 route."""
    original_getaddrinfo = socket.getaddrinfo

    def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_getaddrinfo
    try:
        return urlopen(request, timeout=timeout)
    finally:
        socket.getaddrinfo = original_getaddrinfo


def fetch_bytes(url, accept="text/html,application/xhtml+xml,*/*"):
    """GET with retries for transient disconnects and HTTP 5xx/429 responses."""
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(1, 4):
        request = Request(
            url,
            headers={
                "Accept": accept,
                "User-Agent": "Mozilla/5.0 (compatible; PB95-price-notifier/1.0)",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urlopen_ipv4(request, timeout=15) as response:
                return response.read(), response.geturl()
        except HTTPError as exc:
            if exc.code not in retryable_statuses or attempt == 3:
                raise
            last_error = exc
        except (URLError, RemoteDisconnected, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt == 3:
                raise
            last_error = exc
        delay = min(2 ** (attempt - 1), 8)
        print(f"Tymczasowy błąd pobierania ({attempt}/3): {last_error}. Ponawiam za {delay} s.")
        time.sleep(delay)
    raise RuntimeError(f"Nie udało się pobrać {url}: {last_error}")


def fetch_bytes_once(url, accept="text/html,application/xhtml+xml,*/*", timeout=8):
    """Single short GET for probing not-yet-indexed ELI document numbers."""
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "Mozilla/5.0 (compatible; PB95-price-notifier/1.0)",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen_ipv4(request, timeout=timeout) as response:
        return response.read(), response.geturl()

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


def fetch_gov_price_announcements():
    raw, _ = fetch_bytes(NEWS_URL)
    listing = LinkParser()
    listing.feed(raw.decode("utf-8", errors="replace"))

    candidates = []
    seen = set()
    for href, label in listing.links:
        absolute = urljoin(GOV_BASE, href)
        if "/web/energia/" not in absolute:
            continue
        url_matches = "maksymalna-cena-detaliczna-paliw" in absolute.casefold()
        label_text = " ".join(label.casefold().split())
        label_matches = (
            "maksymalna cena detaliczna paliw" in label_text
            or "maksymalnej ceny paliw" in label_text
        )
        if not (url_matches or label_matches):
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
    for url in candidates[:4]:
        try:
            announcements.append(parse_article(url))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            print(f"Pomijam nieczytelny komunikat: {exc}")

    if not announcements:
        raise RuntimeError("Nie udało się odczytać żadnego komunikatu o cenie paliw. " + " | ".join(errors))

    announcements.sort(key=lambda item: item["published"], reverse=True)
    return announcements



def easter_sunday(year):
    """Gregorian Easter date (Meeus/Jones/Butcher algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f, g = divmod(b + 8, 25)
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def is_polish_non_working_day(day):
    fixed_holidays = {
        (1, 1), (1, 6), (5, 1), (5, 3), (8, 15),
        (11, 1), (11, 11), (12, 25), (12, 26),
    }
    easter = easter_sunday(day.year)
    movable_holidays = {easter + timedelta(days=1), easter + timedelta(days=60)}
    return day.weekday() >= 5 or (day.month, day.day) in fixed_holidays or day in movable_holidays


def next_polish_working_day(day):
    while is_polish_non_working_day(day):
        day += timedelta(days=1)
    return day


def parse_eli_notice(item, year, pdf_raw=None):
    """Download an official Monitor Polski PDF and read gross PB95."""
    position = int(item["pos"])
    pdf_url = f"https://api.sejm.gov.pl/eli/acts/MP/{year}/{position}/text.pdf"
    if pdf_raw is None:
        raw, _ = fetch_bytes(pdf_url, accept="application/pdf,*/*")
    else:
        raw = pdf_raw
    reader = PdfReader(BytesIO(raw))
    pdf_text = " ".join(
        (page.extract_text() or "") for page in reader.pages
    )
    normalized = " ".join(pdf_text.replace("\xa0", " ").split()).casefold()

    section = re.search(
        r"benzyny\s+silnikowej\s+bezołowiowej\s+95(.*?)"
        r"(?=benzyny\s+silnikowej\s+bezołowiowej\s+98)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not section:
        raise ValueError(f"Nie znaleziono sekcji PB95 w PDF: {pdf_url}")

    gross_values = re.findall(
        r"(\d{1,2}[,.]\d{2})\s*zł\s*za\s*1\s*l\b",
        section.group(1),
        flags=re.IGNORECASE,
    )
    if not gross_values:
        # Some PDF text layers omit the space between the unit and litre.
        gross_values = re.findall(
            r"(\d{1,2}[,.]\d{2})\s*zł\s*za\s*1\s*l",
            section.group(1),
            flags=re.IGNORECASE,
        )
    if not gross_values:
        raise ValueError(f"Nie znaleziono ceny brutto PB95 w PDF: {pdf_url}")

    price = round(float(gross_values[-1].replace(",", ".")), 2)
    if not (3.0 <= price <= 15.0):
        raise ValueError(f"Odczytana cena PB95 wygląda nieprawidłowo: {price:.2f} zł/l")

    published = parse_date(item.get("promulgation") or item.get("announcementDate"))
    if not published:
        raise ValueError(f"Brak daty publikacji w metadanych aktu: {item.get('title')}")

    return {
        "published": published,
        "price": price,
        "effective_from": published + timedelta(days=1),
        "effective_to": published + timedelta(days=1),
        "url": f"https://eli.gov.pl/eli/MP/{year}/{position}/ogl/pol",
        "title": item.get("title") or f"M.P. {year} poz. {position}",
    }


def eli_years_for(day):
    """Return API years needed for today/tomorrow and New Year's rollover."""
    tomorrow = day + timedelta(days=1)
    return sorted({day.year - 1, day.year, tomorrow.year})

def fetch_eli_price_announcements():
    """Search ELI directly for current fuel-price notices and supplement with recently changed acts."""
    now = datetime.now(TZ)
    today = now.date()
    years = eli_years_for(today)

    matching_by_eli = {}
    fetch_errors = []

    # Use the ELI search endpoint instead of relying only on the annual listing,
    # which can temporarily lag behind the newest Monitor Polski publications.
    for year in years:
        params = urlencode({
            "publisher": "MP",
            "year": year,
            "title": "maksymalnej ceny paliw ciekłych",
            "limit": 100,
            "sortBy": "promulgation",
            "sortDir": "DESC",
        })
        api_url = f"https://api.sejm.gov.pl/eli/acts/search?{params}"
        try:
            raw, _ = fetch_bytes(api_url, accept="application/json")
            payload = json.loads(raw.decode("utf-8"))
            items = payload.get("items", [])
            print(
                f"ELI search {year}: {len(items)} pasujących aktów "
                f"(łącznie w odpowiedzi: {payload.get('count', len(items))})."
            )
        except Exception as exc:
            fetch_errors.append(f"search {year}: {exc}")
            print(f"Nie udało się wyszukać obwieszczeń ELI z {year} r.: {exc}")
            continue

        for item in items:
            title = (item.get("title") or "").casefold()
            if (
                item.get("publisher") == "MP"
                and "maksymalnej ceny paliw ciekłych" in title
                and item.get("promulgation")
            ):
                key = item.get("ELI") or f"MP/{year}/{item.get('pos')}"
                matching_by_eli[key] = (item, year)

    # The changes feed catches newly added ELI records even when the annual
    # listing/search index has not yet refreshed its result set.
    since = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    changes_url = (
        "https://api.sejm.gov.pl/eli/changes/acts?"
        + urlencode({"since": since})
    )
    try:
        raw, _ = fetch_bytes(changes_url, accept="application/json")
        payload = json.loads(raw.decode("utf-8"))
        changes = payload.get("items", [])
        new_matches = 0
        for item in changes:
            title = (item.get("title") or "").casefold()
            if (
                item.get("publisher") == "MP"
                and "maksymalnej ceny paliw ciekłych" in title
                and item.get("promulgation")
            ):
                year = int(item.get("year") or item["promulgation"][:4])
                key = item.get("ELI") or f"MP/{year}/{item.get('pos')}"
                matching_by_eli[key] = (item, year)
                new_matches += 1
        print(
            f"ELI changes: {new_matches} nowych/zmienionych obwieszczeń paliwowych "
            f"wśród {len(changes)} ostatnich zmian."
        )
    except Exception as exc:
        fetch_errors.append(f"changes feed: {exc}")
        print(f"Nie udało się pobrać listy ostatnio zmienionych aktów ELI: {exc}")

    matching = list(matching_by_eli.values())

    # The annual/search feeds can lag behind the official publication.
    # Probe a few following M.P. positions via the official ELI PDF endpoint.
    direct_pdf_cache = {}
    current_year = today.year
    known_positions = [
        int(item.get("pos", 0))
        for item, item_year in matching
        if item_year == current_year
    ]
    if known_positions:
        next_position = max(known_positions) + 1
        for position in range(next_position, next_position + 6):
            pdf_url = f"https://api.sejm.gov.pl/eli/acts/MP/{current_year}/{position}/text.pdf"
            try:
                raw_pdf, _ = fetch_bytes_once(
                    pdf_url,
                    accept="application/pdf,*/*",
                    timeout=8,
                )
                pdf_text = " ".join(
                    (page.extract_text() or "")
                    for page in PdfReader(BytesIO(raw_pdf)).pages
                )
                normalized = " ".join(
                    pdf_text.replace("\xa0", " ").split()
                ).casefold()
            except Exception as exc:
                print(f"ELI direct PDF {current_year}/{position}: {exc}")
                continue

            if (
                "maksymalnej ceny paliw ciekłych" not in normalized
                or "benzyny silnikowej bezołowiowej 95" not in normalized
            ):
                continue

            publication_match = re.search(
                rf"warszawa,\s*dnia\s*(\d{{1,2}})\s+({MONTH_PATTERN})\s+(20\d{{2}})",
                normalized,
            )
            if not publication_match:
                publication_match = re.search(
                    rf"z dnia\s+(\d{{1,2}})\s+({MONTH_PATTERN})\s+(20\d{{2}})",
                    normalized,
                )
            if not publication_match:
                print(f"ELI direct PDF {current_year}/{position}: brak daty publikacji.")
                continue

            published = parse_polish_month_date(
                publication_match.group(1),
                publication_match.group(2),
                publication_match.group(3),
            )
            item = {
                "publisher": "MP",
                "year": current_year,
                "pos": position,
                "promulgation": published.isoformat(),
                "title": (
                    f"Obwieszczenie Ministra Energii z dnia {publication_match.group(1)} "
                    f"{publication_match.group(2)} {publication_match.group(3)} r. "
                    "w sprawie maksymalnej ceny paliw ciekłych"
                ),
            }
            key = f"MP/{current_year}/{position}"
            matching_by_eli[key] = (item, current_year)
            direct_pdf_cache[(current_year, position)] = raw_pdf
            print(
                f"ELI direct PDF: znaleziono obwieszczenie M.P. {current_year} "
                f"poz. {position} z {published.isoformat()}."
            )

        # ELI can lag behind the official Monitor Polski publication. Probe
        # the primary journal's PDF archive using its official document names.
        # Example: position 1144 in 2026 -> M2026000114401.pdf.
        if next_position:
            for position in range(next_position, next_position + 8):
                official_pdf_url = (
                    f"https://monitorpolski.gov.pl/M{current_year}{position:07d}01.pdf"
                )
                try:
                    raw_pdf, _ = fetch_bytes_once(
                        official_pdf_url,
                        accept="application/pdf,*/*",
                        timeout=5,
                    )
                    pdf_text = " ".join(
                        (page.extract_text() or "")
                        for page in PdfReader(BytesIO(raw_pdf)).pages
                    )
                    normalized = " ".join(
                        pdf_text.replace("\xa0", " ").split()
                    ).casefold()
                except Exception as exc:
                    print(f"Monitor Polski PDF {current_year}/{position}: {exc}")
                    continue

                if (
                    "maksymalnej ceny paliw ciekłych" not in normalized
                    or "benzyny silnikowej bezołowiowej 95" not in normalized
                ):
                    continue

                publication_match = re.search(
                    rf"warszawa,\s*dnia\s*(\d{{1,2}})\s+({MONTH_PATTERN})\s+(20\d{{2}})",
                    normalized,
                )
                if not publication_match:
                    publication_match = re.search(
                        rf"z dnia\s+(\d{{1,2}})\s+({MONTH_PATTERN})\s+(20\d{{2}})",
                        normalized,
                    )
                if not publication_match:
                    print(
                        f"Monitor Polski PDF {current_year}/{position}: "
                        "znaleziono dokument bez rozpoznanej daty publikacji."
                    )
                    continue

                published = parse_polish_month_date(
                    publication_match.group(1),
                    publication_match.group(2),
                    publication_match.group(3),
                )
                item = {
                    "publisher": "MP",
                    "year": current_year,
                    "pos": position,
                    "promulgation": published.isoformat(),
                    "title": (
                        f"Obwieszczenie Ministra Energii z dnia {publication_match.group(1)} "
                        f"{publication_match.group(2)} {publication_match.group(3)} r. "
                        "w sprawie maksymalnej ceny paliw ciekłych"
                    ),
                }
                key = f"MP/{current_year}/{position}"
                matching_by_eli[key] = (item, current_year)
                direct_pdf_cache[(current_year, position)] = raw_pdf
                print(
                    f"Monitor Polski PDF: znaleziono obwieszczenie M.P. {current_year} "
                    f"poz. {position} z {published.isoformat()}."
                )

    matching = list(matching_by_eli.values())
    matching.sort(
        key=lambda entry: (entry[0]["promulgation"], int(entry[0].get("pos", 0))),
        reverse=True,
    )

    if not matching:
        detail = " | ".join(fetch_errors)
        raise RuntimeError(
            "ELI API nie zwróciło obwieszczeń o maksymalnej cenie paliw."
            + (f" Błędy pobierania: {detail}" if detail else "")
        )

    announcements = []
    errors = []
    # Keep enough recent notices to include the previous day's price for comparison.
    for item, year in matching[:20]:
        try:
            position = int(item["pos"])
            cached_pdf = direct_pdf_cache.get((year, position))
            notice = parse_eli_notice(item, year, pdf_raw=cached_pdf)
            if cached_pdf is not None:
                notice["url"] = (
                    f"https://monitorpolski.gov.pl/M{year}{position:07d}01.pdf"
                )
            announcements.append(notice)
        except Exception as exc:
            errors.append(f"{item.get('title')}: {exc}")
            print(f"Pomijam nieczytelne obwieszczenie ELI: {exc}")

    if not announcements:
        raise RuntimeError(
            "Nie udało się odczytać żadnego oficjalnego PDF z ELI. " + " | ".join(errors)
        )

    announcements.sort(key=lambda item: (item["published"], item["title"]))
    for index, announcement in enumerate(announcements):
        if index + 1 < len(announcements):
            # The previous maximum price remains in force through the publication
            # date of the following notice; the latter normally takes effect next day.
            announcement["effective_to"] = announcements[index + 1]["published"]
        else:
            # A Friday/holiday announcement also covers the next working day.
            start = announcement["effective_from"]
            announcement["effective_to"] = (
                next_polish_working_day(start)
                if is_polish_non_working_day(start)
                else start
            )

    print(
        f"ELI: odczytano {len(announcements)} oficjalnych obwieszczeń "
        f"z roczników {', '.join(map(str, years))}. "
        f"Najnowsze: {announcements[-1]['published']} – "
        f"PB95 {announcements[-1]['price']:.2f} zł/l "
        f"({announcements[-1]['url']})."
    )
    return announcements

def fetch_recent_price_announcements():
    """Use the official ELI API first, checking Ministry news if ELI has no price for today."""
    try:
        announcements = fetch_eli_price_announcements()
        today = datetime.now(TZ).date()
        today_notice = find_applicable_notice(announcements, today)

        if today_notice is None:
            print(
                "ELI nie zawiera obwieszczenia obowiązującego dziś. "
                "Sprawdzam uzupełniająco oficjalną listę Ministerstwa Energii."
            )
            try:
                ministry_announcements = fetch_gov_price_announcements()
                known = {item["url"] for item in announcements}
                announcements.extend(
                    item for item in ministry_announcements if item["url"] not in known
                )
                announcements.sort(key=lambda item: item["published"])
            except Exception as ministry_error:
                print(f"Nie udało się uzupełnić danych z gov.pl: {ministry_error}")

        print("Źródło danych: oficjalne API ELI / Monitor Polski" +
              (" + aktualności Ministerstwa Energii." if today_notice is None else "."))
        return announcements
    except Exception as eli_error:
        print(
            "Nie można pobrać oficjalnych danych z ELI. "
            f"Próbuję aktualności Ministerstwa Energii: {eli_error}"
        )
        announcements = fetch_gov_price_announcements()
        print("Źródło danych: aktualności Ministerstwa Energii.")
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
    with urlopen_ipv4(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram zwrócił błąd: {result}")


def notify_once(state_key, target, message):
    """Send a Telegram status/error only once for a given target date."""
    state = load_state()
    if state.get(state_key) == target:
        print(f"Powiadomienie {state_key} dla {target} było już wysłane.")
        return False
    try:
        send_telegram(message[:4000])
    except Exception as exc:
        print(f"Nie udało się wysłać pomocniczego powiadomienia Telegram: {exc}")
        return False
    state[state_key] = target
    state[f"{state_key}_at"] = datetime.now(TZ).isoformat()
    save_state(state)
    print(f"Wysłano pomocnicze powiadomienie: {state_key} dla {target}.")
    return True


def main():
    now = datetime.now(TZ)
    check_only = os.environ.get("CHECK_ONLY", "").casefold() == "true"

    today = now.date()
    tomorrow = today + timedelta(days=1)
    print(
        f"Sprawdzam oficjalne ceny PB95 na dziś ({today.isoformat()}) "
        f"i jutro ({tomorrow.isoformat()})."
    )

    try:
        announcements = fetch_recent_price_announcements()
    except Exception as exc:
        print(f"Nie udało się pobrać lub odczytać komunikatów Ministerstwa: {exc}")
        if not check_only:
            notify_once(
                "last_error_for",
                today.isoformat(),
                "⚠️ BOT PB95 – PROBLEM Z ODCZYTEM\n"
                f"Data: {today:%d.%m.%Y}\n"
                "Nie udało się pobrać lub odczytać oficjalnego komunikatu Ministerstwa. "
                "Bot spróbuje ponownie przy następnym uruchomieniu.\n"
                f"Szczegóły: {str(exc)[:900]}",
            )
        raise

    today_notice = find_applicable_notice(announcements, today)
    tomorrow_notice = find_applicable_notice(announcements, tomorrow)

    if today_notice is None:
        print(
            f"Brak oficjalnego komunikatu określającego cenę PB95 na dziś ({today.isoformat()})."
        )
        # Earlier checks should wait; only the last daily attempt reports this.
        final_check_passed = now.hour > 18 or (now.hour == 18 and now.minute >= 37)
        if final_check_passed and not check_only:
            latest = max(announcements, key=lambda item: item["published"])
            latest_date = latest["published"].strftime("%d.%m.%Y")
            latest_price = latest["price"]
            latest_url = latest["url"]
            notify_once(
                "last_error_for",
                today.isoformat(),
                "⚠️ BOT PB95 – BRAK POTWIERDZONEJ CENY NA DZIŚ\n"
                f"Data: {today:%d.%m.%Y}\n"
                "Nie znalazłem w oficjalnych źródłach komunikatu określającego dzisiejszą cenę PB95. "
                "Nie podaję niepotwierdzonej ceny. Bot sprawdzi ponownie jutro.\n"
                f"Ostatnie potwierdzone obwieszczenie: {latest_date}, PB95 {latest_price:.2f} zł/l.\n"
                f"Źródło: {latest_url}",
            )
        return

    if tomorrow_notice is None:
        print(
            f"Komunikat z ceną PB95 na jutro ({tomorrow.isoformat()}) nie jest jeszcze dostępny. "
            "Kolejna zaplanowana próba sprawdzi to ponownie."
        )

        # Catch up a missed daily report: if the price for today is confirmed
        # but today's report was never delivered, send it once instead of staying silent.
        state = load_state()
        if not check_only and state.get("last_report_for") != today.isoformat():
            yesterday = today - timedelta(days=1)
            yesterday_notice = find_applicable_notice(announcements, yesterday)
            today_price = today_notice["price"]

            if yesterday_notice is not None:
                yesterday_price = yesterday_notice["price"]
                difference = round(today_price - yesterday_price, 2)
                if difference > 0:
                    change_line = f"📈 Wzrost o {difference:.2f} zł/l względem wczoraj ({yesterday_price:.2f} zł/l)."
                elif difference < 0:
                    change_line = f"📉 Spadek o {abs(difference):.2f} zł/l względem wczoraj ({yesterday_price:.2f} zł/l)."
                else:
                    change_line = f"➖ Bez zmian względem wczoraj ({yesterday_price:.2f} zł/l)."
            else:
                yesterday_price = None
                change_line = "Nie mam poprzedniego potwierdzonego komunikatu do porównania."

            catchup_message = (
                "⛽ ZALEGŁY RAPORT – MAKSYMALNA CENA PB95\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📅 Cena obowiązująca dziś: {today:%d.%m.%Y}\n"
                f"💰 {today_price:.2f} zł/l\n"
                f"{change_line}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Nie ma jeszcze oficjalnego obwieszczenia z ceną na jutro. "
                "Bot sprawdzi ponownie przy kolejnym uruchomieniu.\n"
                f"Źródło: {today_notice['url']}"
            )
            send_telegram(catchup_message)
            state.update({
                "last_price": today_price,
                "last_notice": today_notice["url"],
                "last_report_for": today.isoformat(),
                "last_report_price": today_price,
                "reported_previous_price": yesterday_price,
                "sent_at": now.isoformat(),
                "effective_from": today_notice["effective_from"].isoformat(),
                "effective_to": today_notice["effective_to"].isoformat(),
            })
            save_state(state)
            print(
                f"Wysłano zaległy raport PB95 na dziś ({today.isoformat()}): "
                f"{today_price:.2f} zł/l."
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
    state.update({
        "last_price": tomorrow_price,
        "last_notice": tomorrow_notice["url"],
        "last_report_for": report_for,
        "last_report_price": tomorrow_price,
        "reported_today_price": today_price,
        "sent_at": now.isoformat(),
        "effective_from": tomorrow_notice["effective_from"].isoformat(),
        "effective_to": tomorrow_notice["effective_to"].isoformat(),
    })
    save_state(state)
    print(
        f"Wysłano dzienny raport PB95 na {tomorrow.isoformat()}: "
        f"{tomorrow_price:.2f} zł/l; zmiana {difference:+.2f} zł/l."
    )

if __name__ == "__main__":
    main()
