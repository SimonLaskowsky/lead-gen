import os
import base64
import io
import anthropic


def _client():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


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

    prompt = f"""Jesteś senior konsultantem ds. web designu i marketingu cyfrowego. Przeprowadzasz pełny audyt strony polskiego lokalnego biznesu.

=== DANE FIRMY ===
Firma: {lead.get('business_name', '')}
Typ biznesu: {lead.get('business_type', '')}
URL: {lead.get('website_url', '')}

=== WYNIKI AUTOMATYCZNYCH SPRAWDZEŃ ===
{tech_block}
{page_text}

=== TWOJE ZADANIE ===
{visual_instruction}

Oceń każdy punkt konkretnie — nie ogólnikowo:

**1. Pierwsze wrażenie (3-5 sekund)**
Czy strona od razu komunikuje czym się firma zajmuje? Czy wygląda profesjonalnie? Czy zachęca do zostania?

**2. Design i estetyka**
Kolory, typografia, jakość zdjęć/grafik, spójność wizualna. Czy wygląda nowocześnie czy jak relikt lat 2010?

{mobile_section}

**4. Treść i komunikacja**
Czy jasno widać: co oferują, dla kogo, ile kosztuje, jak się skontaktować? Czy są opinie klientów?

**5. Najważniejsze rzeczy do poprawy**
Podaj 3-4 konkretne zmiany które miałyby największy wpływ na konwersję.

Pisz po polsku. Bądź szczery i konkretny — jak gdybyś płacił za ten audyt. Używaj punktorów i nagłówków z powyższej struktury. Nie używaj emoji.

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

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4500,
        messages=[{"role": "user", "content": content}],
    )

    return _parse_analysis(message.content[0].text)


def generate_email(lead: dict, website_data: dict | None = None, ai_analysis: str | None = None, my_feedback: str | None = None) -> str:
    client = _client()

    business_name = lead.get("business_name", "")
    business_type = lead.get("business_type", "firma")
    city = lead.get("city", "")
    has_website = bool(lead.get("website_url"))

    # ── Shared context: who we are ──
    sender_context = """
Kim jesteśmy (nadawcy emaila):
- Sand'n Studio — dwuosobowy duet web developerów z Polski (Szymon i Nikodem)
- Robimy strony dla lokalnych firm, kilka zrealizowanych projektów w regionie, klienci zadowoleni
- Nie jesteśmy korporacją — to atut: szybko, bez biurokracji, bezpośredni kontakt
- Portfolio: https://sandnstudio.pl/
- Odpowiadamy tego samego dnia

Oferta (NIE wymieniaj wszystkich tierów — wspomnij tylko jeden pasujący do sytuacji):
- Landing page: od 750 PLN — strona jednostronicowa, szybka realizacja
- Strona wizytówka (GŁÓWNA): od 1250 PLN — do 4 podstron, responsywna, formularz, pomoc z domeną i hostingiem, 30 dni wsparcia
- System rezerwacji: od 2000 PLN — WordPress + Bookly/Amelia, klienci umawiają się sami bez Twojego udziału

Kotwica cenowa: agencje biorą 3000–8000 PLN za to samo co my od 1250 PLN.
Killer argument: PŁATNOŚĆ PO POŁOWIE — połowa na start, połowa dopiero gdy strona im się podoba. Zero ryzyka.

Przy modernizacji istniejącej strony: cena zależy od zakresu — NIE podawaj żadnej konkretnej kwoty. Powiedz tylko że "wyceniamy indywidualnie po rozmowie" i że płatność jest podzielona na dwie raty.
"""

    # ── Proven statistics to use ──
    stats_arsenal = """
Statystyki które można użyć (tylko te pasujące do konkretnych problemów tej firmy):
- 60% ruchu w internecie pochodzi z telefonów — strona nieresponsywna traci ponad połowę odwiedzających
- Strony ładujące się ponad 3 sekundy tracą 53% użytkowników mobilnych (Google)
- 75% użytkowników ocenia wiarygodność firmy po wyglądzie strony
- Strony z SSL konwertują o 85% lepiej — bez SSL przeglądarka wyświetla "Niezabezpieczona"
- Firmy z profesjonalną stroną dostają średnio 3x więcej zapytań online
- Brak CTA (przycisku "zadzwoń/napisz") to najczęstsza przyczyna ucieczki klientów ze strony
- Strony z opiniami klientów konwertują o 270% lepiej niż bez opinii
Używaj TYLKO 1-2 statystyk pasujących do problemów tej konkretnej firmy. Nie wymieniaj wszystkich.
"""

    outsourced = (website_data or {}).get("outsourced_platform")
    if outsourced:
        pitch = (website_data or {}).get("outsourced_pitch", "korzystają z zewnętrznej platformy")

        # Booking platforms (Booksy, Fresha, Treatwell, Znany Lekarz) are marketplaces —
        # don't suggest replacing them (they lose new customer flow). Instead: own site + keep the widget.
        booking_platforms = {"Booksy", "Fresha", "Treatwell", "Znany Lekarz"}
        is_booking_platform = outsourced in booking_platforms

        if is_booking_platform:
            prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla Sand'n Studio — dwuosobowego studia web developerskiego.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Sytuacja: firma korzysta z **{outsourced}** jako swojej jedynej obecności w internecie — nie ma własnej strony

=== KONTEKST STRATEGICZNY ===
{outsourced} to marketplace — firma słusznie z niego korzysta bo dostaje nowych klientów z aplikacji.
NIE proponuj zastąpienia {outsourced}. To błąd strategiczny który ich odstraszy.
Właściwy kąt: mają świetne opinie na {outsourced}, ale brakuje im własnej strony która buduje markę premium i ściąga klientów z Google.
Rozwiązanie: własna strona wizytówka + widget {outsourced} wbudowany w stronę (klient rezerwuje bez wychodzenia).
Zyski: własna marka, SEO na Google, profesjonalny wizerunek, uniezależnienie się od jedynego kanału.

{stats_arsenal}

=== ZADANIE ===
Napisz cold email który SPRZEDAJE własną stronę jako UZUPEŁNIENIE {outsourced}, nie zamiennik.

Struktura emaila:
1. TEMAT: konkretny — np. "Znalazłem {business_name} na {outsourced} — brakuje jednej rzeczy"
2. HOOK: komplementuj — mają dobre opinie/profil na {outsourced}, ale Google ich nie pokazuje gdy ktoś szuka bezpośrednio
3. PROBLEM: klienci którzy nie szukają przez {outsourced} (np. z polecenia, z Google) nie mają gdzie trafić — tracą część ruchu
4. ROZWIĄZANIE: własna strona z widgetem {outsourced} wbudowanym — rezerwacje zostają, dochodzi SEO i marka premium
5. OFERTA: od 1250 PLN jednorazowo, połowa na start, połowa po oddaniu
6. CTA: "Mamy już gotowy szkic jak mogłaby wyglądać strona {business_name} — chce Pan/Pani zobaczyć?"

Zasady:
- Maksymalnie 180 słów
- Pisz w formie "my" — ZAWSZE liczba mnoga (jesteśmy dwuosobowym studiem). Nigdy "znalazłem/znalazłam/sprawdziłem" — tylko "znalezliśmy/sprawdziliśmy". Żadnych form pierwszej osoby liczby pojedynczej.
- Doceniaj {outsourced} — nie atakuj go, firma słusznie go używa
- NIE brzmij pouczająco — pokaż szansę którą tracą, nie że coś zepsuli
- Pierwsza linia: Temat: [temat]
- Podpisz się: Sand'n Studio (Szymon i Nikodem)
- Nie używaj korporacyjnego języka
- Zacznij od haka, nie od "Dzień dobry"
- Wspomnij portfolio: sandnstudio.pl
- NIE dodawaj P.S. — CTA w punkcie 6 jest wystarczające
"""
        else:
            # Social/link platforms (Facebook, Instagram, Linktree, Google Sites) — these are weak presences,
            # proposing a real website as replacement makes sense here.
            prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla Sand'n Studio — dwuosobowego studia web developerskiego które oferuje własną stronę lokalnej firmie.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Sytuacja: firma używa **{outsourced}** zamiast własnej strony — {pitch}

{stats_arsenal}

=== ZADANIE ===
Napisz cold email który SPRZEDAJE własną stronę zamiast {outsourced}. Argument: {outsourced} nie zastępuje prawdziwej strony — brak SEO, brak własnej marki, brak kontroli.

Struktura emaila:
1. TEMAT: konkretny — nawiązujący do braku własnej strony i tego co przez to tracą
2. HOOK: zauważyłeś że ich jedyną obecnością w sieci jest profil na {outsourced} — Google ich nie pokazuje gdy ktoś szuka ich branży w mieście
3. KOSZT BRAKU STRONY: klienci z Google trafiają do konkurencji, nie do nich
4. ALTERNATYWA: własna strona od 1250 PLN jednorazowo — własna domena, SEO, marka premium. Połowa na start, połowa po oddaniu.
5. CTA: "Mamy już gotowy szkic jak mogłaby wyglądać strona {business_name} — chce Pan/Pani zobaczyć?"

Zasady:
- Maksymalnie 180 słów
- Pisz w formie "my" — ZAWSZE liczba mnoga (jesteśmy dwuosobowym studiem). Nigdy "znalazłem/znalazłam/sprawdziłem" — tylko "znalezliśmy/sprawdziliśmy". Żadnych form pierwszej osoby liczby pojedynczej.
- NIE brzmij pouczająco — pokaż szansę którą tracą
- Pierwsza linia: Temat: [temat]
- Podpisz się: Sand'n Studio (Szymon i Nikodem)
- Nie używaj korporacyjnego języka
- Zacznij od haka, nie od "Dzień dobry"
- Wspomnij portfolio: sandnstudio.pl
- NIE dodawaj P.S. — CTA w punkcie 5 jest wystarczające
"""
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    if not has_website:
        prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla Sand'n Studio — dwuosobowego studia web developerskiego które oferuje zbudowanie strony lokalnej firmie.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Sytuacja: firma NIE MA strony internetowej w ogóle
{f"Dodatkowe spostrzeżenia (wpleć naturalnie): {my_feedback}" if my_feedback else ""}

{stats_arsenal}

=== ZADANIE ===
Napisz cold email który SPRZEDAJE. Nie informacyjny — sprzedażowy.

Struktura emaila (nie pisz nagłówków, po prostu tak go zbuduj):
1. TEMAT: intrygujący, konkretny, nie "Propozycja strony" — coś co wywołuje ciekawość lub lekki strach przed stratą
2. HOOK (pierwsze zdanie): zaskakujący fakt lub pytanie które boli — np. "Szukałem dziś {business_type} w {city} na Google — Pana firmy nie ma."
3. KOSZT BRAKU STRONY: przetłumacz brak strony na realne straty — ilu klientów szuka online i ich nie znajduje
4. SOCIAL PROOF: wspomnij że inne podobne firmy w regionie już to zrobiły i co zyskały (ogólnie, nie fake)
5. OFERTA + CENA: strona wizytówka od 1250 PLN — responsywna, formularz, pomoc z domeną i hostingiem, 30 dni wsparcia. Agencje biorą 3000–8000 PLN za to samo. Połowa płatności na start, połowa po oddaniu.
6. CTA: "Mamy już gotowy szkic strony dla [firma] — chce Pan/Pani zobaczyć jak mogłaby wyglądać?"

Zasady:
- Maksymalnie 180 słów (krótko = szanujemy czas)
- Pisz w formie "my" — ZAWSZE liczba mnoga (jesteśmy dwuosobowym studiem). Nigdy "znalazłem/znalazłam/sprawdziłem" — tylko "znalezliśmy/sprawdziliśmy". Żadnych form pierwszej osoby liczby pojedynczej.
- NIE brzmij pouczająco — właściciele firm są wrażliwi na krytykę
  Zamiast oceniać wprost → pokaż że tracą szansę, nie że coś zepsuli
  Przykład: "Szkoda żeby klienci szukający [branży] w Google trafiali do konkurencji zamiast do Państwa"
- Jedna konkretna statystyka pasująca do branży
- Pierwsza linia to: Temat: [temat]
- Podpisz się: Sand'n Studio (Szymon i Nikodem)
- Nie używaj słów: "pragnę", "uprzejmie", "niniejszym", "pozwalam sobie"
- Nie zaczynaj od "Dzień dobry" — zacznij od haka
- Wspomnij portfolio: sandnstudio.pl
- NIE dodawaj P.S. — CTA w punkcie 6 jest wystarczające
"""

    else:
        # Build specific issues list
        issues = []
        if website_data and not website_data.get("error"):
            if not website_data.get("has_mobile_viewport"):
                issues.append("brak responsywności — strona się psuje na telefonach")
            if not website_data.get("has_ssl"):
                issues.append("brak SSL — Chrome pokazuje 'Niezabezpieczona' zanim klient w ogóle zobaczy stronę")
            if not website_data.get("meta_description"):
                issues.append("brak meta description — Google nie wie jak promować tę stronę")
            if not website_data.get("has_cta"):
                issues.append("brak przycisku CTA — klient nie wie co ma zrobić żeby się skontaktować")
            if not website_data.get("has_contact_form"):
                issues.append("brak formularza — można tylko zadzwonić, połowa klientów woli pisać")
            if not website_data.get("has_phone"):
                issues.append("brak numeru telefonu na stronie — klienci nie mogą zadzwonić bez szukania")
            if website_data.get("uses_tables_layout"):
                issues.append("układ tabelkowy — design rodem z 2008 roku, wygląda nieprofesjonalnie")
            if website_data.get("has_dead_analytics"):
                issues.append("Google Analytics wyłączony od 2023 — właściciel nie widzi ilu klientów traci")
            score = website_data.get("pagespeed_score")
            if score is not None and score < 60:
                issues.append(f"PageSpeed {score}/100 — strona ładuje się bardzo wolno, większość użytkowników mobilnych wychodzi")
            elif score is not None and score < 80:
                issues.append(f"PageSpeed {score}/100 — strona ładuje się wolno na telefonie")
            # SEO specifics with real numbers
            missing_alt = website_data.get("images_missing_alt", 0)
            img_count = website_data.get("image_count", 0)
            if missing_alt > 0 and img_count > 0:
                issues.append(f"{missing_alt} z {img_count} zdjęć nie ma alt text — stracona szansa na pozycjonowanie obrazków w Google")
            wc = website_data.get("word_count")
            if wc is not None and wc < 400:
                issues.append(f"tylko {wc} słów treści na stronie — Google preferuje minimum 500-800 słów dla lokalnego SEO")
            if not website_data.get("has_h1"):
                issues.append("brak tagu H1 — Google nie wie jaka jest główna fraza strony")
            elif website_data.get("h1_text") and business_type:
                # Check if business type keywords appear in H1
                bt_words = business_type.lower().split()
                h1_lower = website_data["h1_text"].lower()
                if not any(w in h1_lower for w in bt_words if len(w) > 3):
                    issues.append(f"H1 \"{website_data['h1_text']}\" nie zawiera frazy kluczowej — marketingowy, ale SEO-neutralny")

        if ai_analysis:
            site_context = f"Szczegółowa analiza AI strony:\n{ai_analysis[:1200]}"
        elif issues:
            site_context = "Konkretne problemy znalezione na stronie:\n" + "\n".join(f"- {i}" for i in issues)
        else:
            site_context = "Strona wymaga modernizacji — przestarzały design, brak nowoczesnych elementów"

        if my_feedback:
            site_context += f"\n\nDodatkowe spostrzeżenia (wpleć naturalnie w email, nie wyróżniaj jako osobnej sekcji):\n{my_feedback}"

        prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla Sand'n Studio — dwuosobowego studia web developerskiego które oferuje modernizację strony lokalnej firmie.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
URL: {lead.get('website_url', '')}

=== CO ZNALAZŁ NA STRONIE ===
{site_context}

{stats_arsenal}

=== ZADANIE ===
Napisz cold email który SPRZEDAJE modernizację strony.

Struktura emaila (nie pisz nagłówków, po prostu tak go zbuduj):
1. TEMAT: konkretny i niepokojący — np. "Sprawdziłem stronę [firma] — jest jeden problem który kosztuje Cię klientów"
2. HOOK: zacznij od JEDNEGO konkretnego problemu który znalazłeś — opisz go tak jakbyś właśnie wyszedł ze strony, bo to prawda
3. KOSZT PROBLEMU: przetłumacz ten problem na realne straty klientów/pieniędzy — użyj jednej trafnej statystyki
4. RESZTA PROBLEMÓW: wymień 1-2 kolejne — ZAWSZE z konkretnymi liczbami lub faktami z danych
   Źle: "SEO wymaga poprawek" → Dobrze: "8 z 14 zdjęć nie ma alt text — Google ich nie indeksuje"
   Źle: "mało treści" → Dobrze: "297 słów na stronie głównej — Google potrzebuje 3x więcej dla lokalnego SEO"
   Źle: "H1 jest nieoptymalne" → Dobrze: "H1 'Doświadcz Prawdziwego Relaksu' jest piękny marketingowo, ale Google szuka tu frazy 'masaż Katowice'"
5. SOCIAL PROOF: wspomnij że pomogłeś już innym firmom w podobnej sytuacji, efekty
6. OFERTA: wspomnij że wyceniamy indywidualnie po rozmowie (bez podawania kwoty), płatność podzielona na dwie raty — połowa na start, reszta gdy strona się podoba. Agencje biorą 3000–8000 PLN, my znacznie mniej.
7. CTA: "Mamy już gotową listę konkretnych zmian dla [firma] — chce Pan/Pani zobaczyć?"

Zasady:
- Maksymalnie 200 słów
- Pisz w formie "my" — ZAWSZE liczba mnoga (jesteśmy dwuosobowym studiem). Nigdy "znalazłem/znalazłam/sprawdziłem" — tylko "znalezliśmy/sprawdziliśmy". Żadnych form pierwszej osoby liczby pojedynczej.
- KONKRETNY Z LICZBAMI — jeśli masz dane liczbowe (ile zdjęć, ile słów, jaki wynik PageSpeed), użyj ich. Ogólniki jak "SEO wymaga poprawek" są bezużyteczne i niszczą wiarygodność — właściciel wie że coś można poprawić, pytanie co konkretnie
- NIE brzmij pouczająco — nie oceniaj strony wprost, pokaż szansę którą tracą
  Źle: "Pana strona nie ma SSL" → Dobrze: "Szkoda żeby klienci widzieli 'Niezabezpieczona' zanim w ogóle zobaczą ofertę"
  Źle: "Strona jest nieresponsywna" → Dobrze: "Większość klientów szuka teraz na telefonie — warto to wykorzystać"
- Jedna konkretna statystyka (pasująca do głównego problemu)
- Pierwsza linia: Temat: [temat]
- Podpisz się: Sand'n Studio (Szymon i Nikodem)
- Nie używaj korporacyjnego języka
- Zacznij od haka, nie od "Dzień dobry"
- Wspomnij portfolio: sandnstudio.pl
- NIE dodawaj P.S. — CTA w punkcie 7 jest wystarczające i mówi dokładnie to samo co P.S. by mówił. Jeden wyraźny hak na końcu wystarczy.
"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text
