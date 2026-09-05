import base64
import io
import json
import os
import re
import html as html_lib

import anthropic
import requests
from bs4 import BeautifulSoup

import scraper

STEP_MODEL = os.getenv("AUDIT_STEP_MODEL", "claude-sonnet-5")
MEMO_MODEL = os.getenv("AUDIT_MEMO_MODEL", "claude-opus-5")
MAX_TOOL_ROUNDS = int(os.getenv("AUDIT_MAX_STEPS", "6"))
MAX_PAGES = 4
MAX_SCREENSHOTS = 4

on_usage = None


def _client():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _record(message, purpose):
    if on_usage:
        on_usage(purpose, message.model, message.usage.input_tokens, message.usage.output_tokens)


def _absolute(base_url, href):
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    root = re.match(r"https?://[^/]+", base_url)
    root = root.group(0) if root else base_url
    if href.startswith("/"):
        return root + href
    folder = base_url.rsplit("/", 1)[0] if "/" in base_url[8:] else base_url.rstrip("/")
    return folder.rstrip("/") + "/" + href


def _page_facts(url):
    """Fakty ze strony w postaci krotkiego tekstu dla modelu: to, co czlowiek sprawdzilby w kodzie."""
    data = scraper.scrape_website(url) or {}
    if data.get("outsourced_platform"):
        return f"To nie jest wlasna strona, tylko profil na platformie {data['outsourced_platform']}.", data, []
    if data.get("error"):
        return f"Nie udalo sie otworzyc strony: {data['error']}", data, []
    try:
        response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(response.text, "html.parser")
        page_html = response.text
    except Exception as error:
        return f"Strona odpowiada, ale nie udalo sie pobrac tresci: {error}", data, []

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = html_lib.unescape(re.sub(r"\s+", " ", soup.get_text(" ")))
    headings = [(h.name.upper(), h.get_text(" ", strip=True)[:70]) for h in soup.find_all(["h1", "h2"])][:14]
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)[:40]
        href = a["href"].strip()
        if not label or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = _absolute(url, href)
        if full in seen or not full.startswith(("http://", "https://")):
            continue
        seen.add(full)
        links.append((label, full))
        if len(links) >= 25:
            break
    prices = re.findall(r"\d[\d\s.,]*\s?(?:zł|PLN)", text)[:8]
    booking_words = [w for w in ("rezerwuj", "rezerwacja online", "sprawdź dostępność", "kalendarz", "booking.com", "hotres", "profitroom", "bookero", "zamów online", "umów wizytę", "booksy") if w in page_html.lower()]

    lines = [
        f"URL: {url}",
        f"Tytul: {data.get('title') or 'brak'}",
        f"Meta description: {'jest' if data.get('meta_description') else 'BRAK'}",
        "Naglowki: " + ("; ".join(f"{t}: {x}" for t, x in headings) if headings else "BRAK H1 i H2"),
        f"Slow tekstu: {data.get('word_count', len(text.split()))}",
        f"Telefon w tresci: {'tak' if data.get('has_phone') else 'nie'}; klikalny link tel: {'tak' if data.get('has_tel_link') else 'NIE'}",
        f"Adres e-mail (mailto): {', '.join(data.get('mailto_emails') or []) or 'brak'}",
        f"Formularz: {'jest' if data.get('has_contact_form') else 'brak'}; przycisk CTA: {'jest' if data.get('has_cta') else 'brak'}",
        f"Ceny na stronie: {', '.join(prices) if prices else 'brak'}",
        f"Slowa o rezerwacji lub zamawianiu online: {', '.join(booking_words) if booking_words else 'brak'}",
        f"SSL: {'tak' if data.get('has_ssl') else 'NIE'}; viewport mobilny: {'tak' if data.get('has_mobile_viewport') else 'NIE'}; PageSpeed mobile: {data.get('pagespeed_score') if data.get('pagespeed_score') is not None else 'brak danych'}",
        f"Zdjecia: {data.get('image_count', 0)}, bez alt: {data.get('images_missing_alt', 0)}; Analytics: {'martwy UA' if data.get('has_dead_analytics') else ('jest' if data.get('has_legacy_ua') else 'nie wykryto')}",
        f"Technologia: {', '.join(data.get('tech_stack') or []) or 'nie wykryto'}",
        "Poczatek tresci: " + text[:700],
        "Linki (etykieta -> adres): " + ("; ".join(f"{l} -> {u}" for l, u in links) if links else "brak"),
    ]
    return "\n".join(lines), data, links


def _jpeg(png_bytes, max_width, max_height=None, quality=75):
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes))
    ratio = min(max_width / img.width, 1.0)
    if ratio < 1.0:
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    if max_height and img.height > max_height:
        img = img.crop((0, 0, img.width, max_height))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _screenshot(url, width, whole_page):
    from playwright.sync_api import sync_playwright
    height = 844 if width <= 500 else (768 if width <= 1024 else 800)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            page.goto(url, timeout=30000, wait_until="networkidle")
        except Exception:
            page.goto(url, timeout=30000, wait_until="load")
            page.wait_for_timeout(2000)
        scraper._dismiss_cookie_banner(page)
        if whole_page:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
        png = page.screenshot(type="png", full_page=whole_page)
        browser.close()
    return png


def _image_block(jpg):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpg).decode("utf-8")}}


TOOLS = [
    {
        "name": "otworz_strone",
        "description": "Pobiera fakty ze strony pod podanym adresem: tytul, naglowki, telefon, formularz, ceny, slowa o rezerwacji, SSL, PageSpeed, poczatek tresci i liste linkow z etykietami. Uzywaj do strony glownej i do podstron, do ktorych prowadza glowne przyciski (oferta, cennik, kontakt, rezerwacja).",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "zrzut_ekranu",
        "description": "Robi zrzut strony w podanej szerokosci: 1280 (desktop), 1024 (laptop) lub 390 (telefon). Zakres 'gora' to sam pierwszy ekran w pelnej rozdzielczosci, 'cala' to cala strona pocieta na 3 pasy. Zacznij od 'gora' 1280 strony, na ktorej jest wlasciwa tresc.",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "szerokosc": {"type": "integer", "enum": [1280, 1024, 390]}, "zakres": {"type": "string", "enum": ["gora", "cala"]}}, "required": ["url", "szerokosc", "zakres"], "additionalProperties": False},
        "strict": True,
    },
]

SYSTEM_STEPS = """Jesteś doświadczonym projektantem UI/UX i konsultantem, który ocenia stronę lokalnej firmy tak, jak zrobiłby to człowiek siedzący przed ekranem. Masz dwa narzędzia: fakty ze strony i zrzut ekranu. Sam decydujesz, co obejrzeć.

Metoda, w tej kolejności:
1. Otwórz stronę główną i zrób zrzut pierwszego ekranu w 1280. Trzy sekundy: co widać, co czuć, z którego roku to wygląda.
2. Idź tam, gdzie prowadzi główny przycisk albo najważniejszy link. Strona główna bywa tylko rozdzielnią; właściwa treść jest o klik dalej. Oceniaj tę stronę, którą zobaczy gość, nie rozdzielnię.
3. Praca gościa: wypisz sobie 3 do 5 rzeczy, po które przychodzi klient tej branży (np. cena, termin, kontakt, dojazd, oferta), i sprawdź, ile klików kosztuje każda i czy w ogóle da się ją załatwić na stronie. To zastępuje każdą listę kontrolną.
4. Fakty techniczne bierz z podstron z treścią, nie z rozdzielni.
5. Jakość wizualna: pismo, układ, kontrast, zdjęcia, hierarchia; z dowodem, gdzie to widać. Zrzut w 390 pokaże telefon, zrzut w 1024 pokaże, czy układ się łamie.

Zasady: najwyżej {max_steps} rund narzędzi, więc nie oglądaj wszystkiego, oglądaj to, co rozstrzyga. Przed każdym wywołaniem napisz jedno krótkie zdanie po polsku: co sprawdzasz i dlaczego (to jest twój dziennik). Gdy wiesz dość, napisz "GOTOWE" i w trzech zdaniach podsumuj, co ustaliłeś: która strona jest właściwa, co jest największym problemem albo że strona jest dobra. Nie pisz jeszcze pełnej notatki."""

MEMO_PROMPT = """Napisz notatkę o tej stronie dla programisty, który ma zdecydować, czy pisać do właściciela z propozycją poprawek, i co mu powiedzieć. Pisz jak konsultant po obejrzeniu strony, nie jak formularz. Wzór stylu, tak ma to brzmieć:

--- WZÓR (inna firma) ---
Werdykt: dobra, świeżo zrobiona strona, prawdopodobnie z tego roku. 8 na 10 na tle branży. Jedyna realna luka: brak rezerwacji online i kalendarza dostępności, więc każda rezerwacja to mail albo telefon, a w sezonie część gości nie czeka na odpowiedź.

Strona główna to rozdzielnia z dwoma przyciskami, Restauracja i Pensjonat, 39 słów. Właściwa treść jest o klik dalej: 5 tysięcy słów, cennik sezonowy z datami do 2027, telefon klikalny w nagłówku, formularz z polami od-do. Gość załatwia pokoje w jeden klik, cenę na tej samej stronie, termin przez formularz. Rezerwacji online nie ma: "Zapytaj o termin" prowadzi do formularza, nie do kalendarza.

Wizualnie: spójna paleta ciemnej zieleni i złota, jeden krój, dobre zdjęcia, oddech. Jedyna uwaga: nagłówek na zdjęciu ma miejscami słaby kontrast na jasnych fragmentach tła.

Najcenniejsza zmiana: silnik rezerwacji z kalendarzem. Analityki też nie ma, ale to drobiazg.
--- KONIEC WZORU ---

Zasady:
- Zacznij od werdyktu w jednym akapicie: ocena 1-10 na tle dobrych stron tej branży w 2026 roku, rok, z którego strona wygląda, i jedna dominująca historia, czyli to, co naprawdę kosztuje firmę klientów albo powód, dla którego nie ma czego poprawiać.
- Potem 2 do 4 krótkich akapitów bez nagłówków i bez wypunktowań: pierwsze wrażenie, praca gościa (co da się załatwić i za ile klików), jakość wizualna, fakty techniczne, tylko te, które mają znaczenie. Każde spostrzeżenie z dowodem, gdzie to widać na ekranie, tak żeby właściciel odnalazł to w dziesięć sekund.
- Jeśli coś jest dobre, napisz to wprost. Jeśli strona jest dobra, powiedz, że nie ma sensu pisać z propozycją poprawek, albo że jedyny sensowny temat to X.
- Nie oceniaj po rozdzielni, jeśli właściwa treść jest na podstronie. Nie zgaduj klikalności ze zrzutu, klikalność jest w faktach.
- Bez emoji, bez słów "brzydka", "amatorska", "katastrofa". Najwyżej 350 słów.
- Ostatnia linia notatki, dokładnie w tym formacie i w jednej linii:
OCENA: {"pierwsze_wrazenie": 1-10, "rok_wygladu": "RRRR", "werdykt": "napisz" albo "pomin", "historia": "jedno zdanie", "wlasciwa_strona": "url strony z trescia", "design": 1-10, "mobile": 1-10, "seo": 1-10, "cta": 1-10}"""


def _tool_result_content(name, args, state):
    if name == "otworz_strone":
        url = args.get("url", "")
        if state["pages"] >= MAX_PAGES:
            return [{"type": "text", "text": "Limit otwieranych stron wyczerpany. Oceniaj na podstawie tego, co masz."}]
        state["pages"] += 1
        facts, data, links = _page_facts(url)
        state["facts"][url] = facts
        if not state.get("primary_data"):
            state["primary_data"] = data
        return [{"type": "text", "text": facts}]
    if name == "zrzut_ekranu":
        url = args.get("url", "")
        width = int(args.get("szerokosc", 1280))
        whole = args.get("zakres") == "cala"
        if state["shots"] >= MAX_SCREENSHOTS:
            return [{"type": "text", "text": "Limit zrzutów wyczerpany."}]
        state["shots"] += 1
        try:
            png = _screenshot(url, width, whole)
        except Exception as error:
            return [{"type": "text", "text": f"Zrzut nie wyszedł: {str(error)[:120]}"}]
        content = []
        if whole:
            strips = [_jpeg(png, 900, 1000 * 3)]
            content.append({"type": "text", "text": f"{url}, {width}px, cała strona (do 3000 px wysokości):"})
            content.append(_image_block(strips[0]))
        else:
            jpg = _jpeg(png, width, 650 if width > 500 else 844)
            content.append({"type": "text", "text": f"{url}, {width}px, pierwszy ekran:"})
            content.append(_image_block(jpg))
        state["images"].append((url, width, content[-1]))
        return content
    return [{"type": "text", "text": "Nieznane narzędzie."}]


def _text_of(message):
    return "\n".join(b.text for b in message.content if b.type == "text").strip()


def _thinking_of(message):
    return "\n".join(b.thinking for b in message.content if b.type == "thinking" and getattr(b, "thinking", "")).strip()


def _serialize_assistant(message):
    blocks = []
    for b in message.content:
        if b.type == "thinking":
            blocks.append({"type": "thinking", "thinking": b.thinking, "signature": b.signature})
        elif b.type == "redacted_thinking":
            blocks.append({"type": "redacted_thinking", "data": b.data})
        elif b.type == "text":
            blocks.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return blocks


def explore(lead):
    """Etap 1: Sonnet oglada strone narzedziami i prowadzi dziennik. Zwraca stan z faktami, obrazami i dziennikiem."""
    client = _client()
    state = {"pages": 0, "shots": 0, "facts": {}, "images": [], "log": [], "summary": ""}
    messages = [{"role": "user", "content": [{"type": "text", "text":
        f"Firma: {lead.get('business_name', '')}\nTyp biznesu: {lead.get('business_type', '')}\nMiasto: {lead.get('city', '')}\nStrona: {lead.get('website_url', '')}\n\nZacznij od otwarcia strony głównej i zrzutu jej pierwszego ekranu w 1280."}]}]
    system = SYSTEM_STEPS.format(max_steps=MAX_TOOL_ROUNDS)
    for _ in range(MAX_TOOL_ROUNDS + 1):
        message = client.messages.create(
            model=STEP_MODEL,
            max_tokens=4000,
            system=system,
            tools=TOOLS,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "low"},
            messages=messages,
        )
        _record(message, "analysis")
        thought = _thinking_of(message)
        said = _text_of(message)
        if thought:
            state["log"].append("myśl: " + thought[:400])
        if said:
            state["log"].append(said[:400])
        messages.append({"role": "assistant", "content": _serialize_assistant(message)})
        tool_uses = [b for b in message.content if b.type == "tool_use"]
        if message.stop_reason != "tool_use" or not tool_uses:
            state["summary"] = said
            break
        results = []
        for call in tool_uses:
            args = call.input if isinstance(call.input, dict) else json.loads(call.input)
            state["log"].append(f"narzędzie: {call.name} {json.dumps(args, ensure_ascii=False)}")
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": _tool_result_content(call.name, args, state)})
        for earlier in messages:
            if earlier["role"] == "user" and isinstance(earlier["content"], list):
                for block in earlier["content"]:
                    block.pop("cache_control", None)
        results[-1]["cache_control"] = {"type": "ephemeral"}
        messages.append({"role": "user", "content": results})
    else:
        state["summary"] = "(budżet kroków wyczerpany)"
    if not state["summary"]:
        state["summary"] = "(model zakończył bez podsumowania)"
    return state


def _parse_memo(text):
    scores = {}
    memo = text.strip()
    match = re.search(r"OCENA:\s*(\{.*\})\s*$", memo, re.S)
    if match:
        try:
            scores = json.loads(match.group(1))
        except Exception:
            scores = {}
        memo = memo[:match.start()].rstrip()
    return memo, scores


def write_memo(lead, state):
    """Etap 2: Opus pisze notatke na podstawie faktow, dziennika i 2-3 obrazow. Bez calej historii, zeby nie placic za nia drugi raz."""
    client = _client()
    content = [{"type": "text", "text": f"Firma: {lead.get('business_name', '')}, typ: {lead.get('business_type', '')}, miasto: {lead.get('city', '')}, strona: {lead.get('website_url', '')}\n\n=== FAKTY ZE STRON (z narzędzi) ===\n" + "\n\n".join(state["facts"].values())}]
    content.append({"type": "text", "text": "=== DZIENNIK OGLĄDANIA (co sprawdzał model eksplorujący i co ustalił) ===\n" + "\n".join(state["log"]) + "\n\nPodsumowanie eksploracji: " + state["summary"]})
    chosen = state["images"][:1] + state["images"][-2:] if len(state["images"]) > 3 else state["images"]
    for url, width, block in chosen:
        content.append({"type": "text", "text": f"Zrzut: {url}, {width}px"})
        content.append(block)
    content.append({"type": "text", "text": MEMO_PROMPT})
    message = client.messages.create(
        model=MEMO_MODEL,
        max_tokens=6000,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": content}],
    )
    _record(message, "analysis")
    memo, scores = _parse_memo(_text_of(message))
    thought = _thinking_of(message)
    return memo, scores, thought


def audit(lead):
    state = explore(lead)
    memo, scores, memo_thought = write_memo(lead, state)
    log = list(state["log"])
    if memo_thought:
        log.append("myśl przed notatką: " + memo_thought[:500])
    verdict = "pomin" if str(scores.get("werdykt", "")).lower().startswith("pomi") else "napisz"
    analysis_text = memo + "\n\nJak model do tego doszedł:\n" + "\n".join("- " + line for line in log)
    numeric = {k: scores.get(k) for k in ("design", "mobile", "seo", "cta") if scores.get(k) is not None}
    numeric["first_impression"] = scores.get("pierwsze_wrazenie")
    numeric["design_year"] = scores.get("rok_wygladu")
    return {
        "analysis": analysis_text,
        "scores": numeric,
        "verdict": verdict,
        "story": scores.get("historia", ""),
        "primary_url": scores.get("wlasciwa_strona") or lead.get("website_url", ""),
        "website_data": state.get("primary_data") or {},
        "log": log,
    }
