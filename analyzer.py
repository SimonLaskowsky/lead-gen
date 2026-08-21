import os
import base64
import io
import anthropic


def _client():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _text(message) -> str:
    """Tekst odpowiedzi. Na Opusie 5 content[0] bywa blokiem myslenia, nie tekstem."""
    return "".join(b.text for b in message.content if b.type == "text")


def _crop_above_fold(png_bytes: bytes, fold_height: int = 900, max_width: int = 1100, quality: int = 85) -> bytes | None:
    """Crop full-page screenshot to above-the-fold portion at high resolution."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        if img.height <= fold_height:
            return None  # already fits viewport, no separate crop needed
        cropped = img.crop((0, 0, img.width, fold_height))
        ratio = min(max_width / cropped.width, 1.0)
        if ratio < 1.0:
            cropped = cropped.resize((int(cropped.width * ratio), int(cropped.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        cropped.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def _parse_analysis(raw: str) -> dict:
    """Extract SCORES: line from Claude response. Returns {"scores": {...}, "analysis": str}."""
    scores = {}
    analysis = raw
    if raw.startswith("SCORES:"):
        line, _, rest = raw.partition("\n")
        analysis = rest.lstrip("\n")
        for part in line[7:].strip().split():
            if "=" in part:
                k, _, v = part.partition("=")
                scores[k.strip()] = int(v.strip()) if v.strip().isdigit() else None
    return {"scores": scores, "analysis": analysis}


def _compress(png_bytes: bytes, max_width: int = 1100, max_height: int = 2500, quality: int = 72) -> bytes:
    """Resize + convert PNG to JPEG to reduce payload size. Falls back to raw PNG if Pillow missing."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        # Scale down to fit within max_width × max_height, preserving aspect ratio
        ratio = min(max_width / img.width, max_height / img.height, 1.0)
        if ratio < 1.0:
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except ImportError:
        return png_bytes  # Pillow not installed, send raw PNG


def analyze_website_visually(lead: dict, screenshots: dict, website_data: dict | None = None) -> dict:
    """Full audit: desktop + mobile screenshots + scraped text → Claude deep analysis.
    Returns {"scores": {"design":X,...}, "analysis": str}."""
    client = _client()

    # Build technical facts
    tech_facts = []
    if website_data and not website_data.get("error"):
        tech_facts.append(f"SSL: {'tak' if website_data.get('has_ssl') else 'NIE'}")
        tech_facts.append(f"Meta viewport: {'tak' if website_data.get('has_mobile_viewport') else 'NIE'}")
        tech_facts.append(f"Meta description: {'tak' if website_data.get('meta_description') else 'NIE'}")
        tech_facts.append(f"Formularz kontaktowy: {'tak' if website_data.get('has_contact_form') else 'NIE'}")
        tech_facts.append(f"CTA button/link: {'tak' if website_data.get('has_cta') else 'NIE'}")
        tech_facts.append(f"Social media linki: {'tak' if website_data.get('has_social') else 'NIE'}")
        tech_facts.append(f"Układ tabelkowy (stary): {'tak' if website_data.get('uses_tables_layout') else 'nie'}")
        if website_data.get("has_dead_analytics"):
            tech_facts.append("Google Analytics: MARTWY — tylko UA (Universal Analytics) wyłączony przez Google w lipcu 2023, brak GA4, strona nie zbiera żadnych danych")
        if website_data.get("has_legacy_ua"):
            tech_facts.append("Google Analytics: ma GA4 (aktywny) + stary UA (martwy od 2023, można usunąć)")
        score = website_data.get("pagespeed_score")
        if score is not None:
            tech_facts.append(f"PageSpeed mobile: {score}/100")
        if website_data.get("title"):
            tech_facts.append(f"Tytuł strony: {website_data['title']}")
        if website_data.get("meta_description"):
            tech_facts.append(f"Meta desc: {website_data['meta_description']}")
        # New checks
        if not website_data.get("has_h1"):
            tech_facts.append("H1: BRAK — Google nie wie jaka jest główna fraza strony")
        elif website_data.get("h1_text"):
            tech_facts.append(f"H1: {website_data['h1_text']}")
        if not website_data.get("has_phone"):
            tech_facts.append("Numer telefonu: NIE ZNALEZIONO na stronie")
        img_count = website_data.get("image_count", 0)
        missing_alt = website_data.get("images_missing_alt", 0)
        if img_count > 0 and missing_alt > 0:
            tech_facts.append(f"Zdjęcia bez alt text: {missing_alt}/{img_count} — problem dla SEO")
        wc = website_data.get("word_count")
        if wc is not None:
            tech_facts.append(f"Liczba słów na stronie: {wc}{' — bardzo mało treści dla Google' if wc < 300 else ''}")

    # Prefer Playwright-rendered text (JS executed) over requests-scraped text
    rendered_text = (screenshots or {}).get("rendered_text") or ""
    text_source = rendered_text or (website_data or {}).get("text_preview") or ""
    page_text = f"\nTreść strony (po renderowaniu JS):\n{text_source[:1500]}" if text_source else ""

    tech_block = "\n".join(tech_facts)

    desktop_bytes = (screenshots or {}).get("desktop")
    mobile_bytes  = (screenshots or {}).get("mobile")
    has_screenshots = bool(desktop_bytes or mobile_bytes)

    visual_instruction = (
        "Masz przed sobą zrzuty ekranu tej strony (desktop i mobile). Przeprowadź szczegółowy audyt wzrokowy i techniczny.\n"
        "WAŻNE: Dane tekstowe poniżej mogą być niekompletne jeśli strona używa JavaScript do renderowania treści. "
        "Zrzuty ekranu są źródłem prawdy — jeśli na screenshocie widać treść której nie ma w danych tekstowych, ufaj screenshotowi."
        if has_screenshots else
        "Nie masz zrzutów ekranu — przeprowadź audyt na podstawie danych technicznych i treści strony poniżej. Bądź równie konkretny i krytyczny.\n"
        "UWAGA: Strona może używać JavaScript do renderowania treści — dane tekstowe mogą być niekompletne."
    )

    mobile_section = (
        "**3. Mobile (patrz na zrzut mobilny)**\nCzy strona działa na telefonie? Co się psuje — tekst, przyciski, układ?"
        if has_screenshots else
        f"**3. Mobile**\nBrak meta viewport: {'TAK — strona NIE jest responsywna' if website_data and not website_data.get('has_mobile_viewport') else 'jest responsywna'}. Oceń konsekwencje."
    )

    prompt = f"""Jesteś bezwzględnym, ale genialnym dyrektorem ds. konwersji i psychologii sprzedaży w internecie. Przeprowadzasz brutalnie szczery audyt strony polskiego lokalnego biznesu, aby znaleźć powody, przez które firma traci klientów na rzecz konkurencji.

=== DANE FIRMY ===
Firma: {lead.get('business_name', '')}
Typ biznesu: {lead.get('business_type', '')}
URL: {lead.get('website_url', '')}

=== WYNIKI AUTOMATYCZNYCH SPRAWDZEŃ ===
{tech_block}
{page_text}

=== TWOJE ZADANIE ===
{visual_instruction}

Przeprowadź analizę, skupiając się na psychologii klienta i konwersji. Unikaj ogólników typu "strona jest ładna/nieładna". Pisz szczerze, bezpośrednio i technicznie konstruktywnie.

Zastosuj poniższą strukturę:

**1. Analiza pierwszego ekranu (Above the fold) i pierwsze 3 sekundy**
- Co widzi użytkownik zanim zacznie przewijać stronę? Czy w ciągu 3 sekund wie CZYM zajmuje się firma i w JAKIM mieście/rejonie działa?
- Czy na pierwszym ekranie jest widoczny, bezpośredni i klikalny przycisk Call-To-Action (np. "Zadzwoń teraz", "Bezpłatna wycena")? Jeśli nie, opisz jak bardzo utrudnia to kontakt.

**2. Krytyczne błędy techniczne i zaufanie (Trust Flags)**
- Przeanalizuj wpływ braku SSL, błędów PageSpeed, braku nagłówka H1 lub martwego Google Analytics na biznes firmy.
- Jak błędy techniczne wpływają na pozycję w Google (SEO) oraz na podświadome poczucie bezpieczeństwa klienta, który ma podać swoje dane lub zadzwonić?

**3. Doświadczenie mobilne (Mobile UX)**
{mobile_section}

**4. Lista 3 najważniejszych zmian o najwyższym ROI**
- Wypisz dokładnie 3 konkretne, techniczne zmiany na stronie, które natychmiast podniosą liczbę telefonów i zapytań od klientów.

Pisz wyłącznie po polsku. Nie używaj emoji. Bądź precyzyjny.

=== FORMAT ODPOWIEDZI ===
Zacznij odpowiedź od JEDNEJ linii z ocenami 1-10 (przed całą analizą):
SCORES: design=X mobile=X seo=X cta=X speed=X
(speed=null jeśli brak danych PageSpeed; null dla dowolnej kategorii jeśli nie możesz ocenić)
Potem pusta linia i pełna analiza."""

    # Build message content
    content = []

    if desktop_bytes:
        fold_crop = _crop_above_fold(desktop_bytes)
        if fold_crop:
            content.append({"type": "text", "text": "**Zrzut ekranu — DESKTOP above-the-fold (pierwsze wrażenie, wysoka rozdzielczość):**"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.standard_b64encode(fold_crop).decode("utf-8")},
            })
        content.append({"type": "text", "text": "**Zrzut ekranu — DESKTOP pełna strona (struktura i układ):**"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(_compress(desktop_bytes)).decode("utf-8")},
        })

    if mobile_bytes:
        content.append({"type": "text", "text": "**Zrzut ekranu — MOBILE (390px):**"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(_compress(mobile_bytes, max_width=600)).decode("utf-8")},
        })

    content.append({"type": "text", "text": prompt})

    # max_tokens z zapasem: na Opusie 5 tokeny myslenia wliczaja sie w limit
    message = client.messages.create(
        model="claude-opus-5",
        max_tokens=16000,
        messages=[{"role": "user", "content": content}],
    )

    return _parse_analysis(_text(message))


def generate_email(lead: dict, website_data: dict | None = None, ai_analysis: str | None = None, my_feedback: str | None = None) -> str:
    client = _client()

    business_name = lead.get("business_name", "")
    business_type = lead.get("business_type", "firma")
    city = lead.get("city", "")
    has_website = bool(lead.get("website_url"))

    # ── Shared context: who we are ──
    sender_context = """
Kim jesteśmy (nadawcy emaila):
- S&N Studio, dwuosobowy duet web developerów z Polski (Szymon i Nikodem)
- Robimy strony dla lokalnych firm, kilka zrealizowanych projektów w regionie, klienci zadowoleni
- Nie jesteśmy korporacją, to atut: szybko, bez biurokracji, bezpośredni kontakt
- Portfolio: https://sandnstudio.pl/
- Odpowiadamy tego samego dnia

Oferta. To sa DWIE ROZNE rzeczy i nigdy nie wolno ich mieszac ani wyceniac tak samo:

1. NOWA STRONA (landing page, strona wizytowka) czyli budowa od zera: od 3000 PLN.
2. ULEPSZENIA istniejacej strony (poprawki, modernizacja, redesign): to NIE jest budowa nowej strony.
   Zakres bywa rozny, wiec cena jest zawsze inna niz przy nowej stronie i ustalana indywidualnie.

ZASADA PIENIEZNA W PIERWSZYM MAILU: nie pisz NIC o pieniadzach. Zadnych kwot, zadnych widelek,
zadnych warunkow platnosci, zadnego "polowa na start". Powyzsze ceny sa dla Ciebie jako kontekst,
kto my jestesmy, a nie tresc do wklejenia. Pierwszy kontakt sprzedaje bezplatny projekt graficzny,
nie usluge. O pieniadzach rozmawiamy dopiero, gdy klient sam odpisze.
"""

    # ── Proven statistics to use ──
    stats_arsenal = """
Statystyki które można użyć (tylko te pasujące do konkretnych problemów tej firmy):
- 60% ruchu w internecie pochodzi z telefonów, strona nieresponsywna traci ponad połowę odwiedzających
- Strony ładujące się ponad 3 sekundy tracą 53% użytkowników mobilnych (Google)
- 75% użytkowników ocenia wiarygodność firmy po wyglądzie strony
- Strony z SSL konwertują o 85% lepiej, bez SSL przeglądarka wyświetla "Niezabezpieczona"
- Firmy z profesjonalną stroną dostają średnio 3x więcej zapytań online
- Brak CTA (przycisku "zadzwoń/napisz") to najczęstsza przyczyna ucieczki klientów ze strony
- Strony z opiniami klientów konwertują o 270% lepiej niż bez opinii
Używaj TYLKO 1-2 statystyk pasujących do problemów tej konkretnej firmy. Nie wymieniaj wszystkich.
Jesli zadna nie pasuje do realnych problemow tej firmy, nie uzywaj zadnej: statystyka na sile brzmi jak masowka.
"""

    # ── Style rules shared by every branch ──
    style_rules = """
=== ZASADY STYLU (bezwzgledne) ===
1. ZAKAZ MYSLNIKOW. Nie wolno uzyc pauzy: ani em dash (unicode U+2014), ani en dash (U+2013),
   ani zwyklego minusa uzytego jako pauza w zdaniu. Zero wyjatkow. Zamiast pauzy uzyj przecinka,
   dwukropka albo rozbij na dwa zdania.
   Lacznik wewnatrz wyrazu ("e-mail", "biało-czerwony") jest OK, pauza miedzy myslami NIE.
2. Nadawcami sa DWAJ MEZCZYZNI, Szymon i Nikodem. Wszystkie czasowniki w rodzaju MESKOOSOBOWYM
   liczby mnogiej: "szukalismy", "trafilismy", "zrobilismy", "jestesmy".
   NIGDY w rodzaju zenskim ("szukalysmy", "trafilysmy") nawet gdy piszesz do kobiety
   i nawet gdy w tekscie przewijaja sie "klientki".
3. Zadnego twierdzenia, ktore odbiorca obali w 10 sekund. Firma MA wizytowke w Google,
   bo wlasnie stamtad mamy jej dane. Nie pisz, ze "nie ma jej w Google" ani ze "nie wyskakuje
   w wyszukiwarce". Brak strony WWW to co innego niz brak obecnosci w Google.
4. Nie obiecuj rzeczy, ktorych nie ma. Szkic strony wspominamy jako propozycje do zrobienia,
   nie jako gotowy plik czekajacy w folderze.
5. Bez P.S., bez emoji, bez wykrzyknikow, bez pogrubien i naglowkow sekcji.
6. BADZ MILY. Piszemy do wlasciciela firmy, ktorej idzie dobrze. Braku strony nie stawiaj
   jako werdyktu ("Wlasnej strony brak."), tylko wplec go w zdanie o tym, co mozna zyskac.
"""

    outsourced = (website_data or {}).get("outsourced_platform")
    if outsourced:
        pitch = (website_data or {}).get("outsourced_pitch", "korzystają z zewnętrznej platformy")

        # Booking platforms (Booksy, Fresha, Treatwell, Znany Lekarz) are marketplaces ,
        # don't suggest replacing them (they lose new customer flow). Instead: own site + keep the widget.
        booking_platforms = {"Booksy", "Fresha", "Treatwell", "Znany Lekarz"}
        is_booking_platform = outsourced in booking_platforms

        if is_booking_platform:
            prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla S&N Studio, dwuosobowego studia web developerskiego.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Sytuacja: firma korzysta z **{outsourced}** jako swojej jedynej obecności w internecie, nie ma własnej strony
{f"Dodatkowe spostrzeżenia (wpleć naturalnie): {my_feedback}" if my_feedback else ""}

=== KONTEKST STRATEGICZNY ===
{outsourced} to marketplace, firma słusznie z niego korzysta bo dostaje nowych klientów z aplikacji.
NIE proponuj zastąpienia {outsourced}. To błąd strategiczny który ich odstraszy.
Właściwy kąt: mają świetne opinie na {outsourced}, ale brakuje im własnej strony która buduje markę premium i ściąga klientów z Google.
Rozwiązanie: własna strona wizytówka + widget {outsourced} wbudowany w stronę (klient rezerwuje bez wychodzenia).
Zyski: własna marka, SEO na Google, profesjonalny wizerunek, uniezależnienie się od jedynego kanału.

{stats_arsenal}
{style_rules}

=== ZADANIE ===
Napisz cold email który SPRZEDAJE własną stronę jako UZUPEŁNIENIE {outsourced}, nie zamiennik.

Struktura emaila:
1. TEMAT: konkretny, najwyzej 60 znakow, np. "Znaleźliśmy {business_name} na {outsourced}, brakuje jednej rzeczy"
2. HOOK: komplementuj, mają dobre opinie/profil na {outsourced}, ale Google ich nie pokazuje gdy ktoś szuka bezpośrednio
3. PROBLEM: klienci którzy nie szukają przez {outsourced} (np. z polecenia, z Google) nie mają gdzie trafić, tracą część ruchu
4. ROZWIĄZANIE: własna strona z widgetem {outsourced} wbudowanym, rezerwacje zostają, dochodzi SEO i marka premium
5. CTA: bezplatny projekt graficzny strony wraz z wycena, bez zobowiazan, zakonczony uprzejmym pytaniem w pelnym zdaniu

Zasady:
- Maksymalnie 180 słów
- Ani slowa o cenie i platnosciach, to temat na pozniej
- Pisz w formie "my", ZAWSZE liczba mnoga (jesteśmy dwuosobowym studiem). Nigdy "znalazłem/znalazłam/sprawdziłem", tylko "znalezliśmy/sprawdziliśmy". Żadnych form pierwszej osoby liczby pojedynczej.
- Doceniaj {outsourced}, nie atakuj go, firma słusznie go używa
- NIE brzmij pouczająco, pokaż szansę którą tracą, nie że coś zepsuli
- Odpowiedz wylacznie gotowa trescia maila, bez zadnych komentarzy przed ani po. Pierwsza linia: Temat: [temat]
- Podpis, dokladnie dwie linie: pierwsza "Szymon i Nikodem", druga "S&N Studio · sandnstudio.pl"
- Nie używaj korporacyjnego języka
- Zacznij od "Dzień dobry," i zaraz po nim hak, bez zdania rozbiegowego
- NIE dodawaj P.S., CTA w punkcie 5 jest wystarczające
"""
        else:
            # Social/link platforms (Facebook, Instagram, Linktree, Google Sites), these are weak presences,
            # proposing a real website as replacement makes sense here.
            prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla S&N Studio, dwuosobowego studia web developerskiego które oferuje własną stronę lokalnej firmie.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Sytuacja: firma używa **{outsourced}** zamiast własnej strony, {pitch}
{f"Dodatkowe spostrzeżenia (wpleć naturalnie): {my_feedback}" if my_feedback else ""}

{stats_arsenal}
{style_rules}

=== ZADANIE ===
Napisz cold email który SPRZEDAJE własną stronę zamiast {outsourced}. Argument: {outsourced} nie zastępuje prawdziwej strony, brak SEO, brak własnej marki, brak kontroli.

Struktura emaila:
1. TEMAT: konkretny, najwyzej 60 znakow, nawiązujący do braku własnej strony i tego co przez to tracą
2. HOOK: zauważyliście że ich jedyną obecnością w sieci jest profil na {outsourced}, Google ich nie pokazuje gdy ktoś szuka ich branży w mieście
3. KOSZT BRAKU STRONY: klienci z Google trafiają do konkurencji, nie do nich
4. ALTERNATYWA: własna strona, własna domena, SEO, marka premium. Ani slowa o cenie i platnosciach.
5. CTA: bezplatny projekt graficzny strony wraz z wycena, bez zobowiazan, zakonczony uprzejmym pytaniem w pelnym zdaniu

Zasady:
- Maksymalnie 180 słów
- Pisz w formie "my", ZAWSZE liczba mnoga (jesteśmy dwuosobowym studiem). Nigdy "znalazłem/znalazłam/sprawdziłem", tylko "znalezliśmy/sprawdziliśmy". Żadnych form pierwszej osoby liczby pojedynczej.
- NIE brzmij pouczająco, pokaż szansę którą tracą
- Odpowiedz wylacznie gotowa trescia maila, bez zadnych komentarzy przed ani po. Pierwsza linia: Temat: [temat]
- Podpis, dokladnie dwie linie: pierwsza "Szymon i Nikodem", druga "S&N Studio · sandnstudio.pl"
- Nie używaj korporacyjnego języka
- Zacznij od "Dzień dobry," i zaraz po nim hak, bez zdania rozbiegowego
- NIE dodawaj P.S., CTA w punkcie 5 jest wystarczające
"""
        message = client.messages.create(
            model="claude-opus-5",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        return _text(message)

    if not has_website:
        prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla S&N Studio, dwuosobowego studia web developerskiego które oferuje zbudowanie strony lokalnej firmie.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Sytuacja: firma NIE MA strony internetowej w ogóle
{f"Dodatkowe spostrzeżenia (wpleć naturalnie): {my_feedback}" if my_feedback else ""}

{style_rules}

=== ZADANIE ===
Napisz krotki cold email sprzedazowy. Ma doprowadzic do odpowiedzi, nie do wyceny.

Struktura (nie pisz numerow ani naglowkow):
1. TEMAT: konkretny, bez obietnic i bez strachu. Nazwij sytuacje, dodaj lekka luke informacyjna.
   Najwyzej 60 znakow, dluzszy temat skrzynki ucinaja.
2. OTWARCIE: "Dzien dobry," a potem zyczliwa obserwacja o ich wizytowce i o tym,
   ze wlasnej strony jeszcze nie maja.
3. CO MOZNA ZYSKAC: jedno zdanie o kliencie, ktory porownuje kilka firm z branzy.
   Bez statystyk, bez procentow, bez pouczania.
4. KIM JESTESMY: pol zdania, dwuosobowe studio z regionu (Szymon i Nikodem), robimy strony
   dla lokalnych firm. Mozna skleic z punktem 5 w jedno zdanie.
5. CTA: proponujemy BEZPLATNY projekt graficzny strony wraz z wycena, bez zobowiazan.
   To jest jedyna rzecz, o ktora prosimy w tym mailu.
6. Podpis: S&N Studio · sandnstudio.pl

Zasady:
- Maksymalnie 60 slow razem z tematem. To twardy limit, krotszy mail wygrywa.
- Najwyzej trzy krotkie akapity, kazdy najwyzej dwa zdania.
- ANI SLOWA O PLATNOSCIACH. Zero cen, zero widelek, zero rat, zero "polowa na start".
  Pierwszy mail sprzedaje darmowy projekt, nie usluge. Pieniadze sa tematem na pozniej.
- ZADNYCH STATYSTYK ani procentow. Brzmia jak wypelniacz i obnizaja wiarygodnosc.
- CTA ma byc uprzejmym pytaniem w pelnym zdaniu, np. "Czy mozemy taki projekt przygotowac
  i podeslac?" albo "Chcialaby Pani zobaczyc, jak taka strona moglaby wygladac?".
  ZAKAZ jednowyrazowych zaczepek typu "Zainteresowana?", "Zainteresowany?", "Chetnie?".
  Brzmia infantylnie i spoufalaja sie z osoba, ktorej nie znamy.
- Zwroty grzecznosciowe dopasuj do plci wlasciciela, jesli da sie ja wywnioskowac
  z nazwy firmy ({business_name}). W razie najmniejszej watpliwosci pisz bezosobowo
  albo "Panstwa": pomylka plci konczy rozmowe zanim sie zaczela.
- Podkresl, ze projekt jest bezplatny i do niczego nie zobowiazuje.
- Zero social proof bez nazw. Nie pisz "kilka firm nam zaufalo", to nic nie znaczy.
- Odpowiedz wylacznie gotowa trescia maila, bez komentarzy przed ani po.
  Pierwsza linia to: Temat: [temat]
- Nie uzywaj slow: "pragne", "uprzejmie", "niniejszym", "pozwalam sobie", "oferta"

WAZNE O ORYGINALNOSCI: powyzsza struktura to szkielet, nie gotowy tekst do przepisania.
Kazdy mail ma byc napisany od nowa pod konkretna firme ({business_name}, {business_type}, {city}).
Zmieniaj sformulowania, kolejnosc slow w zdaniu i sposob opisania problemu.
Dwa maile do dwoch roznych firm nie moga brzmiec jak ten sam tekst z podmieniona nazwa.
"""

    else:
        # Build specific issues list
        issues = []
        if website_data and not website_data.get("error"):
            if not website_data.get("has_mobile_viewport"):
                issues.append("brak responsywności, strona się psuje na telefonach")
            if not website_data.get("has_ssl"):
                issues.append("brak SSL, Chrome pokazuje 'Niezabezpieczona' zanim klient w ogóle zobaczy stronę")
            if not website_data.get("meta_description"):
                issues.append("brak meta description, Google nie wie jak promować tę stronę")
            if not website_data.get("has_cta"):
                issues.append("brak przycisku CTA, klient nie wie co ma zrobić żeby się skontaktować")
            if not website_data.get("has_contact_form"):
                issues.append("brak formularza kontaktowego na stronie głównej (może być na podstronie kontakt), połowa klientów woli napisać niż dzwonić")
            if not website_data.get("has_phone"):
                issues.append("numer telefonu niewidoczny na stronie głównej (może być na podstronie), klient mobilny który trafi z Google nie zadzwoni bez szukania")
            if website_data.get("uses_tables_layout"):
                issues.append("układ tabelkowy, design rodem z 2008 roku, wygląda nieprofesjonalnie")
            if website_data.get("has_dead_analytics"):
                issues.append("Google Analytics wyłączony od 2023, właściciel nie widzi ilu klientów traci")
            score = website_data.get("pagespeed_score")
            if score is not None and score < 60:
                issues.append(f"PageSpeed {score}/100, strona ładuje się bardzo wolno, większość użytkowników mobilnych wychodzi")
            elif score is not None and score < 80:
                issues.append(f"PageSpeed {score}/100, strona ładuje się wolno na telefonie")
            # SEO specifics with real numbers
            missing_alt = website_data.get("images_missing_alt", 0)
            img_count = website_data.get("image_count", 0)
            if missing_alt > 0 and img_count > 0:
                issues.append(f"{missing_alt} z {img_count} zdjęć nie ma alt text, stracona szansa na pozycjonowanie obrazków w Google")
            wc = website_data.get("word_count")
            if wc is not None and wc < 400:
                issues.append(f"tylko {wc} słów treści na stronie, Google preferuje minimum 500-800 słów dla lokalnego SEO")
            if not website_data.get("has_h1"):
                issues.append("brak tagu H1, Google nie wie jaka jest główna fraza strony")
            elif website_data.get("h1_text") and business_type:
                # Check if business type keywords appear in H1
                bt_words = business_type.lower().split()
                h1_lower = website_data["h1_text"].lower()
                if not any(w in h1_lower for w in bt_words if len(w) > 3):
                    issues.append(f"H1 \"{website_data['h1_text']}\" nie zawiera frazy kluczowej, marketingowy, ale SEO-neutralny")

        if ai_analysis:
            # ai_analysis may be a JSON blob ({"scores":..., "analysis":...}), extract clean text
            analysis_text = ai_analysis
            try:
                import json as _json
                parsed = _json.loads(ai_analysis)
                if isinstance(parsed, dict) and parsed.get("analysis"):
                    analysis_text = parsed["analysis"]
            except Exception:
                pass
            site_context = (
                f"Szczegółowa analiza AI strony:\n{analysis_text}\n\n"
                f"Dane techniczne (mogą być niepełne dla stron JS-rendered, traktuj jako wskazówki, nie pewniki):\n"
                + "\n".join(f"- {i}" for i in issues)
            ) if issues else f"Szczegółowa analiza AI strony:\n{analysis_text}"
        elif issues:
            site_context = (
                "Dane techniczne (automatyczny skaner, mogą być niepełne dla stron z JavaScriptem):\n"
                + "\n".join(f"- {i}" for i in issues)
            )
        else:
            site_context = "Strona wymaga modernizacji, przestarzały design, brak nowoczesnych elementów"

        if my_feedback:
            site_context += f"\n\nDodatkowe spostrzeżenia (wpleć naturalnie w email, nie wyróżniaj jako osobnej sekcji):\n{my_feedback}"

        audit_text = site_context

        prompt = f"""Jesteś genialnym copywriterem specjalizującym się w cold mailingu B2B do lokalnych firm handlowo-usługowych w Polsce. Twoim zadaniem jest napisanie otwierającej wiadomości e-mail na podstawie dostarczonego audytu strony internetowej.

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
URL: {lead.get('website_url', '')}

=== WYNIKI AUDYTU STRONY (ŹRÓDŁO WIEDZY) ===
{audit_text}
{style_rules}
=== WYTYCZNE DLA COLD MAILA ===
1. Zwrot do adresata: "Dzień dobry" lub "Panie/Pani [imię]", ale imienia uzyj TYLKO jesli wystepuje w nazwie firmy, nigdy go nie zgaduj. Pisz per Pan/Pani, szanując tradycyjne podejście lokalnych przedsiębiorców. Żadnego "Cześć" na start.
2. Temat maila: Intrygujący, bezpośredni, najwyzej 60 znakow, nawiązujący do smartfona i konkretnego błędu ze źródłowego audytu (np. "Weszliśmy na [domena] z telefonu, klienci mogą nie doczekać się wyceny").
3. Wstęp: Wykorzystaj kontekst lokalny i psychologiczny (np. "Wyszukaliśmy [Nazwa Firmy] na telefonie, udając klienta z [Miasto/Region], któremu pilnie potrzebna jest pomoc...").
4. Rozwinięcie (NAJWAŻNIEJSZE, tu pokazujesz głębię analizy): Z dostarczonego audytu wybierz 1 najboleśniejszy błąd biznesowy i rozwiń go najmocniej w 2-3 zdaniach językiem korzyści (nie "responsywność", lecz "klienci z telefonów uciekają, bo nie widzą numeru"). To Twój główny haczyk.
5. Krótka lista "co jeszcze wyłapaliśmy": Zaraz po głównym problemie dorzuć zwięzłą wypunktowaną listę 3-4 KOLEJNYCH konkretnych usterek z audytu (np. brak SSL, martwy Google Analytics, brak nagłówka H1, wolne ładowanie, brak opinii, ukryty formularz). Każdy punkt jedno krótkie zdanie, chodzi o pokazanie, że naprawdę przeszliśmy stronę punkt po punkcie, a nie wysłaliśmy masówki. Wybieraj punkty REALNIE obecne w audycie, nie zmyślaj.
6. Sygnał, że to dopiero wierzchołek: Po liście dodaj jedno zdanie w stylu "To tylko część tego, co znaleźliśmy, pełną listę z konkretnymi poprawkami mamy spisaną i chętnie prześlemy". Pokaż, że za mailem stoi solidny, obszerny audyt, a nie kilka ogólników.
7. Kim jesteście: "Jesteśmy S&N Studio, dwuosobowy zespół programistów z Polski. Bierzemy na warsztat witryny lokalnych firm i sprawnie przebudowujemy je tak, aby generowały więcej telefonów." (link do realizacji jest w podpisie, nie powtarzaj go w treści).
8. Wycena: to sa ULEPSZENIA istniejacej strony, a nie budowa nowej, wiec zakres i cena sa zawsze indywidualne. NIE podawaj ZADNEJ kwoty, ani widelek, ani stawek agencji, ani warunkow platnosci. Napisz tylko, ze wycene przygotowujemy indywidualnie po obejrzeniu zakresu i ze dolaczamy ja do bezplatnego podgladu.
9. Call to Action (Haczyk): Zaproponuj podrzucenie bezpłatnego, prostego podglądu (mockupu) ekranu głównego po optymalizacji. Zapytaj na końcu: "Czy możemy podesłać ten bezpłatny podgląd do rzucenia okiem?".

=== ZASADY STYLU ===
- Maksymalnie 180 słów razem z tematem. Dłuższego cold maila właściciel firmy nie doczyta do CTA. Jeśli musisz ciąć, tnij rozwinięcie i opis zespołu, nie listę usterek.
- Pisz zwięźle i konkretnie, bez lania wody i bez marketingu korporacyjnego, ale NIE skracaj listy usterek z punktu 5, ona ma robić wrażenie skrupulatności.
- Mail ma wyglądać tak, jakby Szymon lub Nikodem napisali go ręcznie po dokładnym przejściu strony, ma czuć się jak realny, szczegółowy audyt, nie szablon.
- Główny problem rozwiń, resztę usterek podaj telegraficznie w punktach, kontrast między głębią a listą buduje poczucie, że masz tego dużo więcej.
- Całkowity zakaz używania emoji.
- Odpowiedz WYŁĄCZNIE gotową treścią maila (Temat + Treść), bez żadnych dodatkowych komentarzy od AI przed czy po tekście.

Podpisz maila dokladnie tak:
Szymon i Nikodem
S&N Studio · sandnstudio.pl"""

    message = client.messages.create(
        model="claude-opus-5",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )

    return _text(message)
