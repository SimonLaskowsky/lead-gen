# Przykładowe dane do klikania po aplikacji: python seed_demo.py
import json
from datetime import datetime, timedelta

import db


def days_ago(days, hour=10):
    stamp = datetime.now() - timedelta(days=days)
    return stamp.replace(hour=hour, minute=12, second=0).isoformat(timespec="seconds")


def analysis(scores, text):
    return json.dumps({"scores": scores, "analysis": text}, ensure_ascii=False)


def checks(**fields):
    base = {
        "has_ssl": True, "has_mobile_viewport": True, "meta_description": "opis strony", "has_contact_form": True,
        "has_cta": True, "has_social": True, "uses_tables_layout": False, "has_dead_analytics": False,
        "pagespeed_score": 72, "tech_stack": [], "has_h1": True, "has_phone": True, "has_tel_link": True,
        "image_count": 12, "images_missing_alt": 2, "word_count": 640,
    }
    base.update(fields)
    return json.dumps(base, ensure_ascii=False)


LEADS = [
    {
        "business_name": "Salon Fryzjerski Klaudia", "city": "Kraków", "business_type": "fryzjer",
        "email": "kontakt@salon-klaudia.pl", "phone": "+48 512 338 904", "website_url": "https://salon-klaudia.pl",
        "address": "ul. Karmelicka 41, Kraków", "status": "ready", "autopilot": 1,
        "website_checks": checks(tech_stack=["WordPress", "Elementor"], has_mobile_viewport=False, pagespeed_score=38, has_contact_form=False, images_missing_alt=9),
        "ai_analysis": analysis({"design": 4, "mobile": 2, "seo": 5, "cta": 6, "speed": 3},
            "**1. Pierwszy ekran**\nZdjęcie stockowe na całą szerokość, nazwa salonu dopiero po przewinięciu. Klientka z telefonu nie wie, gdzie jest salon i jak umówić wizytę.\n\n**2. Spójność wizualna**\nTrzy różne kroje pisma, cennik wklejony jako zdjęcie, opinie jako zrzuty ekranu z Facebooka.\n\n**3. Technika**\n- brak meta viewport, strona na telefonie wymaga powiększania\n- PageSpeed 38/100, zdjęcia po 3 MB\n\n**4. Trzy zmiany o najwyższym ROI**\n- przycisk „Umów wizytę” na pierwszym ekranie\n- cennik jako tekst\n- kompresja zdjęć"),
        "generated_email": "Temat: Rzut oka na salon-klaudia.pl z telefonu\n\nDzień dobry,\nwyszukałem Salon Klaudia na telefonie, udając klientkę z Krowodrzy, która chce się umówić na dziś. Strona otwiera się w wersji na komputer, cennik trzeba powiększać palcami, a przycisku do umówienia nie ma nigdzie na pierwszym ekranie.\n\nPrzy przeglądzie wyszło jeszcze kilka drobniejszych rzeczy, jak opinie wklejone jako zrzuty ekranu. Pełną listę dołączę do bezpłatnego podglądu.\n\nJestem programistą z Krakowa, robię strony dla lokalnych firm. Czy mogę podesłać bezpłatny podgląd ekranu głównego po poprawkach?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571",
        "notes": "",
    },
    {
        "business_name": "Auto-Serwis Marek Nowicki", "city": "Kraków", "business_type": "mechanik",
        "email": "", "phone": "+48 604 771 235", "website_url": "", "address": "ul. Wielicka 188, Kraków",
        "status": "new", "autopilot": 1, "notes": "brak maila, tylko telefon, spróbować SMS",
    },
    {
        "business_name": "Pizzeria Pod Kasztanem", "city": "Gdańsk", "business_type": "restauracja",
        "email": "zamowienia@podkasztanem.pl", "phone": "+48 58 341 20 87", "website_url": "https://podkasztanem.pl",
        "address": "ul. Długa 12, Gdańsk", "status": "emailed", "autopilot": 1, "emailed_at": days_ago(3),
        "website_checks": checks(tech_stack=["Wix"], has_ssl=True, pagespeed_score=55),
        "ai_analysis": analysis({"design": 6, "mobile": 6, "seo": 4, "cta": 5, "speed": 5},
            "**1. Pierwszy ekran**\nMenu w PDF, brak przycisku zamówienia online. Numer telefonu widoczny, ale nieklikalny na telefonie.\n\n**2. Trust flags**\nBrak opinii, brak zdjęć lokalu, godziny otwarcia tylko w stopce."),
        "generated_email": "Temat: Menu Pod Kasztanem oczami klienta z telefonu\n\nDzień dobry,\nszukałem pizzerii na Głównym Mieście z telefonu i trafiłem na Państwa stronę. Menu otwiera się jako PDF, a numeru nie da się kliknąć, więc klient przepisuje go ręcznie albo idzie dalej.\n\nCzy mogę podesłać bezpłatny podgląd strony z menu i przyciskiem „Zadzwoń”?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571",
    },
    {
        "business_name": "Gabinet Kosmetyczny Nova Skin", "city": "Warszawa", "business_type": "kosmetyczka",
        "email": "", "phone": "+48 733 901 447", "website_url": "https://booksy.com/pl-pl/nova-skin-warszawa",
        "address": "ul. Puławska 102, Warszawa", "status": "new", "autopilot": 0,
        "website_checks": json.dumps({"outsourced_platform": "Booksy", "outsourced_pitch": "płacą prowizję od każdej rezerwacji", "tech_stack": []}),
        "ai_analysis": "## Strona na platformie Booksy\n\nTen biznes korzysta z **Booksy** zamiast własnej strony: płacą prowizję od każdej rezerwacji.\n\n### Szansa sprzedażowa\n- **Zero prowizji**: własna strona nie pobiera procentu od rezerwacji\n- **Własna marka i domena**\n- **Lepsza widoczność w Google**\n\n### Rekomendacja\nZaproponuj stronę wizytówkę z wbudowanym widgetem Booksy.",
        "notes": "znaleźć maila na Instagramie",
    },
    {
        "business_name": "Dach-Pol Usługi Dekarskie", "city": "Bielsko-Biała", "business_type": "dekarz",
        "email": "biuro@dach-pol.com.pl", "phone": "+48 602 118 330", "website_url": "https://dach-pol.com.pl",
        "address": "ul. Cieszyńska 320, Bielsko-Biała", "status": "converted", "autopilot": 0, "emailed_at": days_ago(21),
        "website_checks": checks(tech_stack=["Joomla"], uses_tables_layout=True, has_dead_analytics=True, pagespeed_score=29),
        "ai_analysis": analysis({"design": 2, "mobile": 2, "seo": 3, "cta": 3, "speed": 2}, "**Werdykt**\nStrona z 2009 roku, układ tabelkowy, martwe Google Analytics. Firma ma świetne opinie w Google, strona to psuje."),
        "generated_email": "Temat: Kilka rzeczy na dach-pol.com.pl, które łatwo poprawić\n\nDzień dobry,\ntreść wysłanego maila.\n\nSzymon Laskowski",
        "notes": "podpisana umowa 4 200 PLN, start 15.09",
    },
    {
        "business_name": "Kwiaciarnia Stokrotka", "city": "Wrocław", "business_type": "kwiaciarnia",
        "email": "stokrotka.wroclaw@gmail.com", "phone": "+48 71 322 45 19", "website_url": "https://www.facebook.com/kwiaciarniastokrotkawroclaw",
        "address": "ul. Świdnicka 8, Wrocław", "status": "replied", "autopilot": 1, "emailed_at": days_ago(5),
        "website_checks": json.dumps({"outsourced_platform": "Facebook", "outsourced_pitch": "używają Facebooka zamiast własnej strony", "tech_stack": []}),
        "ai_analysis": "## Strona na platformie Facebook\n\nTen biznes korzysta z **Facebooka** zamiast własnej strony.",
        "generated_email": "Temat: Stokrotka w Google bez własnej strony\n\nDzień dobry,\nszukałem kwiaciarni przy Świdnickiej i Google pokazuje tylko Państwa profil na Facebooku, bez godzin, cennika i formularza zamówienia.\n\nCzy mogę podesłać bezpłatny projekt prostej strony z zamówieniami?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571",
        "notes": "odpisała pani Renata: chce zobaczyć projekt, wysłać do piątku",
    },
    {
        "business_name": "Warsztat Stolarski Dębowy Kąt", "city": "Poznań", "business_type": "stolarz",
        "email": "info@debowykat.pl", "phone": "+48 61 852 77 10", "website_url": "https://debowykat.pl",
        "address": "ul. Głogowska 240, Poznań", "status": "new", "autopilot": 0,
        "website_checks": checks(tech_stack=["WordPress"], has_cta=False, has_contact_form=False, word_count=180, images_missing_alt=14, image_count=16),
        "ai_analysis": analysis({"design": 5, "mobile": 7, "seo": 3, "cta": 2, "speed": 6},
            "**1. Pierwszy ekran**\nGaleria realizacji wygląda dobrze, ale nigdzie nie ma zachęty do kontaktu. Klient ogląda i wychodzi.\n\n**2. SEO**\n180 słów na całej stronie, 14 z 16 zdjęć bez opisu alt. Google nie wie, że to stolarnia w Poznaniu."),
        "notes": "analiza zrobiona, mail do napisania ręcznie",
    },
    {
        "business_name": "Studio Tatuażu Czarny Tusz", "city": "Łódź", "business_type": "studio tatuażu",
        "email": "", "phone": "+48 690 455 212", "website_url": "https://www.instagram.com/czarnytusz.lodz",
        "address": "ul. Piotrkowska 97, Łódź", "status": "skipped", "autopilot": 0,
        "website_checks": json.dumps({"outsourced_platform": "Instagram", "outsourced_pitch": "używają Instagrama zamiast własnej strony", "tech_stack": []}),
        "notes": "dzwoniłem, nie zainteresowani, wracają do tematu w 2027",
    },
    {
        "business_name": "Hydraulik Express Kowalczyk", "city": "Katowice", "business_type": "hydraulik",
        "email": "biuro@hydraulik-express.pl", "phone": "+48 501 220 918", "website_url": "https://hydraulik-express.pl",
        "address": "ul. Mikołowska 15, Katowice", "status": "failed", "autopilot": 1,
        "last_error": "Profil Szymon Laskowski nie ma skonfigurowanej skrzynki pocztowej",
        "website_checks": checks(tech_stack=["WordPress", "Divi"], has_ssl=False, pagespeed_score=44),
        "ai_analysis": analysis({"design": 5, "mobile": 5, "seo": 5, "cta": 7, "speed": 4}, "**Trust flags**\nBrak SSL, Chrome pokazuje „Niezabezpieczona” zanim klient zobaczy numer telefonu."),
        "generated_email": "Temat: Ostrzeżenie w Chrome na hydraulik-express.pl\n\nDzień dobry,\nwszedłem na Państwa stronę z telefonu i Chrome pokazuje „Niezabezpieczona” zanim wyświetli numer. Dla klienta z zalaną łazienką to jeden powód mniej, żeby zadzwonić.\n\nCzy mogę podesłać bezpłatny podgląd strony po poprawkach?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571",
    },
    {
        "business_name": "Przedszkole Językowe Little Stars", "city": "Rzeszów", "business_type": "przedszkole",
        "email": "sekretariat@littlestars-rzeszow.pl", "phone": "+48 17 862 14 40", "website_url": "https://littlestars-rzeszow.pl",
        "address": "ul. Krakowska 58, Rzeszów", "status": "ready", "autopilot": 1,
        "website_checks": checks(tech_stack=["Squarespace"], pagespeed_score=61, has_contact_form=True),
        "ai_analysis": analysis({"design": 7, "mobile": 7, "seo": 5, "cta": 4, "speed": 6}, "**Werdykt**\nStrona ładna, ale zapisy na rok szkolny ukryte w podstronie trzeciego poziomu."),
        "generated_email": "Temat: Zapisy do Little Stars w trzy kliknięcia zamiast siedmiu\n\nDzień dobry,\nszukałem, jak zapisać dziecko do Little Stars i formularz znalazłem dopiero po siedmiu kliknięciach. Rodzice z telefonu odpadają wcześniej.\n\nCzy mogę podesłać bezpłatny podgląd strony głównej z zapisami na pierwszym ekranie?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571",
    },
    {
        "business_name": "Serwis Rowerowy Dwa Koła", "city": "Gdynia", "business_type": "serwis rowerowy",
        "email": "serwis@dwakola-gdynia.pl", "phone": "+48 58 620 33 71", "website_url": "https://dwakola-gdynia.pl",
        "address": "ul. Świętojańska 74, Gdynia", "status": "new", "autopilot": 0,
        "website_checks": checks(tech_stack=["Next.js"], pagespeed_score=88),
        "notes": "",
    },
    {
        "business_name": "Restauracja Stary Młyn", "city": "Zakopane", "business_type": "restauracja",
        "email": "rezerwacje@starymlyn-zakopane.pl", "phone": "+48 18 201 55 60", "website_url": "https://starymlyn-zakopane.pl",
        "address": "ul. Krupówki 33, Zakopane", "status": "emailed", "autopilot": 1, "emailed_at": days_ago(10),
        "website_checks": checks(tech_stack=["WordPress"], pagespeed_score=51, has_cta=False),
        "ai_analysis": analysis({"design": 6, "mobile": 5, "seo": 6, "cta": 3, "speed": 5}, "**Pierwszy ekran**\nSlider z pięcioma zdjęciami, rezerwacja dopiero w stopce."),
        "generated_email": "Temat: Rezerwacja w Starym Młynie z telefonu\n\nDzień dobry,\nchciałem zarezerwować stolik na Krupówkach z telefonu i przycisk rezerwacji znalazłem dopiero w stopce, pod sliderem.\n\nCzy mogę podesłać bezpłatny podgląd strony z rezerwacją na pierwszym ekranie?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571",
        "followups": json.dumps(["Dzień dobry,\nwracam z krótką wiadomością w sprawie podglądu strony Starego Młyna. Propozycja bezpłatnego projektu jest nadal aktualna. Czy mogę go podesłać?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571"], ensure_ascii=False),
    },
    {
        "business_name": "Optyk Jasny Wzrok", "city": "Lublin", "business_type": "optyk",
        "email": "salon@jasnywzrok.pl", "phone": "+48 81 534 09 22", "website_url": "https://jasnywzrok.pl",
        "address": "ul. Lubartowska 20, Lublin", "status": "new", "autopilot": 0,
        "website_checks": checks(tech_stack=["Shopify"], pagespeed_score=66),
    },
]


def seed():
    db.init_db()
    profile = db.get_profiles()[0]
    ids = {}
    for lead in LEADS:
        row = dict(lead)
        row.setdefault("profile_id", profile["id"])
        lead_id = db.add_lead(**row)
        if lead_id is None:
            print(f"pominięto (już jest): {lead['business_name']}")
            continue
        ids[lead["business_name"]] = lead_id
    if not ids:
        print("Nic nie dodano, dane demo już są w bazie.")
        return

    def message(name, kind, subject, body, status, **extra):
        if name not in ids:
            return
        row_id = db.add_message(ids[name], kind=kind, direction="out", subject=subject, body=body, status=status,
                                scheduled_at=extra.get("scheduled_at", db.now_iso()), message_id=extra.get("message_id", ""))
        if extra:
            db.update_message(row_id, **{k: v for k, v in extra.items() if k in ("sent_at", "error")})

    message("Salon Fryzjerski Klaudia", "initial", "Rzut oka na salon-klaudia.pl z telefonu",
            "Dzień dobry,\nwyszukałem Salon Klaudia na telefonie, udając klientkę z Krowodrzy, która chce się umówić na dziś. Strona otwiera się w wersji na komputer, cennik trzeba powiększać palcami, a przycisku do umówienia nie ma nigdzie na pierwszym ekranie.\n\nCzy mogę podesłać bezpłatny podgląd ekranu głównego po poprawkach?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571", "queued")
    message("Przedszkole Językowe Little Stars", "initial", "Zapisy do Little Stars w trzy kliknięcia zamiast siedmiu",
            "Dzień dobry,\nszukałem, jak zapisać dziecko do Little Stars i formularz znalazłem dopiero po siedmiu kliknięciach.\n\nCzy mogę podesłać bezpłatny podgląd strony głównej z zapisami na pierwszym ekranie?\n\nSzymon Laskowski\nszymonlaskowski.pl\n+48 731 531 571", "queued")
    message("Pizzeria Pod Kasztanem", "initial", "Menu Pod Kasztanem oczami klienta z telefonu", "Dzień dobry,\nszukałem pizzerii na Głównym Mieście z telefonu...", "sent",
            message_id="<demo-1@leadgen.local>", sent_at=days_ago(3))
    message("Kwiaciarnia Stokrotka", "initial", "Stokrotka w Google bez własnej strony", "Dzień dobry,\nszukałem kwiaciarni przy Świdnickiej...", "sent",
            message_id="<demo-2@leadgen.local>", sent_at=days_ago(5))
    message("Restauracja Stary Młyn", "initial", "Rezerwacja w Starym Młynie z telefonu", "Dzień dobry,\nchciałem zarezerwować stolik...", "sent",
            message_id="<demo-3@leadgen.local>", sent_at=days_ago(10))
    message("Restauracja Stary Młyn", "followup", "Re: Rezerwacja w Starym Młynie z telefonu", "Dzień dobry,\nwracam z krótką wiadomością...", "sent",
            message_id="<demo-4@leadgen.local>", sent_at=days_ago(6))
    message("Hydraulik Express Kowalczyk", "initial", "Ostrzeżenie w Chrome na hydraulik-express.pl", "Dzień dobry,\nwszedłem na Państwa stronę z telefonu...", "failed",
            error="Profil Szymon Laskowski nie ma skonfigurowanej skrzynki pocztowej")

    if not db.get_campaigns():
        fryzjer = db.add_campaign("fryzjer", "Kraków", target_count=20, profile_id=profile["id"])
        db.update_campaign(fryzjer, found_count=2, active=0, last_run_at=db.now_iso())
        mechanik = db.add_campaign("mechanik", "Łódź", target_count=10)
        db.update_campaign(mechanik, found_count=10, active=0, last_run_at=days_ago(1))
        booksy = db.add_campaign("kosmetyczka", "Warszawa", target_count=15, no_website=True)
        db.update_campaign(booksy, found_count=3, active=0, last_error="Google nie zwraca już nowych firm dla tego zapytania", last_run_at=days_ago(2))

    for purpose, model, tokens_in, tokens_out in [
        ("analiza", "claude-opus-5", 9800, 4100), ("mail", "claude-opus-5", 3300, 1900),
        ("analiza", "claude-opus-5", 15200, 4600), ("mail", "claude-opus-5", 3100, 2100),
        ("mail", "claude-opus-5", 1900, 1700),
    ]:
        db.add_usage(purpose, model, tokens_in, tokens_out)

    print(f"Dodano {len(ids)} firm, kolejkę, kampanie i zużycie API. Odpal: python app.py")
    print("Kampanie są zatrzymane, a nowe leady poza autopilotem, żeby demo nie uruchamiało płatnych wywołań.")


if __name__ == "__main__":
    seed()
