import os
import base64
import io
import anthropic


def _client():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _compress(png_bytes: bytes, max_width: int = 1100, quality: int = 72) -> bytes:
    """Resize + convert PNG to JPEG to reduce payload size. Falls back to raw PNG if Pillow missing."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except ImportError:
        return png_bytes  # Pillow not installed, send raw PNG


def analyze_website_visually(lead: dict, screenshots: dict, website_data: dict | None = None) -> str:
    """Full audit: desktop + mobile screenshots + scraped text → Claude deep analysis."""
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
            tech_facts.append(f"Meta desc: {website_data['meta_description'][:120]}")

    page_text = ""
    if website_data and website_data.get("text_preview"):
        page_text = f"\nTreść strony (fragment):\n{website_data['text_preview'][:1500]}"

    tech_block = "\n".join(tech_facts)

    desktop_bytes = (screenshots or {}).get("desktop")
    mobile_bytes  = (screenshots or {}).get("mobile")
    has_screenshots = bool(desktop_bytes or mobile_bytes)

    visual_instruction = (
        "Masz przed sobą zrzuty ekranu tej strony (desktop i mobile). Przeprowadź szczegółowy audyt wzrokowy i techniczny."
        if has_screenshots else
        "Nie masz zrzutów ekranu — przeprowadź audyt na podstawie danych technicznych i treści strony poniżej. Bądź równie konkretny i krytyczny."
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

Pisz po polsku. Bądź szczery i konkretny — jak gdybyś płacił za ten audyt. Używaj punktorów i nagłówków z powyższej struktury."""

    # Build message content — add screenshots only if available
    content = []

    if desktop_bytes:
        content.append({"type": "text", "text": "**Zrzut ekranu — DESKTOP (1280px):**"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(_compress(desktop_bytes)).decode("utf-8"),
            },
        })

    if mobile_bytes:
        content.append({"type": "text", "text": "**Zrzut ekranu — MOBILE (390px):**"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(_compress(mobile_bytes, max_width=600)).decode("utf-8"),
            },
        })

    content.append({"type": "text", "text": prompt})

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": content}],
    )

    return message.content[0].text


def generate_email(lead: dict, website_data: dict | None = None, ai_analysis: str | None = None) -> str:
    client = _client()

    business_name = lead.get("business_name", "")
    business_type = lead.get("business_type", "firma")
    city = lead.get("city", "")
    has_website = bool(lead.get("website_url"))

    # ── Shared context: who Szymon is ──
    sender_context = """
Kim jest Szymon (nadawca emaila):
- Młody web developer z Polski, robi strony dla lokalnych firm
- Zrealizował kilka projektów dla firm w regionie, klienci są zadowoleni
- Nie jest wielką agencją — jest to atut (szybko, tanio, bezpośredni kontakt)
- Cena: 500 PLN za stronę 4-podstronową (to mocno poniżej rynku — agencje biorą 3000-8000 PLN za to samo)
- Dostępny, odpowiada tego samego dnia
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

    if not has_website:
        prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla Szymona — web developera który oferuje zbudowanie strony lokalnej firmie.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Sytuacja: firma NIE MA strony internetowej w ogóle

{stats_arsenal}

=== ZADANIE ===
Napisz cold email który SPRZEDAJE. Nie informacyjny — sprzedażowy.

Struktura emaila (nie pisz nagłówków, po prostu tak go zbuduj):
1. TEMAT: intrygujący, konkretny, nie "Propozycja strony" — coś co wywołuje ciekawość lub lekki strach przed stratą
2. HOOK (pierwsze zdanie): zaskakujący fakt lub pytanie które boli — np. "Szukałem dziś {business_type} w {city} na Google — Pana firmy nie ma."
3. KOSZT BRAKU STRONY: przetłumacz brak strony na realne straty — ilu klientów szuka online i ich nie znajduje
4. SOCIAL PROOF: wspomnij że inne podobne firmy w regionie już to zrobiły i co zyskały (ogólnie, nie fake)
5. OFERTA + CENA: konkretnie — co dostaną, ile kosztuje, ile to trwa. Zakotwicz cenę (agencje biorą 5x więcej)
6. CTA: jedno proste działanie — nie "proszę o kontakt" ale konkretne np. "Czy mogę pokazać Panu przykładowy projekt w tym tygodniu?"

Zasady:
- Maksymalnie 180 słów (krótko = szanujemy czas)
- Pisz jak człowiek, nie jak bot ani agencja marketingowa
- Jedna konkretna statystyka pasująca do branży
- Pierwsza linia to: Temat: [temat]
- Podpisz się: Szymon
- Nie używaj słów: "pragnę", "uprzejmie", "niniejszym", "pozwalam sobie"
- Nie zaczynaj od "Dzień dobry" — zacznij od haka"""

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
            if website_data.get("uses_tables_layout"):
                issues.append("układ tabelkowy — design rodem z 2008 roku, wygląda nieprofesjonalnie")
            if website_data.get("has_dead_analytics"):
                issues.append("Google Analytics wyłączony od 2023 — właściciel nie widzi ilu klientów traci")
            score = website_data.get("pagespeed_score")
            if score is not None and score < 60:
                issues.append(f"PageSpeed {score}/100 — strona ładuje się bardzo wolno, większość użytkowników mobilnych wychodzi")
            elif score is not None and score < 80:
                issues.append(f"PageSpeed {score}/100 — strona ładuje się wolno na telefonie")

        if ai_analysis:
            site_context = f"Szczegółowa analiza AI strony:\n{ai_analysis[:1200]}"
        elif issues:
            site_context = "Konkretne problemy znalezione na stronie:\n" + "\n".join(f"- {i}" for i in issues)
        else:
            site_context = "Strona wymaga modernizacji — przestarzały design, brak nowoczesnych elementów"

        prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla Szymona — web developera który oferuje modernizację strony lokalnej firmie.

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
Napisz cold email który SPRZEDAJE modernizację strony. Nie "zauważyłem kilka rzeczy" — "Twoja strona traci Ci klientów i wiem jak to naprawić za 500 PLN."

Struktura emaila (nie pisz nagłówków, po prostu tak go zbuduj):
1. TEMAT: konkretny i niepokojący — np. "Sprawdziłem stronę [firma] — jest jeden problem który kosztuje Cię klientów"
2. HOOK: zacznij od JEDNEGO konkretnego problemu który znalazłeś — opisz go tak jakbyś właśnie wyszedł ze strony, bo to prawda
3. KOSZT PROBLEMU: przetłumacz ten problem na realne straty klientów/pieniędzy — użyj jednej trafnej statystyki
4. RESZTA PROBLEMÓW: wymień 1-2 kolejne (skrótowo)
5. SOCIAL PROOF: wspomnij że pomogłeś już innym firmom w podobnej sytuacji, efekty
6. OFERTA: konkretnie — 4-podstronowa modernizacja, 500 PLN, 7-14 dni. Zakotwicz cenę vs agencje (3000-8000 PLN)
7. CTA: jedno konkretne pytanie lub propozycja następnego kroku

Zasady:
- Maksymalnie 200 słów
- KONKRETNY — odwołuj się do rzeczy które naprawdę znalazłeś na ich stronie
- Pisz jak człowiek, bezpośrednio, po imieniu jeśli pasuje
- Jedna konkretna statystyka (pasująca do głównego problemu)
- Pierwsza linia: Temat: [temat]
- Podpisz się: Szymon
- Nie używaj korporacyjnego języka
- Zacznij od haka, nie od "Dzień dobry, nazywam się Szymon i..."
"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text
