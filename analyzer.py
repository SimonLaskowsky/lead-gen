import os
import json
import base64
import io
import anthropic


STYLE_RULES = """
=== ZASADY STYLU (bezwzgledne) ===
1. ZAKAZ MYSLNIKOW. Nie wolno uzyc pauzy: ani em dash (unicode U+2014), ani en dash (U+2013),
   ani zwyklego minusa uzytego jako pauza w zdaniu. Zero wyjatkow. Zamiast pauzy uzyj przecinka,
   dwukropka albo rozbij na dwa zdania.
   Lacznik wewnatrz wyrazu ("e-mail", "biało-czerwony") jest OK, pauza miedzy myslami NIE.
2. Nadawca jest JEDEN MEZCZYZNA. Pisz w pierwszej osobie liczby POJEDYNCZEJ rodzaju
   meskiego: "szukalem", "trafilem", "zrobilem", "jestem". NIGDY liczby mnogiej
   ("szukalismy", "jestesmy") i NIGDY rodzaju zenskiego, nawet gdy piszesz do kobiety
   i nawet gdy w tekscie przewijaja sie "klientki".
3. Zadnego twierdzenia, ktore odbiorca obali w 10 sekund. W szczegolnosci: nie twierdz,
   ze czegos "nie da sie kliknac" albo ze cos "nie dziala", jesli nie potwierdzaja tego
   wprost dane techniczne audytu. Wyglad na zrzucie to za malo. Firma MA wizytowke w Google,
   bo wlasnie stamtad mamy jej dane. Nie pisz, ze "nie ma jej w Google" ani ze "nie wyskakuje
   w wyszukiwarce". Brak strony WWW to co innego niz brak obecnosci w Google.
4. Nie obiecuj rzeczy, ktorych nie ma. Szkic strony wspominamy jako propozycje do zrobienia,
   nie jako gotowy plik czekajacy w folderze.
5. Bez P.S., bez emoji, bez wykrzyknikow, bez pogrubien i naglowkow sekcji.
6. Zanim oddasz tekst, sprawdz pisownie kazdego slowa. Jedna literowka w mailu
   wytykajacym cudze niedorobki kompromituje cala wiadomosc.
7. BADZ MILY. Piszemy do wlasciciela firmy, ktorej idzie dobrze. Braku strony nie stawiaj
   jako werdyktu ("Wlasnej strony brak."), tylko wplec go w zdanie o tym, co mozna zyskac.
8. Polskie znaki sa OBOWIAZKOWE w calym mailu: ą, ę, ć, ł, ń, ó, ś, ź, ż ("Dzień dobry",
   "wizytówka", "własnej"). Te instrukcje sa pisane bez polskich znakow, ale mail ma je miec
   w kazdym slowie. Mail bez polskich znakow wyglada jak spam z automatu.
9. Nazwa firmy: nazwa z Google bywa inna niż ta, którą firma sama nosi na stronie i na szyldzie.
   Nie odmieniaj, nie tłumacz i nie skracaj nazwy z Google (nie rób z "Willa" ani "Villa" formy
   "Villi"). Gdy nazwa ze strony różni się od nazwy z Google albo masz wątpliwość, pisz
   "Państwa stronę" albo podaj domenę. Nazwa własna ma wyglądać dokładnie tak, jak firma ją pisze.
10. Liczby z audytu przepisujesz, nie opisujesz: jeśli audyt podaje wagę, czas ładowania, liczbę
   ekranów albo kontrast, w mailu ma być ta liczba, nie "bardzo długo" ani "ogromny".
   Liczb, których w audycie nie ma, nie wymyślasz: żadnych własnych sekund, procentów ani godzin
   pracy. Czas ładowania bierzesz z szacunku w audycie, w tej samej skali.
"""


ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "claude-sonnet-5")
ANALYSIS_EFFORT = os.getenv("ANALYSIS_EFFORT", "low")
EMAIL_MODEL = os.getenv("EMAIL_MODEL", "claude-opus-5")
EMAIL_EFFORT = os.getenv("EMAIL_EFFORT", "medium")
FOLLOWUP_MODEL = os.getenv("FOLLOWUP_MODEL", "claude-sonnet-5")
DESKTOP_STRIPS = int(os.getenv("DESKTOP_STRIPS", "4"))
MOBILE_STRIPS = int(os.getenv("MOBILE_STRIPS", "2"))
LAPTOP_STRIPS = int(os.getenv("LAPTOP_STRIPS", "2"))

on_usage = None


def _client():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _record(message, purpose: str) -> None:
    usage = getattr(message, "usage", None)
    if on_usage is None or usage is None:
        return
    on_usage(purpose, getattr(message, "model", ""), usage.input_tokens, usage.output_tokens,
             getattr(usage, "cache_read_input_tokens", 0) or 0,
             getattr(usage, "cache_creation_input_tokens", 0) or 0)


def _text(message) -> str:
    """Tekst odpowiedzi. Na Opusie 5 content[0] bywa blokiem myslenia, nie tekstem."""
    return "".join(b.text for b in message.content if b.type == "text")


def _slice_page(png_bytes: bytes, max_width: int = 1100, strip_height: int = 1000,
                quality: int = 80, max_strips: int = 8) -> list[bytes]:
    """Tnie pelny zrzut strony na pasy w pelnej rozdzielczosci.
    API skaluje kazdy obraz do ok. 1.15 Mpx / 1568 px dluzszego boku, wiec jeden zrzut
    calej dlugiej strony robi sie nieczytelna miniaturka. Pasy mieszcza sie w limicie
    bez skalowania, model oglada strone sekcja po sekcji jak czlowiek przewijajacy na zywo."""
    try:
        from PIL import Image
    except ImportError:
        return [png_bytes]  # Pillow brak: wysylamy surowy PNG, API i tak go przeskaluje
    img = Image.open(io.BytesIO(png_bytes))
    ratio = min(max_width / img.width, 1.0)
    if ratio < 1.0:
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    strips = []
    for top in range(0, img.height, strip_height):
        if len(strips) >= max_strips:
            break
        strip = img.crop((0, top, img.width, min(top + strip_height, img.height)))
        if strip.height < 60 and strips:
            break  # kilkupikselowy ogon nic nie wnosi
        buf = io.BytesIO()
        strip.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        strips.append(buf.getvalue())
    return strips


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


def _above_fold(png_bytes: bytes, max_width: int, height: int, quality: int = 80) -> bytes | None:
    """Sam pierwszy ekran: to, co widzi gosc zanim zacznie przewijac."""
    try:
        from PIL import Image
    except ImportError:
        return png_bytes
    img = Image.open(io.BytesIO(png_bytes))
    ratio = min(max_width / img.width, 1.0)
    if ratio < 1.0:
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    crop = img.crop((0, 0, img.width, min(height, img.height)))
    buf = io.BytesIO()
    crop.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _parse_impression(raw: str) -> dict:
    out = {"text": raw.strip(), "first_impression": None, "design_year": None}
    if raw.startswith("IMPRESSION:"):
        line, _, rest = raw.partition("\n")
        out["text"] = rest.strip()
        for part in line[len("IMPRESSION:"):].strip().split():
            key, _, value = part.partition("=")
            if key == "score" and value.isdigit():
                out["first_impression"] = int(value)
            elif key == "year" and value.isdigit():
                out["design_year"] = value
    return out


def first_impression(lead: dict, screenshots: dict) -> dict | None:
    """Osobne, tanie przejscie: trzy pierwsze ekrany (desktop, laptop, telefon) i ocena
    oczami projektanta UI po trzech sekundach, z dowodami widocznymi na ekranie.
    Uniwersalne z zalozenia: nie szuka konkretnej listy usterek, tylko nazywa to,
    co psuje pierwsze wrazenie na TEJ stronie, i wskazuje, gdzie to widac."""
    desktop = (screenshots or {}).get("desktop")
    views = [
        ("DESKTOP 1280 px", desktop, 1280, 650),
        ("DETAL: góra strony w pełnej rozdzielczości (logo, nagłówek, nawigacja)", desktop, 1280, 400),
        ("LAPTOP 1024 px", (screenshots or {}).get("laptop"), 1024, 600),
        ("TELEFON 390 px", (screenshots or {}).get("mobile"), 390, 844),
    ]
    content = []
    for label, png, width, height in views:
        if not png:
            continue
        jpg = _above_fold(png, width, height)
        if not jpg:
            continue
        content.append({"type": "text", "text": f"**Pierwszy ekran, {label}:**"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(jpg).decode("utf-8")},
        })
    if not content:
        return None

    prompt = f"""Jesteś doświadczonym projektantem UI/UX. Oceniasz stronę lokalnej firmy tak, jak robi to człowiek: najpierw w trzy sekundy czujesz, czy to wygląda dobrze, potem umiesz powiedzieć dlaczego.

Firma: {lead.get('business_name', '')}
Typ biznesu: {lead.get('business_type', '')}
Rok: 2026

Masz przed sobą pierwszy ekran tej samej strony w trzech szerokościach: desktop, laptop i telefon, oraz osobny kadr górnej części strony w pełnej rozdzielczości, na którym widać kształt liter w logo, nagłówku i nawigacji tak, jak widzi je człowiek. Nie masz listy rzeczy do sprawdzenia. Patrzysz na całość i nazywasz to, co naprawdę buduje albo psuje pierwsze wrażenie NA TEJ STRONIE, cokolwiek to jest: krój i czytelność pisma, hierarchia, odstępy i wyrównanie, kontrast, jakość i dobór zdjęć, kolory, gęstość, zachowanie układu między szerokościami (ucinanie, nachodzenie, puste połacie), widoczność tego, po co gość przyszedł. Jeśli coś wygląda dobrze, powiedz to wprost, to też jest informacja.

Format odpowiedzi, dokładnie taki:
Pierwsza linia: IMPRESSION: score=X year=YYYY
  score to ocena 1-10 na tle dobrych stron tej branży z 2026 roku, year to rok, w którym ta strona wygląda, jakby została zaprojektowana.
Potem pusta linia i trzy krótkie akapity bez nagłówków:
1. Jedno zdanie: co czuje gość po trzech sekundach (zaufanie, obojętność, wrażenie zaniedbania, konsternacja) i co za to odpowiada.
2. Trzy najsilniejsze sygnały wizualne, od najważniejszego. Każdy w jednym-dwóch zdaniach: co widać, gdzie dokładnie na ekranie (np. menu po lewej, nagłówek sekcji, tło pod tekstem) i dlaczego to działa na odbiorcę. Tylko rzeczy, które właściciel sam zobaczy w dziesięć sekund po otwarciu własnej strony. Jeśli różne szerokości pokazują coś różnego, napisz na której.
3. Jedno zdanie: co dobra strona tej branży w 2026 roku pokazuje na pierwszym ekranie, a czego tu brakuje.

Zasady: po polsku, bez emoji, najwyżej 180 słów po pierwszej linii. Nie oceniaj klikalności elementów, tego zrzut nie pokazuje. Nie używaj słów "brzydka", "amatorska", "tandetna": nazwij konkret, nie werdykt."""
    content.append({"type": "text", "text": prompt})
    client = _client()
    message = client.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=1500,
        output_config={"effort": ANALYSIS_EFFORT},
        messages=[{"role": "user", "content": content}],
    )
    _record(message, "analysis")
    return _parse_impression(_text(message))


def analyze_website_visually(lead: dict, screenshots: dict, website_data: dict | None = None,
                             impression: str | None = None) -> dict:
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
        tech_facts.append(f"Klikalny numer (link tel: w HTML): {'tak' if website_data.get('has_tel_link') else 'NIE'}")
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
    laptop_bytes  = (screenshots or {}).get("laptop")
    mobile_bytes  = (screenshots or {}).get("mobile")
    has_screenshots = bool(desktop_bytes or laptop_bytes or mobile_bytes)
    impression_block = (
        f"\n=== PIERWSZE WRAŻENIE (osobne przejście projektanta UI, trzy pierwsze ekrany) ===\n{impression}\n"
        "Potraktuj to jako punkt wyjścia: rozwiń, potwierdź albo skoryguj na podstawie pełnych sekcji poniżej.\n"
        if impression else ""
    )

    visual_instruction = (
        "Masz przed sobą stronę pociętą na sekcje w pełnej rozdzielczości, od góry do dołu: najpierw DESKTOP, potem LAPTOP 1024 px, potem MOBILE. "
        "Obejrzyj każdą sekcję uważnie, łącznie z drobnym tekstem, jakością grafik i typografią, tak jakbyś przewijał stronę na żywo.\n"
        "WAŻNE: Dane tekstowe poniżej mogą być niekompletne jeśli strona używa JavaScript do renderowania treści. "
        "Zrzuty ekranu są źródłem prawdy — jeśli na screenshocie widać treść której nie ma w danych tekstowych, ufaj screenshotowi."
        if has_screenshots else
        "Nie masz zrzutów ekranu — przeprowadź audyt na podstawie danych technicznych i treści strony poniżej. Bądź równie konkretny i krytyczny.\n"
        "UWAGA: Strona może używać JavaScript do renderowania treści — dane tekstowe mogą być niekompletne."
    )

    mobile_section = (
        "**4. Doświadczenie mobilne (patrz na sekcje MOBILE)**\nCzy strona działa na telefonie? Co się psuje — tekst, przyciski, układ?"
        if has_screenshots else
        f"**4. Doświadczenie mobilne**\nBrak meta viewport: {'TAK — strona NIE jest responsywna' if website_data and not website_data.get('has_mobile_viewport') else 'jest responsywna'}. Oceń konsekwencje."
    )

    prompt = f"""Jesteś bezwzględnym, ale genialnym dyrektorem ds. konwersji i psychologii sprzedaży w internecie. Przeprowadzasz brutalnie szczery audyt strony polskiego lokalnego biznesu, aby znaleźć powody, przez które firma traci klientów na rzecz konkurencji.

=== DANE FIRMY ===
Firma: {lead.get('business_name', '')}
Typ biznesu: {lead.get('business_type', '')}
URL: {lead.get('website_url', '')}

=== WYNIKI AUTOMATYCZNYCH SPRAWDZEŃ ===
{tech_block}
{page_text}
{impression_block}
=== TWOJE ZADANIE ===
{visual_instruction}

Przeprowadź analizę, skupiając się na psychologii klienta i konwersji. Unikaj ogólników typu "strona jest ładna/nieładna". Pisz szczerze, bezpośrednio i technicznie konstruktywnie.
WAŻNE: zrzut ekranu NIE mówi, czy element jest klikalny. O klikalności numeru telefonu i linków wnioskuj WYŁĄCZNIE z danych automatycznych (linia "Klikalny numer"), nigdy z samego wyglądu.

Zastosuj poniższą strukturę:

**1. Analiza pierwszego ekranu (Above the fold) i pierwsze 3 sekundy**
- Co widzi użytkownik zanim zacznie przewijać stronę? Czy w ciągu 3 sekund wie CZYM zajmuje się firma i w JAKIM mieście/rejonie działa?
- Czy na pierwszym ekranie jest widoczny, bezpośredni i klikalny przycisk Call-To-Action (np. "Zadzwoń teraz", "Bezpłatna wycena")? Jeśli nie, opisz jak bardzo utrudnia to kontakt.

**2. Jakość wizualna oczami projektanta**
- Nie odhaczaj listy. Patrz na całość i nazwij to, co na TEJ stronie najbardziej buduje albo psuje wrażenie, w dowolnej z tych warstw: pismo (krój, czytelność liter i polskich znaków, hierarchia nagłówków, wyjustowanie z rzekami), układ (wyrównanie, odstępy, gęstość, puste połacie, ucinanie lub nachodzenie elementów między szerokościami DESKTOP, LAPTOP i MOBILE), kolor i kontrast (tekst na zdjęciach, kolory gryzące się ze sobą), zdjęcia i grafiki (jakość, spójność, stock, odznaki wklejone jako obrazki), hierarchia (czy oko wie, gdzie patrzeć, czy widać, po co gość przyszedł).
- Każde spostrzeżenie z dowodem: co widać i gdzie dokładnie na ekranie, tak żeby właściciel odnalazł to w dziesięć sekund. Jeśli coś jest zrobione dobrze, napisz to.
- Werdykt: z którego roku ta strona wygląda i czy w 2026 roku wytrzymuje porównanie z dobrymi stronami tej branży.

**3. Krytyczne błędy techniczne i zaufanie (Trust Flags)**
- Przeanalizuj wpływ braku SSL, błędów PageSpeed, braku nagłówka H1 lub martwego Google Analytics na biznes firmy.
- Jak błędy techniczne wpływają na pozycję w Google (SEO) oraz na podświadome poczucie bezpieczeństwa klienta, który ma podać swoje dane lub zadzwonić?

{mobile_section}

**5. Lista 3 najważniejszych zmian o najwyższym ROI**
- Wypisz dokładnie 3 konkretne, techniczne zmiany na stronie, które natychmiast podniosą liczbę telefonów i zapytań od klientów.

Pisz wyłącznie po polsku. Nie używaj emoji. Bądź precyzyjny.
Cała analiza ma się zmieścić w 400 słowach: krótkie akapity i punkty, konkret zamiast opisu, bez powtarzania danych wejściowych.

=== FORMAT ODPOWIEDZI ===
Zacznij odpowiedź od JEDNEJ linii z ocenami 1-10 (przed całą analizą):
SCORES: design=X mobile=X seo=X cta=X speed=X
(speed=null jeśli brak danych PageSpeed; null dla dowolnej kategorii jeśli nie możesz ocenić)
Potem pusta linia i pełna analiza."""

    # Build message content: strona pocieta na pasy, kazdy pas to osobny obraz
    content = []

    def _add_strips(png, label, **kw):
        strips = _slice_page(png, **kw)
        for i, jpg in enumerate(strips, 1):
            content.append({"type": "text", "text": f"**{label}, sekcja {i}/{len(strips)} (kolejno od góry strony):**"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.standard_b64encode(jpg).decode("utf-8")},
            })
        if len(strips) == kw.get("max_strips", 8):
            content.append({"type": "text", "text": f"({label}: strona jest dłuższa, dolna część poza kadrem)"})

    if desktop_bytes:
        _add_strips(desktop_bytes, "DESKTOP", max_width=900, strip_height=1000, max_strips=DESKTOP_STRIPS)
    if laptop_bytes:
        _add_strips(laptop_bytes, "LAPTOP 1024px", max_width=800, strip_height=1000, max_strips=LAPTOP_STRIPS)
    if mobile_bytes:
        _add_strips(mobile_bytes, "MOBILE 390px", max_width=500, strip_height=1500, max_strips=MOBILE_STRIPS)

    content.append({"type": "text", "text": prompt})

    # max_tokens z zapasem: na Opusie 5 tokeny myslenia wliczaja sie w limit
    message = client.messages.create(
        model=ANALYSIS_MODEL,
        max_tokens=6000,
        output_config={"effort": ANALYSIS_EFFORT},
        messages=[{"role": "user", "content": content}],
    )
    _record(message, "analiza")

    return _parse_analysis(_text(message))


OPT_OUT_LINE = "Jeśli nie chcą Państwo takich wiadomości, wystarczy odpisać jedno słowo: nie. Więcej nie napiszę."


def _with_opt_out(text: str) -> str:
    """Linia o wypisaniu jest doklejana w kodzie, bo model potrafi ją 'poprawić' (np. samo Pan)."""
    lines = [ln for ln in text.rstrip().split("\n") if "wystarczy odpisać" not in ln]
    return "\n".join(lines).rstrip() + "\n\n" + OPT_OUT_LINE


AUDIT_LOG_MARKER = "\n\nJak model do tego doszedł:"


def _audit_for_email(ai_analysis: str) -> tuple[str, str]:
    analysis_text = ai_analysis
    story = ""
    try:
        parsed = json.loads(ai_analysis)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        analysis_text = parsed.get("analysis") or ai_analysis
        story = str(parsed.get("story") or "").strip()
    log_start = analysis_text.find(AUDIT_LOG_MARKER)
    if log_start != -1:
        analysis_text = analysis_text[:log_start].rstrip()
    return analysis_text, story


MAKIETA_W_ZALACZNIKU = """
=== PROJEKT JEST JUŻ ZROBIONY I WISI W ZAŁĄCZNIKU ===
Do tego maila dołączony jest obrazek: gotowy projekt nowej strony głównej tej firmy,
złożony z ICH własnych zdjęć, ich nazwy i ich treści.

To zmienia całą prośbę w mailu i ma pierwszeństwo nad punktami o raporcie i o podglądzie:
- Nie proponuj, że coś przygotujesz. To jest już przygotowane, wystarczy otworzyć załącznik.
- Napisz jednym zdaniem, że w załączniku jest projekt nowej strony zrobiony na ich materiałach,
  i że nic za to nie płacą ani do niczego się nie zobowiązują.
- Zwróć uwagę, że użyłeś ICH zdjęć. To jest najmocniejszy punkt tego maila, bo dowodzi,
  że ktoś naprawdę wszedł na ich stronę, a nie wysłał masówki.
- Prośba na końcu dotyczy odpowiedzi, nie wysyłki: zapytaj, czy mają rzucić okiem
  i czy odesłać wycenę, jeśli projekt im się spodoba.
- Ani słowa o kwotach. Wycena jest tematem po ich odpowiedzi.
- Nie opisuj projektu sekcja po sekcji. Oni go zaraz zobaczą, opis jest zbędny.
"""


def _z_linkiem(text: str, link: str, sig: str) -> str:
    """Mail ścieżki B bez linku jest pusty, więc gdy model go zgubi, wstawiamy go przed podpisem."""
    if not link or link in text:
        return text
    pierwsza_linia_podpisu = (sig or "").splitlines()[0] if sig else ""
    if pierwsza_linia_podpisu and pierwsza_linia_podpisu in text:
        return text.replace(pierwsza_linia_podpisu, f"{link}\n\n{pierwsza_linia_podpisu}", 1)
    return text.rstrip() + "\n\n" + link


def generate_email(lead: dict, website_data: dict | None = None, ai_analysis: str | None = None, my_feedback: str | None = None, profile: dict | None = None, has_mockup: bool = False, sciezka: str = "audyt", mockup_link: str = "") -> str:
    client = _client()
    p = profile or {}
    snd = {
        "name":   p.get("name") or "Szymon Laskowski",
        "domain": p.get("domain") or "",
        "phone":  p.get("phone") or "",
        "bio": "; ".join(x for x in (
            p.get("experience"),
            f"realizacje: {p['realizations']}" if p.get("realizations") else "",
        ) if x) or "programista, robi strony dla lokalnych firm",
    }
    sig = "\n".join(x for x in (snd["name"], snd["domain"], snd["phone"]) if x)

    business_name = lead.get("business_name", "")
    business_type = lead.get("business_type", "firma")
    city = lead.get("city", "")
    has_website = bool(lead.get("website_url"))

    # ── Shared context: who we are ──
    sender_context = f"""
Kim jest nadawca emaila:
- {snd['name']}, programista z regionu, robi strony dla lokalnych firm
- {snd['bio']}
- Kontakt i portfolio: {snd['domain'] or snd['name']}
- Jedna konkretna osoba, nie agencja i nie studio, to atut: szybko, bez biurokracji, kontakt wprost z wykonawcą
- Odpowiada tego samego dnia

Oferta. To sa DWIE ROZNE rzeczy i nigdy nie wolno ich mieszac ani wyceniac tak samo:

1. NOWA STRONA (landing page, strona wizytowka) czyli budowa od zera: od 3000 PLN.
2. ULEPSZENIA istniejacej strony (poprawki, modernizacja, redesign): to NIE jest budowa nowej strony.
   Zakres bywa rozny, wiec cena jest zawsze inna niz przy nowej stronie i ustalana indywidualnie.

ZASADA PIENIEZNA W PIERWSZYM MAILU: nie pisz NIC o pieniadzach. Zadnych kwot, zadnych widelek,
zadnych warunkow platnosci, zadnego "polowa na start". Powyzsze ceny sa dla Ciebie jako kontekst,
kim jest nadawca, a nie tresc do wklejenia. Pierwszy kontakt sprzedaje bezplatna probke pracy, nie usluge:
firmie ze strona raport z przegladu, firmie bez strony podglad nowej strony.
O pieniadzach rozmawiamy dopiero, gdy klient sam odpisze.
"""

    # ── Proven statistics to use ──
    stats_arsenal = """
Statystyki które można użyć (tylko te pasujące do konkretnych problemów tej firmy):
- 60% ruchu w internecie pochodzi z telefonów, strona nieresponsywna traci ponad połowę odwiedzających
- Strony ładujące się ponad 3 sekundy tracą 53% użytkowników mobilnych (Google)
- 75% użytkowników ocenia wiarygodność firmy po wyglądzie strony
- Bez SSL Chrome wyświetla "Niezabezpieczona" zanim klient w ogóle zobaczy stronę (fakt, nie statystyka)
- Brak CTA (przycisku "zadzwoń/napisz") to najczęstsza przyczyna ucieczki klientów ze strony
- Strony z opiniami klientów konwertują o 270% lepiej (badanie Spiegel Research Center)
Używaj TYLKO 1-2 statystyk pasujących do problemów tej konkretnej firmy. Nie wymieniaj wszystkich.
Jesli zadna nie pasuje do realnych problemow tej firmy, nie uzywaj zadnej: statystyka na sile brzmi jak masowka.
"""

    style_rules = STYLE_RULES

    if sciezka == "makieta":
        prompt = f"""Jesteś copywriterem piszącym krótkiego, bezpretensjonalnego cold maila po polsku
dla {snd['name']}, programisty robiącego strony lokalnym firmom.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Nazwa ze strony (bywa inna niż w Google, nie odmieniaj żadnej z nich): {(website_data or {}).get('title') or 'brak'}
Typ biznesu: {business_type}
Miasto: {city}
Ich obecna strona: {lead.get('website_url', '')}
{f"Dodatkowe spostrzeżenia (wpleć naturalnie): {my_feedback}" if my_feedback else ""}

=== CO JEST GOTOWE ===
Nadawca zbudował podgląd nowej strony głównej tej firmy, z ich własnych zdjęć i ich treści.
Podgląd wisi pod tym adresem i to jest cała treść tego maila:
{mockup_link}

{style_rules}

=== ODSTĘPSTWO OD ZASAD STYLU ===
Punkt 4 zasad stylu nie obowiązuje w tym mailu. Podgląd nie jest propozycją do zrobienia,
tylko gotową stroną, która czeka pod linkiem. Wolno o niej pisać jak o czymś, co istnieje.

=== ZADANIE ===
Napisz maila, który pokazuje, a nie ocenia. Reguły są twarde:

1. ZERO OCENY ADRESATA. Zakazane jest każde zdanie sugerujące, że ich strona jest słaba,
   przestarzała albo że ta wersja jest lepsza. "Zrobiłem Państwa stronę lepiej", "odświeżyłem",
   "unowocześniłem", "przydałoby się" to zdania zakazane. Ocena zmusza odbiorcę do obrony,
   a on niczego nie zamawiał.
2. ZERO LISTY WAD. Ani jednej. Ta ścieżka niczego nie zarzuca, ona pokazuje.
3. Rama ciekawostkowa: zrobiłem podgląd, jak mogłaby wyglądać strona [nazwa firmy],
   nic Państwo nie zamawiali, wrzucam link, jakby był ciekawy. Napisz to własnymi słowami,
   nie przepisuj tego zdania dosłownie.
4. Jedno zdanie o tym, że podgląd stoi na ICH zdjęciach i ICH treści. To dowód, że ktoś
   naprawdę wszedł na ich stronę, a nie wysłał masówki.
5. NAJWYŻEJ 80 SŁÓW razem z tematem. Cała treść to link, reszta ma zejść mu z drogi.
6. JEDNA prośba, na końcu, miękka: żeby rzucili okiem albo odpisali, co o tym myślą.
   Żadnego drugiego pytania, żadnej propozycji rozmowy, spotkania, wyceny ani raportu
   z przeglądu. Raport jest ofertą innej ścieżki i w tym mailu nie istnieje.
7. Link wklej DOKŁADNIE tak, jak jest wyżej, w osobnej linii, bez skracania i bez nawiasów.
8. Temat najwyżej 60 znaków, spokojny, bez obietnic i bez straszenia.
9. Zero kwot, zero widełek, zero słowa o cenie. Zero emoji, zero wypunktowań, zero P.S.
10. Per Pan/Pani, zwrot otwierający "Dzień dobry,".

Odpowiedz wyłącznie gotową treścią maila, bez komentarzy przed ani po.
Pierwsza linia: Temat: [temat]

Podpisz maila dokładnie tak:
{sig}"""
        message = client.messages.create(
            model=EMAIL_MODEL,
            max_tokens=8000,
            output_config={"effort": EMAIL_EFFORT},
            messages=[{"role": "user", "content": prompt}],
        )
        _record(message, "mail")
        return _with_opt_out(_z_linkiem(_text(message), mockup_link, sig))

    outsourced = (website_data or {}).get("outsourced_platform")
    if outsourced:
        pitch = (website_data or {}).get("outsourced_pitch", "korzystają z zewnętrznej platformy")

        # Booking platforms (Booksy, Fresha, Treatwell, Znany Lekarz) are marketplaces ,
        # don't suggest replacing them (they lose new customer flow). Instead: own site + keep the widget.
        booking_platforms = {"Booksy", "Fresha", "Treatwell", "Znany Lekarz"}
        is_booking_platform = outsourced in booking_platforms

        if is_booking_platform:
            prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla {snd['name']}, programisty robiącego strony dla lokalnych firm.

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
1. TEMAT: konkretny, najwyzej 60 znakow, np. "Znalazłem {business_name} na {outsourced}, brakuje jednej rzeczy"
2. HOOK: komplementuj, mają dobre opinie/profil na {outsourced}, ale Google ich nie pokazuje gdy ktoś szuka bezpośrednio
3. PROBLEM: klienci którzy nie szukają przez {outsourced} (np. z polecenia, z Google) nie mają gdzie trafić, tracą część ruchu
4. ROZWIĄZANIE: własna strona z widgetem {outsourced} wbudowanym, rezerwacje zostają, dochodzi SEO i marka premium
5. CTA: bezplatny podglad nowej strony dla Panstwa (projekt graficzny) wraz z wycena, bez zobowiazan, zakonczony uprzejmym pytaniem w pelnym zdaniu

Zasady:
- Maksymalnie 180 słów
- Ani slowa o cenie i platnosciach, to temat na pozniej
- Pisz w pierwszej osobie liczby pojedynczej rodzaju męskiego ("znalazłem", "sprawdziłem"). Żadnych form "my" ani liczby mnogiej.
- Doceniaj {outsourced}, nie atakuj go, firma słusznie go używa
- NIE brzmij pouczająco, pokaż szansę którą tracą, nie że coś zepsuli
- Odpowiedz wylacznie gotowa trescia maila, bez zadnych komentarzy przed ani po. Pierwsza linia: Temat: [temat]
- Podpis, dokladnie te linie i zadne inne:
{sig}
- Nie używaj korporacyjnego języka
- Zacznij od "Dzień dobry," i zaraz po nim hak, bez zdania rozbiegowego
- NIE dodawaj P.S., CTA w punkcie 5 jest wystarczające
"""
        else:
            # Social/link platforms (Facebook, Instagram, Linktree, Google Sites), these are weak presences,
            # proposing a real website as replacement makes sense here.
            prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla {snd['name']}, programisty który oferuje własną stronę lokalnej firmie.

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
2. HOOK: zauważyłeś, że ich jedyną obecnością w sieci jest profil na {outsourced}, Google ich nie pokazuje gdy ktoś szuka ich branży w mieście
3. KOSZT BRAKU STRONY: klienci z Google trafiają do konkurencji, nie do nich
4. ALTERNATYWA: własna strona, własna domena, SEO, marka premium. Ani slowa o cenie i platnosciach.
5. CTA: bezplatny podglad nowej strony dla Panstwa (projekt graficzny) wraz z wycena, bez zobowiazan, zakonczony uprzejmym pytaniem w pelnym zdaniu

Zasady:
- Maksymalnie 180 słów
- Pisz w pierwszej osobie liczby pojedynczej rodzaju męskiego ("znalazłem", "sprawdziłem"). Żadnych form "my" ani liczby mnogiej.
- NIE brzmij pouczająco, pokaż szansę którą tracą
- Odpowiedz wylacznie gotowa trescia maila, bez zadnych komentarzy przed ani po. Pierwsza linia: Temat: [temat]
- Podpis, dokladnie te linie i zadne inne:
{sig}
- Nie używaj korporacyjnego języka
- Zacznij od "Dzień dobry," i zaraz po nim hak, bez zdania rozbiegowego
- NIE dodawaj P.S., CTA w punkcie 5 jest wystarczające
"""
        message = client.messages.create(
            model=EMAIL_MODEL,
            max_tokens=8000,
            output_config={"effort": EMAIL_EFFORT},
            messages=[{"role": "user", "content": prompt}],
        )
        _record(message, "mail")
        return _with_opt_out(_text(message))

    inactive_reason = (website_data or {}).get("inactive_reason")
    if has_website and inactive_reason:
        evidence = (website_data or {}).get("inactive_evidence") or ""
        evidence_line = f'Komunikat, ktory widzi gosc (dosłownie): "{evidence}"' if evidence else "Zadnego komunikatu do zacytowania, gosc widzi po prostu blad."
        domain = lead.get("website_url", "").split("://")[-1].removeprefix("www.").strip("/")
        prompt = f"""Jestes copywriterem piszacym cold email sprzedazowy po polsku dla {snd['name']}, programisty ktory oferuje zbudowanie strony lokalnej firmie.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Adres strony podany w wizytowce Google: {lead.get('website_url', '')}
Sytuacja: pod tym adresem NIE MA dzialajacej strony. Co dokladnie jest: {inactive_reason}.
{evidence_line}
{f"Dodatkowe spostrzezenia (wplec naturalnie): {my_feedback}" if my_feedback else ""}

{style_rules}

=== KONTEKST ===
To inna sytuacja niz firma bez strony i inna niz strona do poprawy. Firma MA adres strony w wizytowce
Google, wiec wizytowka sprowadza ruch, ale kto kliknie w ten adres, trafia na komunikat albo blad
zamiast oferty. Klient porownujacy kilka firm po takim widoku zaklada, ze firma nie dziala albo o siebie
nie dba, i idzie do konkurencji. Wlasciciel czesto o tym nie wie (ktos nie przedluzyl hostingu, wykonawca
zniknal), wiec ton jest zyczliwy: informuje, nie wytykam.
Nie ma czego poprawiac, wiec NIE proponuj naprawy, przywrocenia ani poprawek starej strony.
Oferta to NOWA strona: bezplatny podglad (projekt graficzny, jak moglaby wygladac) razem z wycena, bez zobowiazan.

=== ZADANIE ===
Napisz krotki cold email. Ma doprowadzic do odpowiedzi, nie do wyceny.

Struktura (nie pisz numerow ani naglowkow):
1. TEMAT: nazwij obserwacje, bez straszenia, najwyzej 60 znakow, np. "Co widzi klient po kliknięciu w {domain}".
2. OTWARCIE: "Dzien dobry," i od razu obserwacja: kliknalem w adres strony z Panstwa wizytowki w Google
   i zamiast strony zobaczylem to, co stoi w "Sytuacja". Opisz DOKLADNIE to i nic wiecej. Jesli jest
   komunikat do zacytowania, zacytuj go w cudzyslowie, krotko.
3. CO TO KOSZTUJE: jedno, najwyzej dwa zdania o kliencie, ktory porownuje kilka firm z branzy w Google
   i po takim widoku wybiera inna. Bez statystyk, bez procentow, bez pouczania.
4. KIM JESTEM: pol zdania, {snd['name']}, programista z regionu, robi strony dla lokalnych firm.
   Mozna skleic z punktem 5 w jedno zdanie.
5. CTA: proponuje BEZPLATNY podglad nowej strony dla Panstwa (projekt graficzny) wraz z wycena,
   bez zobowiazan. Slowo "podglad" wprowadz zdanie wczesniej, zanim o niego zapytasz.
   To jest jedyna rzecz, o ktora prosze w tym mailu.
6. Podpis, dokladnie te linie i zadne inne:
{sig}

Zasady:
- Maksymalnie 90 slow razem z tematem. To twardy limit, krotszy mail wygrywa.
- Najwyzej trzy krotkie akapity, kazdy najwyzej dwa zdania.
- ANI SLOWA O PLATNOSCIACH. Zero cen, zero widelek, zero rat.
- ZADNYCH STATYSTYK ani procentow.
- Nie pisz, ze firma "nie ma strony": ma adres, ale strona pod nim nie dziala. Nie zgaduj przyczyny
  (nie pisz "hosting wygasl", "nie zaplacili"), chyba ze komunikat mowi to wprost. Przepisz sytuacje
  z danych, nie dopisuj wlasnych szczegolow.
- Nie pisz o SSL, SEO, H1, meta description ani o innych technikaliach: przy stronie, ktorej nie ma,
  to nie ma znaczenia i brzmi jak automat.
- CTA ma byc uprzejmym pytaniem w pelnym zdaniu, np. "Czy moge przygotowac i podeslac taki podglad?".
  ZAKAZ jednowyrazowych zaczepek typu "Zainteresowana?", "Zainteresowany?", "Chetnie?".
- Zwroty grzecznosciowe dopasuj do plci wlasciciela, jesli da sie ja wywnioskowac z nazwy firmy
  ({business_name}). W razie najmniejszej watpliwosci pisz bezosobowo albo "Panstwa".
- Podkresl, ze podglad jest bezplatny i do niczego nie zobowiazuje.
- Odpowiedz wylacznie gotowa trescia maila, bez komentarzy przed ani po.
  Pierwsza linia to: Temat: [temat]
- Nie uzywaj slow: "pragne", "uprzejmie", "niniejszym", "pozwalam sobie", "oferta"

WAZNE O ORYGINALNOSCI: powyzsza struktura to szkielet, nie gotowy tekst do przepisania.
Kazdy mail ma byc napisany od nowa pod konkretna firme ({business_name}, {business_type}, {city}).
Dwa maile do dwoch roznych firm nie moga brzmiec jak ten sam tekst z podmieniona nazwa.
"""

    elif not has_website:
        prompt = f"""Jesteś copywriterem piszącym cold email sprzedażowy po polsku dla {snd['name']}, programisty który oferuje zbudowanie strony lokalnej firmie.

{sender_context}

=== DANE FIRMY ===
Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
Sytuacja: firma NIE MA strony internetowej w ogóle
{f"Dodatkowe spostrzeżenia (wpleć naturalnie): {my_feedback}" if my_feedback else ""}

{style_rules}
{MAKIETA_W_ZALACZNIKU if has_mockup else ""}

=== ZADANIE ===
Napisz krotki cold email sprzedazowy. Ma doprowadzic do odpowiedzi, nie do wyceny.

Struktura (nie pisz numerow ani naglowkow):
1. TEMAT: konkretny, bez obietnic i bez strachu. Nazwij sytuacje, dodaj lekka luke informacyjna.
   Najwyzej 60 znakow, dluzszy temat skrzynki ucinaja.
2. OTWARCIE: "Dzien dobry," a potem zyczliwa obserwacja o ich wizytowce i o tym,
   ze wlasnej strony jeszcze nie maja.
3. CO MOZNA ZYSKAC: jedno zdanie o kliencie, ktory porownuje kilka firm z branzy.
   Bez statystyk, bez procentow, bez pouczania.
4. KIM JESTEM: pol zdania, {snd['name']}, programista z regionu, robi strony
   dla lokalnych firm. Mozna skleic z punktem 5 w jedno zdanie.
5. CTA: proponuje BEZPLATNY podglad nowej strony dla Panstwa (projekt graficzny, jak mogloby
   to wygladac) wraz z wycena, bez zobowiazan. Slowo "podglad" wprowadz zdanie wczesniej,
   zanim o niego zapytasz. To jest jedyna rzecz, o ktora prosze w tym mailu.
6. Podpis, dokladnie te linie i zadne inne:
{sig}

Zasady:
- Maksymalnie 60 slow razem z tematem. To twardy limit, krotszy mail wygrywa.
- Najwyzej trzy krotkie akapity, kazdy najwyzej dwa zdania.
- ANI SLOWA O PLATNOSCIACH. Zero cen, zero widelek, zero rat, zero "polowa na start".
  Pierwszy mail sprzedaje darmowy podglad, nie usluge. Pieniadze sa tematem na pozniej.
- ZADNYCH STATYSTYK ani procentow. Brzmia jak wypelniacz i obnizaja wiarygodnosc.
- CTA ma byc uprzejmym pytaniem w pelnym zdaniu, np. "Czy moge przygotowac i podeslac
  taki podglad?" albo "Chcialaby Pani zobaczyc podglad, jak taka strona moglaby wygladac?".
  ZAKAZ jednowyrazowych zaczepek typu "Zainteresowana?", "Zainteresowany?", "Chetnie?".
  Brzmia infantylnie i spoufalaja sie z osoba, ktorej nie znamy.
- Zwroty grzecznosciowe dopasuj do plci wlasciciela, jesli da sie ja wywnioskowac
  z nazwy firmy ({business_name}). W razie najmniejszej watpliwosci pisz bezosobowo
  albo "Panstwa": pomylka plci konczy rozmowe zanim sie zaczela.
- Podkresl, ze podglad jest bezplatny i do niczego nie zobowiazuje.
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
            analysis_text, story = _audit_for_email(ai_analysis)
            story_line = f"Dominująca historia wg audytu (o tym ma być mail): {story}\n\n" if story else ""
            site_context = f"{story_line}Audyt strony, jedyne źródło wiedzy o niej:\n{analysis_text}"
        elif issues:
            site_context = (
                "Automatyczny skaner kodu, bez oglądania strony. Każdy punkt to podejrzenie, nie pewnik,\n"
                "bo skaner czyta surowy HTML i nie widzi podstron ani treści dorysowanej JavaScriptem:\n"
                + "\n".join(f"- {i}" for i in issues)
            )
        else:
            site_context = "Nie ma ani audytu, ani niczego z skanera. Nie masz podstaw, żeby cokolwiek tej firmie zarzucić."

        if my_feedback:
            site_context += f"\n\nDodatkowe spostrzeżenia (wpleć naturalnie w email, nie wyróżniaj jako osobnej sekcji):\n{my_feedback}"

        audit_text = site_context

        prompt = f"""Jesteś genialnym copywriterem specjalizującym się w cold mailingu B2B do lokalnych firm handlowo-usługowych w Polsce. Twoim zadaniem jest napisanie otwierającej wiadomości e-mail na podstawie dostarczonego audytu strony internetowej.

=== DANE FIRMY ===
Nazwa w Google (bywa inna niż na stronie, nie odmieniaj jej): {business_name}
Nazwa, którą firma nosi na własnej stronie (tytuł strony): {(website_data or {}).get('title') or 'brak'}
Typ biznesu: {business_type}
URL: {lead.get('website_url', '')}

=== WYNIKI AUDYTU STRONY (ŹRÓDŁO WIEDZY) ===
{audit_text}
{sender_context}
{style_rules}
{MAKIETA_W_ZALACZNIKU if has_mockup else ""}
=== WYTYCZNE DLA COLD MAILA ===
1. Zwrot do adresata: "Dzień dobry" lub "Panie/Pani [imię]", ale imienia uzyj TYLKO jesli wystepuje w nazwie firmy, nigdy go nie zgaduj. Pisz per Pan/Pani, szanując tradycyjne podejście lokalnych przedsiębiorców. Żadnego "Cześć" na start.
2. Temat maila: najwyzej 60 znakow, oparty na CIEKAWOSCI, nie na strachu. Nazwij konkretną obserwację ze strony i zostaw lukę informacyjną (np. "Rzut oka na [domena] oczami klienta z telefonu", "Kilka rzeczy na [domena], które łatwo poprawić"). Zakaz straszenia utratą klientów w temacie.
3. Wstęp: Wykorzystaj kontekst lokalny i psychologiczny, ale opisuj to, co nadawca naprawdę zrobił: obejrzał stronę tak, jak widzi ją klient na telefonie i na komputerze (np. "Obejrzałem Państwa stronę tak, jak widzi ją klient z [Miasto/Region] szukający [usługi] na telefonie..."). Nie pisz o wpisywaniu nazwy w Google ani o innych czynnościach, których audyt nie obejmował.
4. Rozwinięcie (NAJWAŻNIEJSZE, tu pokazujesz głębię analizy): najpierw ustal DOMINUJĄCĄ HISTORIĘ audytu, czyli to, co naprawdę kosztuje firmę klientów, i o niej napisz. Jeśli audyt takiej historii nie pokazuje, NIE WYMYŚLAJ jej: napisz krótko, że strona jest zrobiona porządnie, i zaproponuj raport jako drugie spojrzenie, a nie jako listę zarzutów. Mail bez zarzutu jest lepszy niż mail z zarzutem naciąganym. Nie wybieraj pojedynczego technicznego detalu, gdy audyt mówi, że problemem jest całość.
   - Jeśli audyt stwierdza, że strona jest wizualnie niespójna, przestarzała lub wygląda nieprofesjonalnie, historią jest ZAUFANIE: klient ocenia wiarygodność firmy po stronie zanim zadzwoni i część wybiera konkurenta, który wygląda poważniej. Napisz to dyplomatycznie, nigdy "brzydka" ani "amatorska", tylko np. "strona nie gra w tej samej lidze co Państwa usługi" albo "odstaje od konkurencji, przez co część klientów odpada zanim zadzwoni". Poprzyj to DWOMA najbardziej widocznymi konkretami z audytu, wplecionymi w zdania.
   - Jeśli strona wygląda porządnie, a audyt wskazuje jeden krytyczny błąd konwersji, rozwiń ten jeden błąd w 2-3 zdaniach językiem korzyści.
   Zawsze wybieraj to, co właściciel sam zobaczy w 10 sekund po otwarciu własnej strony na telefonie. Jeśli audyt podaje dominującą historię, to jest ta historia; nie zastępuj jej własnym wyborem.
5. Sygnał głębi BEZ listy, TYLKO gdy jest z czego: jeżeli audyt wymienia jeszcze jakieś drobiazgi, dodaj JEDNO zdanie, że przy przeglądzie wyszło ich kilka (możesz nazwać najwyżej dwie, wplecione w naturalne zdanie, żadnych wypunktowań) i że pełną listę dostanie w raporcie. To zdanie jest OPCJONALNE. Jeżeli audyt nie wymienia nic poza główną historią, po prostu je pomiń: dopisanie na siłę drugiej wady jest gorsze niż jej brak, bo właściciel od razu pozna, że to wypełniacz. Bierz wyłącznie rzeczy, które audyt naprawdę stwierdził i które klient firmy odczuwa (przycisk prowadzący donikąd, brak mapy, literówka), nie technikalia (przekierowanie https, analityka, robots, sitemap, canonical).
6. Kim jestem: przedstaw nadawcę w jednym-dwóch zdaniach na bazie sekcji "Kim jest nadawca": imię i nazwisko, doświadczenie, jedna-dwie imienne realizacje. Zero ogólników typu "wiele firm mi zaufało". (dane kontaktowe są w podpisie, nie powtarzaj ich w treści).
7. Wycena: to sa ULEPSZENIA istniejacej strony, a nie budowa nowej, wiec zakres i cena sa zawsze indywidualne. NIE podawaj ZADNEJ kwoty, ani widelek, ani stawek agencji, ani warunkow platnosci. Napisz tylko, ze wycene przygotowuje indywidualnie po obejrzeniu zakresu i ze dolacza ja do raportu.
8. Call to Action: Zaproponuj, że podeślesz bezpłatny raport z przeglądu: pełną listę tego, co znalazłeś, z opisem co i jak poprawić, oraz indywidualną wycenę. Dopiero potem zapytaj, np.: "Czy mogę podesłać taki raport do rzucenia okiem?". Słowo "raport" musi być wprowadzone zdanie wcześniej, zanim o nim zapytasz. Żadnego "podglądu", "makiety" ani "projektu" strony, to oferta dla firm bez strony. To jest JEDYNA prośba w mailu, nie dodawaj innych pytań ani ofert.

=== ZASADY STYLU ===
- Maksymalnie 120 słów razem z tematem. Dłuższego cold maila właściciel firmy nie doczyta do CTA. Jeśli musisz ciąć, tnij opis zespołu, nie główny problem.
- Pisz zwięźle i konkretnie, bez lania wody i bez marketingu korporacyjnego.
- Mail ma wyglądać jak życzliwa obserwacja od człowieka, który naprawdę przeszedł stronę, nie jak protokół kontroli i nie jak szablon.
- Jeden dobrze rozwinięty problem robi większe wrażenie niż lista zarzutów. Żadnych wypunktowań w treści maila.
- ZAKAZ ZARZUTU, KTÓRY PRZECZY INNEMU ZDANIU W TYM SAMYM MAILU. Zanim wyślesz, przeczytaj całość i sprawdź, czy dwa zdania nie mówią czegoś przeciwnego. Jeśli piszesz, że przycisk prowadzi do kontaktu, nie wolno ci zarazem zarzucać, że nie ma jak się skontaktować. Jeśli chwalisz zdjęcia, nie zarzucaj potem słabej oprawy wizualnej.
- To, że firma zaprasza klienta do kontaktu, jest ZALETĄ, nie wadą. Przycisk prowadzący do telefonu, maila albo formularza to jest dokładnie to, co taka strona ma robić. Zarzut można postawić tylko wtedy, gdy audyt pokazuje, że kontakt jest utrudniony (przycisk prowadzi donikąd, numeru nie da się kliknąć, formularz nie działa), a nie za samo istnienie kontaktu.
- Nie zamieniaj braku funkcji w wadę, dopóki audyt nie mówi, że gość jej tam szukał. Pensjonat bez formularza, ale z widocznym telefonem i rezerwacją online, niczego nie traci.
- Całkowity zakaz używania emoji.
- Odpowiedz WYŁĄCZNIE gotową treścią maila (Temat + Treść), bez żadnych dodatkowych komentarzy od AI przed czy po tekście.

Podpisz maila dokladnie tak:
{sig}"""

    message = client.messages.create(
        model=EMAIL_MODEL,
        max_tokens=8000,
        output_config={"effort": EMAIL_EFFORT},
        messages=[{"role": "user", "content": prompt}],
    )
    _record(message, "mail")

    return _with_opt_out(_text(message))


def generate_followup(lead: dict, followup_number: int = 1) -> str:
    """Follow-up do wyslanego cold maila. Zwraca sama tresc (bez tematu), do wyslania w tym samym watku."""
    client = _client()
    prev_email = lead.get("generated_email", "")
    if followup_number <= 1:
        goal = (
            "To PIERWSZY follow-up, wysylany kilka dni po pierwszym mailu bez odpowiedzi.\n"
            "Cel: krotkie, zyczliwe przypomnienie i ponowienie propozycji z pierwszego maila: bezplatny raport z przegladu strony albo podglad nowej strony, dokladnie to i tym samym slowem, co w pierwszym mailu.\n"
            "Zakaz zdan w stylu 'czy dotarl moj poprzedni mail' i jakichkolwiek pretensji o brak odpowiedzi.\n"
            "Mozesz dodac JEDEN nowy drobny konkret lub argument, ktorego nie bylo w pierwszym mailu."
        )
    else:
        goal = (
            "To DRUGI i OSTATNI follow-up (break-up mail).\n"
            "Cel: uprzejmie zamknac temat. Napisz wprost, ze to moja ostatnia wiadomosc i nie bede juz przypominac.\n"
            "Zostaw otwarta furtke: propozycja z pierwszego maila (raport z przegladu albo podglad nowej strony) pozostaje aktualna, wystarczy krotka odpowiedz, gdy temat wroci.\n"
            "Zero wyrzutow, zero dramatu, lekki i zyczliwy ton."
        )
    prompt = f"""Jestes copywriterem. Nadawca wyslal do firmy {lead.get('business_name', '')} ({lead.get('business_type', '')}, {lead.get('city', '')}) ponizszy cold email i nie dostal odpowiedzi.

=== PIERWSZY MAIL (wyslany wczesniej) ===
{prev_email}

=== ZADANIE ===
{goal}
{STYLE_RULES}
=== FORMAT ===
- Follow-up idzie jako ODPOWIEDZ w tym samym watku, wiec NIE piszesz tematu. Odpowiedz wylacznie trescia maila, od "Dzien dobry," do podpisu, bez zadnych komentarzy przed ani po.
- Maksymalnie 50 slow. Najwyzej dwa krotkie akapity.
- Nie powtarzaj argumentow z pierwszego maila tym samym jezykiem i nie streszczaj go.
- Zakoncz jednym uprzejmym pytaniem w pelnym zdaniu o zgode na podeslanie tego, co zaproponowano w pierwszym mailu (raport albo podglad), tym samym slowem co tam.
- Podpisz sie DOKLADNIE tak samo jak w pierwszym mailu, te same linie podpisu.
"""
    message = client.messages.create(
        model=FOLLOWUP_MODEL,
        max_tokens=4000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    _record(message, "follow-up")
    return _text(message)
