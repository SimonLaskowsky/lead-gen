# Minimalny smoke test logiki bez sieci: python test_smoke.py
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import db
import scraper
import analyzer


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
