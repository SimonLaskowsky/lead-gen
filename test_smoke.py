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


if __name__ == "__main__":
    test_db_dedup()
    test_domain_handling()
    test_email_picking()
    test_scores_parsing()
    print("OK — wszystkie smoke testy przeszły")
