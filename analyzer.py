import os
import base64
import io
import anthropic


def _client():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _compress(png_bytes: bytes, max_width: int = 1100, quality: int = 72) -> bytes:
    """Resize + convert PNG to JPEG to reduce payload size."""
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes))
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


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

    prompt = f"""Jesteś senior konsultantem ds. web designu i marketingu cyfrowego. Przeprowadzasz pełny audyt strony polskiego lokalnego biznesu.

=== DANE FIRMY ===
Firma: {lead.get('business_name', '')}
Typ biznesu: {lead.get('business_type', '')}
URL: {lead.get('website_url', '')}

=== WYNIKI AUTOMATYCZNYCH SPRAWDZEŃ ===
{tech_block}
{page_text}

=== TWOJE ZADANIE ===
Masz przed sobą zrzuty ekranu tej strony (desktop i mobile). Przeprowadź szczegółowy audyt.

Oceń każdy punkt konkretnie — nie ogólnikowo:

**1. Pierwsze wrażenie (3-5 sekund)**
Czy strona od razu komunikuje czym się firma zajmuje? Czy wygląda profesjonalnie? Czy zachęca do zostania?

**2. Design i estetyka**
Kolory, typografia, jakość zdjęć/grafik, spójność wizualna. Czy wygląda nowocześnie czy jak relikt lat 2010?

**3. Mobile (patrz na zrzut mobilny)**
Czy strona działa na telefonie? Co się psuje — tekst, przyciski, układ?

**4. Treść i komunikacja**
Czy jasno widać: co oferują, dla kogo, ile kosztuje, jak się skontaktować? Czy są opinie klientów?

**5. Najważniejsze rzeczy do poprawy**
Podaj 3-4 konkretne zmiany które miałyby największy wpływ na konwersję.

Pisz po polsku. Bądź szczery i konkretny — jak gdybyś płacił za ten audyt. Używaj punktorów i nagłówków z powyższej struktury."""

    # Build message content with both screenshots
    content = []

    desktop_bytes = screenshots.get("desktop")
    mobile_bytes = screenshots.get("mobile")

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

    if not has_website:
        prompt = f"""Jesteś pomocnikiem webdevelopera piszącego cold email po polsku do lokalnego biznesu, który NIE ma strony internetowej.

Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}

Napisz krótki, przyjazny, profesjonalny cold email po polsku proponując zbudowanie strony internetowej.
Zasady:
- Maksymalnie 150 słów
- Wspomnij konkretne korzyści dla tego typu biznesu
- Wspomnij cenę 500 PLN za stronę 4-podstronową
- Nie bądź nachalny, bądź pomocny
- Zacznij od "Temat: ..." (pierwsza linia to temat)
- Podpisz się: Szymon
- Pisz naturalnie, jak człowiek — nie jak robot"""

    else:
        # Prefer AI visual analysis over hardcoded checks if available
        if ai_analysis:
            analysis_context = f"Analiza AI strony:\n{ai_analysis}"
        else:
            issues = []
            if website_data and not website_data.get("error"):
                if not website_data.get("has_mobile_viewport"):
                    issues.append("strona nie jest responsywna (źle wygląda na telefonie)")
                if not website_data.get("has_ssl"):
                    issues.append("brak certyfikatu SSL (strona nie jest bezpieczna — przeglądarka wyświetla ostrzeżenie)")
                if not website_data.get("meta_description"):
                    issues.append("brak opisu meta — gorsza widoczność w Google")
                if not website_data.get("has_cta"):
                    issues.append("brak wyraźnego przycisku/wezwania do działania (CTA)")
                if not website_data.get("has_contact_form"):
                    issues.append("brak formularza kontaktowego")
                if website_data.get("uses_tables_layout"):
                    issues.append("przestarzały układ oparty na tabelach — strona wygląda jak z lat 2000")
                if not website_data.get("has_social"):
                    issues.append("brak linków do mediów społecznościowych")
                score = website_data.get("pagespeed_score")
                if score is not None and score < 60:
                    fcp = website_data.get("pagespeed_fcp", "")
                    issues.append(f"bardzo wolne ładowanie na telefonie (PageSpeed score: {score}/100{', czas: ' + fcp if fcp else ''})")
                elif score is not None and score < 80:
                    issues.append(f"wolne ładowanie na telefonie (PageSpeed score: {score}/100)")
                if website_data.get("text_preview"):
                    issues.append(f"Treść strony: {website_data['text_preview'][:300]}")
            analysis_context = "Zauważone problemy:\n" + "\n".join(f"- {i}" for i in issues[:4]) if issues else "Ogólna modernizacja i poprawa UX"

        prompt = f"""Jesteś pomocnikiem webdevelopera piszącego cold email po polsku do lokalnego biznesu oferując ulepszenie ich strony.

Firma: {business_name}
Typ biznesu: {business_type}
Miasto: {city}
URL: {lead.get('website_url', '')}

{analysis_context}

Na podstawie tej analizy napisz krótki, przyjazny, profesjonalny cold email po polsku.
Zasady:
- Maksymalnie 180 słów
- Wspomnij 2-3 konkretne problemy które zauważyłeś (na podstawie analizy powyżej)
- Wspomnij cenę 500 PLN za odświeżenie strony (4 podstrony)
- Nie bądź nachalny, bądź pomocny i konkretny
- Zacznij od "Temat: ..." (pierwsza linia to temat)
- Podpisz się: Szymon
- Pisz naturalnie, jak człowiek — nie jak robot"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text
