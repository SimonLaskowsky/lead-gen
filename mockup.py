"""Buduje prompt do wygenerowania makiety strony. Aplikacja nie rysuje makiety sama,
tylko zbiera materiał i układa z niego polecenie, które wkleja się modelowi."""

import json
import re

MAX_ZDJEC_W_PROMPCIE = 18
MAX_ZNAKOW_TRESCI = 1800


def _fakty_firmy(lead, website_data):
    nazwa_na_stronie = (website_data.get("title") or "").strip()
    fakty = [
        ("Nazwa w Google", lead.get("business_name")),
        ("Nazwa na własnej stronie", nazwa_na_stronie),
        ("Branża", lead.get("business_type")),
        ("Miasto", lead.get("city")),
        ("Adres z wizytówki", lead.get("address")),
        ("Telefon", lead.get("phone")),
        ("E-mail", lead.get("email")),
        ("Obecna strona", lead.get("website_url")),
    ]
    return [f"- {etykieta}: {wartosc}" for etykieta, wartosc in fakty if (wartosc or "").strip()]


def _naglowki_i_tresc(website_data):
    linie = []
    h1 = (website_data.get("h1_text") or "").strip()
    if h1:
        linie.append(f"H1 na obecnej stronie: {h1}")
    opis = (website_data.get("meta_description") or "").strip()
    if opis:
        linie.append(f"Meta description: {opis}")
    tekst = _scisniety(website_data.get("text_preview") or website_data.get("full_text") or "")
    if tekst:
        linie.append("Tekst z obecnej strony, do przepisania własnymi słowami:\n" + tekst[:MAX_ZNAKOW_TRESCI])
    return linie


def _scisniety(tekst):
    """Surowy tekst ze strony ma setki pustych linii z menu i pustych kontenerow."""
    return " ".join((tekst or "").split())


def _spis_zdjec(website_data):
    zdjecia = website_data.get("image_urls") or []
    if not zdjecia:
        return []
    linie = [f"Zdjęcia z ich strony ({len(zdjecia)}, pobierz je i użyj tylko tych):"]
    for pozycja in zdjecia[:MAX_ZDJEC_W_PROMPCIE]:
        podpis = f"  ({pozycja['alt']})" if pozycja.get("alt") else ""
        linie.append(f"  {pozycja['url']}{podpis}")
    return linie


def _ustalenia_audytu(analysis):
    if not analysis:
        return []
    notatka = analysis
    if isinstance(analysis, str):
        try:
            odczytane = json.loads(analysis)
            notatka = odczytane.get("analysis", analysis) if isinstance(odczytane, dict) else analysis
        except Exception:
            notatka = analysis
    notatka = re.split(r"\n\nJak model do tego doszedł:", str(notatka))[0].strip()
    if not notatka:
        return []
    return ["Co audyt ustalił o obecnej stronie. Nowa makieta ma to naprawić, nie powtórzyć:",
            notatka]


BEZ_MATERIALU = """Firma nie ma strony, więc nie ma jej tekstów i cały tekst piszesz od zera.
To nie znosi zakazu wymyślania faktów, tylko go zawęża: wolno ci opisywać to, co wynika
z samego zawodu (hydraulik usuwa awarie, montuje armaturę, robi instalacje wod-kan),
a nie wolno ci twierdzić NICZEGO o tej konkretnej firmie. Zakazane są więc lata na rynku,
liczba realizacji, całodobowość, czas dojazdu, gwarancje, ceny, certyfikaty i opinie.
Trzymaj się zakresu usług i obszaru działania, bo to jedyne, co naprawdę wiesz."""


ZASADY = """Jak to zrobić:

1. Najpierw pobierz zdjęcia i ZOBACZ je. Złóż z nich jedną kontaktówkę w siatce z podpisami
   i obejrzyj ją, zamiast oglądać każde z osobna. Odrzuć te słabe: prześwietlone, przypadkowe,
   przestarzałe wnętrza. Lepiej użyć sześciu dobrych niż piętnastu jakichkolwiek.
2. Paletę wyciągnij próbkowaniem z ich zdjęć i logo, nie wybieraj jej z głowy.
   Kolor, który już u nich występuje, wygląda jak ich marka, a nie jak cudzy szablon.
3. Kadruj kodem pod docelowe proporcje i podbij lekko kontrast i nasycenie.
   Zdjęcia HDR z aparatu są płaskie i w surowej postaci wyglądają blado.
4. Jeden plik HTML, samodzielny: zdjęcia wbudowane jako data URI, żadnych odwołań do sieci
   poza fontami. Ten plik ma się otworzyć u klienta bez internetu i wejść w kolumnę mockup_html.

Twarde wymagania, każde sprawdź po wyrenderowaniu, nie na oko:

- Hero mieści się w pierwszym ekranie, nagłówek najwyżej w DWÓCH liniach, podtytuł najwyżej 20 słów.
  Zmierz liczbę linii w przeglądarce, nie zgaduj.
- Zero przepełnienia w poziomie przy 1440 px i przy 390 px. Zmierz
  scrollWidth minus clientWidth, ma wyjść 0 na obu.
- Telefon klikalny (tel:) w nagłówku i w stopce, na każdej szerokości.
- Tekst na zdjęciu ma czytelną zasłonę tam, gdzie leży, a zdjęcie zostaje zdjęciem tam, gdzie tekstu nie ma.
- Bez myślników i półpauz (znaki — i –).

Czego nie wolno:

- NIE WYMYŚLAJ ŻADNYCH FAKTÓW. Adres, ulica, godziny otwarcia, doba hotelowa, ceny, liczba pokoi,
  lata działalności, oceny: jeśli nie ma tego w materiale wyżej, tego nie ma na stronie.
  To idzie do prawdziwej firmy i jeden zmyślony szczegół kończy rozmowę.
- Nie wklejaj opinii klientów ani cytatów, nawet jeśli są na ich obecnej stronie.
- Nie używaj zdjęć z internetu ani stockowych. Tylko ich własne.
- Nie kopiuj ich tekstów słowo w słowo, przepisz je krócej i konkretniej, zachowując fakty.

Na koniec wyrenderuj stronę w 1440 i w 390, obejrzyj oba zrzuty i popraw to, co siada.
Pierwsza wersja zwykle ma zły kadr hero albo nagłówek łamiący się na trzy linie."""


def build_prompt(lead, website_data=None, analysis=None) -> str:
    website_data = website_data or {}
    ma_strone = bool((lead.get("website_url") or "").strip())
    branza = lead.get("business_type") or "lokalna firma"
    miasto = lead.get("city") or ""

    czesci = []
    if ma_strone:
        czesci.append(
            f"Zaprojektuj i zbuduj makietę NOWEJ strony głównej dla firmy {lead.get('business_name', '')}. "
            "Firma ma już stronę, ale słabą. Makieta ma pokazać właścicielowi, o ile lepiej może to wyglądać "
            "przy tych samych materiałach, które już ma."
        )
    else:
        czesci.append(
            f"Zaprojektuj i zbuduj makietę strony głównej dla firmy {lead.get('business_name', '')} "
            f"({branza}, {miasto}). Firma NIE MA własnej strony. Makieta ma pokazać właścicielowi, "
            "jak jego usługi mogłyby wyglądać w sieci."
        )

    czesci.append("Materiał, którym dysponujesz:\n" + "\n".join(_fakty_firmy(lead, website_data)))

    tresc = _naglowki_i_tresc(website_data)
    if tresc:
        czesci.append("\n".join(tresc))

    zdjecia = _spis_zdjec(website_data)
    if zdjecia:
        czesci.append("\n".join(zdjecia))
    else:
        czesci.append(
            "Nie mamy ich zdjęć ze strony. Weź zdjęcia z wizytówki Google (Places Photos) dla tego miejsca. "
            "Jeśli i tam ich nie ma, zbuduj stronę bez fotografii, na typografii, kolorze i układzie. "
            "Nie podstawiaj zdjęć stockowych ani cudzych."
        )

    logo = (website_data.get("logo_url") or "").strip()
    if logo:
        czesci.append(f"Logo: {logo}\nUżyj ich logo, nie rysuj nowego.")

    ustalenia = _ustalenia_audytu(analysis)
    if ustalenia:
        czesci.append("\n".join(ustalenia))

    if not ma_strone:
        czesci.append(BEZ_MATERIALU)

    czesci.append(
        f"Dobierz sekcje pod branżę ({branza}), nie pod uniwersalny szablon. "
        "Zastanów się, po co przychodzi klient tej konkretnej firmy i to postaw na wierzchu. "
        "Pierwszy ekran, potem pasek konkretów, potem to, co ta firma sprzedaje, potem kontakt."
    )
    czesci.append(ZASADY)
    return "\n\n".join(czesci)
