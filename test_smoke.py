# Minimalny smoke test logiki bez sieci: python test_smoke.py
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import db
import scraper
import analyzer
import agent_audit
import mockup


def test_db_dedup():
    db.init_db()
    first = db.add_lead(business_name="Pizzeria Roma", city="Kraków")
    dup = db.add_lead(business_name="Pizzeria Roma", city="Kraków")
    other = db.add_lead(business_name="Pizzeria Roma", city="Gdańsk")
    assert first is not None
    assert dup is None, "duplikat ma zwracać None, nie id istniejącego rekordu"
    assert other is not None
    assert db.lead_exists("Pizzeria Roma", "Kraków")
    assert not db.lead_exists("Nieistniejąca", "Kraków")


def test_domain_handling():
    # removeprefix, nie lstrip: domena na "w" nie może stracić pierwszych liter
    assert scraper._domain_of("https://www.warsztat.pl/kontakt") == "warsztat.pl"
    assert scraper.detect_outsourced_platform("https://www.booksy.com/pl/salon")["name"] == "Booksy"
    assert scraper.detect_outsourced_platform("https://warsztat.pl") is None


def test_email_picking():
    data = {
        "mailto_emails": ["biuro@warsztat.pl"],
        "full_text": "kontakt: noreply@wordpress.com albo szef@gmail.com",
        "text_preview": "",
    }
    assert scraper.extract_email_from_website(data, "warsztat.pl") == "biuro@warsztat.pl"
    # deobfuskacja "malpa"
    data2 = {"mailto_emails": [], "full_text": "napisz: biuro (małpa) firma.pl", "text_preview": ""}
    assert scraper.extract_email_from_website(data2, "firma.pl") == "biuro@firma.pl"


def test_scores_parsing():
    parsed = analyzer._parse_analysis("SCORES: design=7 mobile=3 speed=null\n\nAnaliza...")
    assert parsed["scores"] == {"design": 7, "mobile": 3, "speed": None}
    assert parsed["analysis"] == "Analiza..."
    plain = analyzer._parse_analysis("Zwykły tekst bez ocen")
    assert plain["scores"] == {} and plain["analysis"] == "Zwykły tekst bez ocen"


def test_slice_page():
    from PIL import Image
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGB", (1280, 2500), "white").save(buf, format="PNG")
    strips = analyzer._slice_page(buf.getvalue())
    # 1280 -> 1100 szerokosci, wysokosc 2148: pasy 1000 + 1000 + 148
    assert len(strips) == 3, len(strips)
    first = Image.open(_io.BytesIO(strips[0]))
    assert first.size == (1100, 1000), first.size
    # limit pasow: bardzo dluga strona nie generuje dziesiatek obrazow
    buf2 = _io.BytesIO()
    Image.new("RGB", (1100, 20000), "white").save(buf2, format="PNG")
    assert len(analyzer._slice_page(buf2.getvalue())) == 8


WILLA_TEXT = "Willa Orle Gniazdo Strona została zawieszona. Jesteś właścicielem? Prosimy o kontakt: 601 830 000 szczyrk.com"


def test_inactive_site_detection():
    suspended = scraper.detect_inactive_site("http://www.willa.szczyrk.com/", "Willa Orle Gniazdo", WILLA_TEXT, 15)
    assert suspended["inactive"] is True
    assert suspended["inactive_reason"] == "strona jest zawieszona przez hosting"
    assert "Strona została zawieszona" in suspended["inactive_evidence"]

    # "coming soon" na cienkiej stronie to zapowiedz, na pelnej stronie to zwykle zdanie z tresci
    thin = scraper.detect_inactive_site("https://firma.pl", "Firma", "Coming soon", 2)
    assert thin["inactive"] is True
    rich = scraper.detect_inactive_site("https://firma.pl", "Firma", "Nowe menu coming soon " + "słowo " * 400, 403)
    assert rich["inactive"] is False

    nginx = scraper.detect_inactive_site("https://firma.pl", "Welcome to nginx!", "If you see this page", 20)
    assert nginx["inactive_reason"].startswith("pod adresem jest domyślna strona serwera")

    parked = scraper.detect_inactive_site("https://sedo.com/search/details/?domain=firma.pl", "Sedo", "", 300)
    assert parked["inactive"] is True and "sedo.com" in parked["inactive_reason"]

    healthy = scraper.detect_inactive_site("https://firma.pl/", "Warsztat Kowalski", "Naprawa aut, zadzwoń", 800)
    assert healthy == {"inactive": False, "inactive_reason": "", "inactive_evidence": ""}

    dns = scraper._connection_failure_reason(Exception("HTTPConnectionPool: NameResolutionError"))
    assert "DNS" in dns
    refused = scraper._connection_failure_reason(Exception("Connection refused"))
    assert "odrzucone" in refused


def test_inactive_site_email_only_from_own_domain():
    hosting_page = {"inactive": True, "inactive_reason": "strona jest zawieszona przez hosting",
                    "mailto_emails": ["pomoc@hosting.pl"], "full_text": "kontakt pomoc@hosting.pl", "text_preview": ""}
    assert scraper.find_contact_email("https://firma.pl", hosting_page) == ""
    owner_page = {"inactive": True, "inactive_reason": "strona jest w budowie",
                  "mailto_emails": [], "full_text": "Strona w budowie, pisz: biuro@firma.pl", "text_preview": ""}
    assert scraper.find_contact_email("https://firma.pl", owner_page) == "biuro@firma.pl"


class _FakeMessages:
    def __init__(self):
        self.prompts = []

    def create(self, **kwargs):
        from types import SimpleNamespace
        self.prompts.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Temat: Test\n\nDzień dobry,\ntreść.")], usage=None)


def test_inactive_site_gets_new_site_pitch_not_audit_email():
    from types import SimpleNamespace
    fake = _FakeMessages()
    original_client = analyzer._client
    analyzer._client = lambda: SimpleNamespace(messages=fake)
    try:
        lead = {"business_name": "Willa Orle Gniazdo", "business_type": "pensjonat", "city": "Szczyrk",
                "website_url": "http://www.willa.szczyrk.com/"}
        website_data = {"inactive": True, "inactive_reason": "strona jest zawieszona przez hosting",
                        "inactive_evidence": "strona została zawieszona", "has_ssl": False, "word_count": 15}
        email = analyzer.generate_email(lead, website_data, ai_analysis="## Strona nieaktywna", profile={"name": "Szymon"})
    finally:
        analyzer._client = original_client
    prompt = fake.prompts[0]
    assert "NIE MA dzialajacej strony" in prompt
    assert "strona jest zawieszona przez hosting" in prompt
    assert "strona została zawieszona" in prompt
    assert "podglad" in prompt
    assert "WYNIKI AUDYTU STRONY" not in prompt, "mail o nieaktywnej stronie nie moze isc szablonem audytu"
    assert email.endswith(analyzer.OPT_OUT_LINE)


if __name__ == "__main__":
    test_db_dedup()
    test_domain_handling()
    test_email_picking()
    test_scores_parsing()
    test_slice_page()
    test_inactive_site_detection()
    test_inactive_site_email_only_from_own_domain()
    test_inactive_site_gets_new_site_pitch_not_audit_email()
    print("OK — wszystkie smoke testy przeszły")


class _FakeResponse:
    def __init__(self, resource_type, status, content_type, url):
        self.request = type("Request", (), {"resource_type": resource_type})()
        self.status = status
        self.headers = {"content-type": content_type}
        self.url = url


class _FakeBrowser:
    def __init__(self, responses):
        self.responses = responses

    failed_assets = agent_audit._Browser.failed_assets
    main_document_response = agent_audit._Browser.main_document_response


def test_broken_stylesheet_is_reported_as_page_failure():
    css_404 = _FakeResponse("stylesheet", 404, "text/html", "https://firma.pl/wp-content/uploads/uag-css-760.css")
    line = agent_audit._broken_assets_line(_FakeBrowser([css_404]))
    assert "STRONA JEST USZKODZONA" in line
    assert "uag-css-760.css" in line


def test_stylesheet_served_as_html_counts_as_broken():
    podszywajacy_sie = _FakeResponse("stylesheet", 200, "text/html; charset=UTF-8", "https://firma.pl/style.css")
    line = agent_audit._broken_assets_line(_FakeBrowser([podszywajacy_sie]))
    assert "STRONA JEST USZKODZONA" in line


def test_broken_script_alone_is_not_a_page_failure():
    script_404 = _FakeResponse("script", 404, "text/html", "https://firma.pl/kalendarz.js")
    line = agent_audit._broken_assets_line(_FakeBrowser([script_404]))
    assert "STRONA JEST USZKODZONA" not in line
    assert "kalendarz.js" in line


def test_healthy_page_reports_no_broken_assets():
    css_ok = _FakeResponse("stylesheet", 200, "text/css", "https://firma.pl/style.css")
    obrazek_404 = _FakeResponse("image", 404, "text/html", "https://firma.pl/brak.jpg")
    line = agent_audit._broken_assets_line(_FakeBrowser([css_ok, obrazek_404]))
    assert line == "Arkusze stylów i skrypty: wszystkie wczytały się poprawnie."


def test_throttled_response_is_blamed_on_us_not_the_owner():
    odrzucenie = _FakeResponse("document", 429, "text/html", "https://firma.pl/")
    line = agent_audit._document_failure_line(_FakeBrowser([odrzucenie]))
    assert "SERWER NAS ODRZUCIŁ" in line
    assert "nie usterka, którą widzi gość" in line


def test_server_error_on_document_is_reported_as_error_page():
    awaria = _FakeResponse("document", 500, "text/html", "https://firma.pl/oferta/")
    line = agent_audit._document_failure_line(_FakeBrowser([awaria]))
    assert "STRONA ZWRÓCIŁA BŁĄD 500" in line
    assert "SERWER NAS ODRZUCIŁ" not in line


def test_healthy_document_says_nothing():
    dobra = _FakeResponse("document", 200, "text/html", "https://firma.pl/")
    css_404 = _FakeResponse("stylesheet", 404, "text/html", "https://firma.pl/style.css")
    assert agent_audit._document_failure_line(_FakeBrowser([dobra, css_404])) == ""


def test_site_level_facts_are_fetched_once_per_host():
    agent_audit._site_level_cache.clear()
    wywolania = []
    oryginal = agent_audit._measure_site_level
    agent_audit._measure_site_level = lambda url: wywolania.append(url) or ["robots.txt: jest"]
    try:
        agent_audit._site_level_facts("https://firma.pl/")
        agent_audit._site_level_facts("https://firma.pl/kontakt/")
        agent_audit._site_level_facts("https://inna.pl/")
    finally:
        agent_audit._measure_site_level = oryginal
        agent_audit._site_level_cache.clear()
    assert len(wywolania) == 2, f"na hosta raz, nie na podstronę: {wywolania}"


def test_missing_verdict_holds_the_email_back():
    assert agent_audit._verdict_from({"werdykt": "napisz"}) == ("napisz", "")
    assert agent_audit._verdict_from({"werdykt": "pomin"})[0] == "pomin"
    assert agent_audit._verdict_from({"werdykt": "pomiń"})[0] == "pomin"

    werdykt, powod = agent_audit._verdict_from({})
    assert werdykt == "pomin", "nieodczytany werdykt nie może domyślnie wysyłać maila"
    assert powod, "pominięcie z powodu braku werdyktu ma trafić do notatki leada"


def test_email_prompt_drops_the_scanner_checklist_when_audit_exists():
    from types import SimpleNamespace

    zlapane = {}

    class _Fake:
        def create(self, **kwargs):
            zlapane["prompt"] = kwargs["messages"][0]["content"]
            return SimpleNamespace(model="claude-opus-5", content=[SimpleNamespace(type="text", text="Temat: x\n\ntresc")],
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    oryginal = analyzer._client
    analyzer._client = lambda: SimpleNamespace(messages=_Fake())
    try:
        analyzer.generate_email(
            {"business_name": "Willa Luiza", "business_type": "pensjonat", "city": "Wisła",
             "website_url": "https://luizawisla.pl"},
            website_data={"has_ssl": True, "has_contact_form": False, "has_cta": True, "word_count": 900},
            ai_analysis="Werdykt: strona jest w porządku.",
            profile={"name": "Szymon"})
    finally:
        analyzer._client = oryginal

    prompt = zlapane["prompt"]
    assert "brak formularza kontaktowego" not in prompt, "checklista skanera nie ma dopisywać wad obok audytu"
    assert "Werdykt: strona jest w porządku." in prompt


def test_photo_urls_skip_logos_and_size_duplicates():
    from bs4 import BeautifulSoup
    html = """
      <img src="/wp-content/uploads/logo-firmy.png">
      <img src="/wp-content/uploads/ikony/sprite.png">
      <img src="/wp-content/uploads/salon-800x600.jpg" alt="Salon">
      <img src="/wp-content/uploads/salon.jpg">
      <img src="/wp-content/uploads/taras-scaled.jpeg">
      <img src="data:image/gif;base64,R0lGOD">
    """
    zdjecia = scraper._photo_urls(BeautifulSoup(html, "html.parser"), "https://firma.pl/")
    adresy = [z["url"] for z in zdjecia]
    assert adresy == ["https://firma.pl/wp-content/uploads/salon.jpg",
                      "https://firma.pl/wp-content/uploads/taras.jpeg"], adresy
    assert zdjecia[0]["alt"] == "Salon", "alt z pierwszego wystąpienia ma zostać"


def test_logo_url_prefers_full_size():
    from bs4 import BeautifulSoup
    html = '<img src="/uploads/Logo-Firma-180x108.png" alt="logo">'
    znaleziony = scraper._logo_url(BeautifulSoup(html, "html.parser"), "https://firma.pl/")
    assert znaleziony == "https://firma.pl/uploads/Logo-Firma.png", znaleziony


def test_mockup_prompt_carries_real_material_only():
    lead = {"business_name": "Willa Luiza", "business_type": "pensjonat", "city": "Wisła",
            "phone": "500 414 866", "website_url": "https://luizawisla.pl/"}
    dane = {"title": "Willa Luiza, Apartamenty w Wiśle", "h1_text": "Willa Luiza",
            "text_preview": "Oaza   spokoju\n\n w sercu Beskidów.",
            "image_urls": [{"url": "https://luizawisla.pl/a.jpg", "alt": "pokój"}],
            "logo_url": "https://luizawisla.pl/logo.png"}
    prompt = mockup.build_prompt(lead, dane, "Werdykt: przeciętna, telefonu nie da się kliknąć.")

    assert "500 414 866" in prompt and "https://luizawisla.pl/a.jpg" in prompt
    assert "https://luizawisla.pl/logo.png" in prompt
    assert "telefonu nie da się kliknąć" in prompt
    assert "Oaza spokoju w sercu Beskidów." in prompt, "tekst ma iść ściśnięty, bez pustych linii"
    assert "NIE WYMYŚLAJ ŻADNYCH FAKTÓW" in prompt


def test_mockup_prompt_without_site_forbids_stock_photos():
    lead = {"business_name": "Hydraulik Kowalski", "business_type": "hydraulik",
            "city": "Bielsko-Biała", "phone": "600 100 200", "website_url": ""}
    prompt = mockup.build_prompt(lead, {}, None)
    assert "NIE MA własnej strony" in prompt
    assert "Places" in prompt
    assert "Nie podstawiaj zdjęć stockowych" in prompt
    assert "cały tekst piszesz od zera" in prompt, "bez strony nie ma skąd wziąć tekstu"
    assert "całodobowość" in prompt, "ma być wyliczone, czego nie wolno twierdzić o firmie"


def test_mockup_prompt_with_site_has_no_from_scratch_clause():
    lead = {"business_name": "Willa Luiza", "business_type": "pensjonat",
            "city": "Wisła", "website_url": "https://luizawisla.pl/"}
    prompt = mockup.build_prompt(lead, {"text_preview": "Oaza spokoju."}, None)
    assert "cały tekst piszesz od zera" not in prompt, "mając ich teksty nie piszemy od zera"
