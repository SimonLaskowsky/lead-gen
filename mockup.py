"""Buduje prompt do wygenerowania makiety strony i sprawdza gotową makietę pod jej własną
konstytucją. Aplikacja nie rysuje makiety sama, tylko zbiera materiał, układa z niego polecenie
i po wgraniu pyta osobne wywołanie modelu, gdzie makieta łamie własne reguły."""

import hashlib
import json
import os
import re

import anthropic

MAX_ZDJEC_W_PROMPCIE = 18
MAX_ZNAKOW_TRESCI = 1800
MAX_ZNAKOW_HTML_W_AUDYCIE = 90000

AUDIT_MODEL = os.getenv("MOCKUP_AUDIT_MODEL", "claude-sonnet-5")
AUDIT_EFFORT = os.getenv("MOCKUP_AUDIT_EFFORT", "medium")

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
    return "".join(b.text for b in message.content if b.type == "text")


def _fakty_firmy(lead, website_data):
    nazwa_na_stronie = (website_data.get("title") or "").strip()
    fakty = [
        ("Nazwa w Google", lead.get("business_name")),
        ("Nazwa na własnej stronie", nazwa_na_stronie),
        ("Branża", lead.get("business_type")),
        ("Miasto", lead.get("city")),
        ("Adres z wizytówki", lead.get("address")),
        ("Telefon", lead.get("phone")),
        ("E-mail", lead.get("email")),
        ("Obecna strona", lead.get("website_url")),
    ]
    return [f"- {etykieta}: {wartosc}" for etykieta, wartosc in fakty if (wartosc or "").strip()]


def _naglowki_i_tresc(website_data):
    linie = []
    h1 = (website_data.get("h1_text") or "").strip()
    if h1:
        linie.append(f"H1 na obecnej stronie: {h1}")
    opis = (website_data.get("meta_description") or "").strip()
    if opis:
        linie.append(f"Meta description: {opis}")
    tekst = _scisniety(website_data.get("text_preview") or website_data.get("full_text") or "")
    if tekst:
        linie.append("Tekst z obecnej strony, do przepisania własnymi słowami:\n" + tekst[:MAX_ZNAKOW_TRESCI])
    return linie


def _scisniety(tekst):
    """Surowy tekst ze strony ma setki pustych linii z menu i pustych kontenerow."""
    return " ".join((tekst or "").split())


def _spis_zdjec(website_data):
    zdjecia = website_data.get("image_urls") or []
    if not zdjecia:
        return []
    linie = [f"Zdjęcia z ich strony ({len(zdjecia)}, pobierz je i użyj tylko tych):"]
    for pozycja in zdjecia[:MAX_ZDJEC_W_PROMPCIE]:
        podpis = f"  ({pozycja['alt']})" if pozycja.get("alt") else ""
        linie.append(f"  {pozycja['url']}{podpis}")
    return linie


def _ustalenia_audytu(analysis):
    if not analysis:
        return []
    notatka = analysis
    if isinstance(analysis, str):
        try:
            odczytane = json.loads(analysis)
            notatka = odczytane.get("analysis", analysis) if isinstance(odczytane, dict) else analysis
        except Exception:
            notatka = analysis
    notatka = re.split(r"\n\nJak model do tego doszedł:", str(notatka))[0].strip()
    if not notatka:
        return []
    return ["Co audyt ustalił o obecnej stronie. Nowa makieta ma to naprawić, nie powtórzyć:",
            notatka]


BEZ_MATERIALU = """Firma nie ma strony, więc nie ma jej tekstów i cały tekst piszesz od zera.
To nie znosi zakazu wymyślania faktów, tylko go zawęża: wolno ci opisywać to, co wynika
z samego zawodu (hydraulik usuwa awarie, montuje armaturę, robi instalacje wod-kan),
a nie wolno ci twierdzić NICZEGO o tej konkretnej firmie. Zakazane są więc lata na rynku,
liczba realizacji, całodobowość, czas dojazdu, gwarancje, ceny, certyfikaty i opinie.
Trzymaj się zakresu usług i obszaru działania, bo to jedyne, co naprawdę wiesz."""


OSIE_UKLADU = [
    "wąska kolumna treści przyklejona do lewej krawędzi ekranu, prawa trzecia część strony zostaje pusta i nic jej nie wypełnia",
    "jedna kolumna o stałej szerokości na środku, z której zdjęcia wychodzą na pełną szerokość okna",
    "dwie kolumny w proporcji mniej więcej 1:2, wąska trzyma etykiety, numery i podpisy, szeroka całą treść",
    "pasy na pełną szerokość okna bez marginesu bocznego, na przemian sam tekst i samo zdjęcie",
    "siatka z pierwszą kolumną celowo pustą, po to żeby treść nigdy nie zaczynała się przy krawędzi",
    "łamanie dwuszpaltowe jak w gazecie, nagłówki i zdjęcia przecinają obie szpalty",
]

KIERUNKI_TYPOGRAFII = [
    "szeryf o dużym kontraście kresek w nagłówkach, neutralny grotesk w tekście",
    "jeden grotesk w obu rolach, role rozróżniane wyłącznie stopniem, grubością i wersalikami",
    "szeryf w tekście ciągłym, grotesk wyłącznie w etykietach, liczbach i nawigacji",
    "krój o technicznym rysunku (slab, mechaniczny, rysowany pod tabliczki) w nagłówkach, wąski grotesk w tekście",
    "monospace w etykietach, numerach i podpisach, szeryf w nagłówkach i w tekście",
    "antykwa w nagłówkach złożona bardzo dużym stopniem, tekst świadomie mały",
]

MOTYWY = [
    "wielkie numery sekcji na marginesie",
    "etykieta obrócona o 90 stopni przy krawędzi ekranu",
    "jedna linia przecinająca każdą sekcję dokładnie w tym samym miejscu",
    "w każdej sekcji jeden element wychodzący poza siatkę",
    "podpisy pod zdjęciami pisane jak w katalogu wystawy: co, gdzie, kiedy",
    "pierwsze słowo sekcji złożone wersalikami w rozstrzelonym odstępie",
]

ZAKAZANE_KROJE = [
    "Inter", "Roboto", "Open Sans", "Montserrat", "Lato", "Poppins", "Raleway", "Nunito",
    "Oswald", "Playfair Display", "Merriweather", "Rubik", "Work Sans", "Mulish", "Ubuntu",
    "Noto Sans", "PT Sans", "Source Sans", "Fira Sans", "Josefin Sans", "Quicksand", "Manrope",
]

ZAKAZANE_CHWYTY = [
    "ciemny hero z poświatą albo z rozmytą plamą gradientu pod nagłówkiem",
    "trzy karty z ikonkami w kółkach",
    "pasek logotypów zaufania",
    "FAQ w akordeonie",
    "CTA na fioletowym albo granatowym pasie przez całą szerokość",
    "sekcja z opiniami i gwiazdkami",
    "licznik liczb, które rosną przy przewijaniu",
    "nagłówek sekcji w rodzaju \"Dlaczego my\", \"O nas\", \"Nasze atuty\"",
    "zaokrąglenie 12 px z miękkim cieniem pod każdym prostokątem",
]


def _rama_projektu(lead) -> dict:
    """Losowanie stałe dla firmy: dwie makiety z tego samego miasta mają dostać różne układy,
    a ta sama firma ma dostać tę samą ramę przy każdym podejściu."""
    ziarno = f"{lead.get('business_name', '')}|{lead.get('city', '')}|{lead.get('business_type', '')}"
    odcisk = hashlib.sha256(ziarno.encode("utf-8")).digest()
    return {
        "uklad": OSIE_UKLADU[odcisk[0] % len(OSIE_UKLADU)],
        "typografia": KIERUNKI_TYPOGRAFII[odcisk[1] % len(KIERUNKI_TYPOGRAFII)],
        "motyw": MOTYWY[odcisk[2] % len(MOTYWY)],
    }


def _krok_konstytucji(lead, rama) -> str:
    branza = lead.get("business_type") or "lokalna firma"
    zakazane_kroje = ", ".join(ZAKAZANE_KROJE)
    zakazane_chwyty = "\n".join(f"  - {chwyt}" for chwyt in ZAKAZANE_CHWYTY)
    return f"""KROK 1: KONSTYTUCJA. Piszesz ją zanim napiszesz pierwszą linię kodu.

Model bez ograniczeń zwraca środek rozkładu. Każda decyzja ze środka rozkładu jest z osobna
poprawna i dlatego całość wychodzi nijaka. Ta makieta jest ofertą, więc przeciętna makieta
jest dowodem, że nadawca nie umie projektować. Konstytucja jest po to, żeby decyzje zapadły
przed kodem i żeby po kodzie dało się sprawdzić, czy zostały dotrzymane.

Wypisz w odpowiedzi, w tej kolejności, zanim cokolwiek zbudujesz:

1. TRZY REFERENCJE SPOZA WEB DESIGNU.
   Wyprowadź je z tej konkretnej firmy: z zawodu, z materiału, z okolicy i z tego, co widać
   na jej zdjęciach. Pensjonat w Beskidach to nie jest ten sam świat co kancelaria w centrum
   miasta, a {branza} to nie jest ten sam świat co jedno i drugie.
   Referencją jest na przykład karta dań z lat sześćdziesiątych, mapa turystyczna, katalog
   wystawy, etykieta przetworów, tabliczka znamionowa maszyny, przewodnik górski, plakat
   filmowy, formularz pocztowy, oznakowanie dworca.
   Referencją NIE jest strona internetowa, biblioteka komponentów, Dribbble ani Awwwards.
   Do każdej referencji jedno zdanie: co konkretnie stąd bierzesz do układu, typografii albo koloru.

2. PIĘĆ DO SIEDMIU REGUŁ ŁAMLIWYCH.
   Warunek dopuszczenia reguły: da się orzec, że została złamana, i da się wskazać gdzie.
   "Elegancko", "minimalistycznie", "nowocześnie", "premium", "czysto" regułami nie są, bo model
   uważa, że już je spełnia.
   Regułą jest: dokładnie trzy rozmiary tekstu na całej stronie, zero cieni, wszystkie krawędzie
   ostre, jeden akcent użyty najwyżej cztery razy, odstępy wyłącznie z ciągu 8/16/32/64/128,
   wszystkie zdjęcia w kadrze 4:5 wyrównane do lewej kolumny.
   Reguły mają wynikać z referencji, nie być przepisane z tej listy.

3. JEDEN MOTYW PRZEWODNI, WRACAJĄCY MINIMUM PIĘĆ RAZY.
   Jeden powtarzalny gest, arbitralny, bo arbitralność jest jedyną rzeczą, której model sam
   z siebie nie wymyśli. Wypisz, w których pięciu miejscach wraca.

4. TOKENY, WĄSKO. Kompletna paleta to paleta bez wyboru.
   - Trzy szarości, nie dziewięć: tło, tekst, linia.
   - Dokładnie trzy rozmiary tekstu, nie sześć. Role rozróżniasz grubością, wersalikami
     i odstępem, nie kolejnym stopniem.
   - Jeden akcent, użyty najwyżej cztery razy na całej stronie.
   - Kolory wyciągnięte próbkowaniem ze zdjęć i z logo firmy, nie z generatora palet.
     Przy każdym kolorze napisz, z którego zdjęcia pochodzi.
   - Jedna jednostka odstępu i jej wielokrotności, nic spoza ciągu.
   - Krawędzie: albo wszystkie ostre, albo wszystkie z tym samym promieniem. Bez mieszania.
   - Krój z Google Fonts, ale spoza pierwszej dwudziestki. Zakazane wprost: {zakazane_kroje}.
     Jeśli mimo to bierzesz krój z tej listy, napisz w konstytucji powód mocniejszy niż wygoda.

5. CZEGO ŚWIADOMIE NIE MA.
   Lista rzeczy odrzuconych, żeby nie wróciły tylnymi drzwiami w trzeciej sekcji.
   Z góry odrzucone i niepodlegające dyskusji:
{zakazane_chwyty}

RAMA WYLOSOWANA DLA TEJ FIRMY.
Trzymasz się jej, żeby dwie różne firmy nie dostały tego samego systemu. Wolno ją zmienić
tylko wtedy, gdy materiał tej firmy naprawdę się w niej nie mieści, i wtedy w konstytucji
piszesz, dlaczego i co bierzesz w zamian.
   - Oś układu: {rama['uklad']}
   - Kierunek typografii: {rama['typografia']}
   - Kandydat na motyw przewodni: {rama['motyw']}. Wolno wziąć własny, jeśli mocniej wynika
     z referencji, ale motyw ma być równie arbitralny i ma wracać minimum pięć razy.

RYTM.
Sekcje dobierasz pod branżę ({branza}) i pod to, po co przychodzi klient tej konkretnej firmy,
a nie pod uniwersalny szablon. Nie ma obowiązkowego paska konkretów ani obowiązkowego "o nas".
Rytm ma być zmienny: co najmniej jedna sekcja bardzo pusta, jedno zdanie w dużej przestrzeni,
i co najmniej jedna bardzo gęsta, dużo konkretów blisko siebie. Pas sekcji równomiernie średnio
wypełnionych czyta się jak metronom i jest błędem sam w sobie."""


ZASADY = """KROK 2: BUDOWA.

1. Najpierw pobierz zdjęcia i ZOBACZ je. Złóż z nich jedną kontaktówkę w siatce z podpisami
   i obejrzyj ją, zamiast oglądać każde z osobna. Odrzuć te słabe: prześwietlone, przypadkowe,
   przestarzałe wnętrza. Lepiej użyć sześciu dobrych niż piętnastu jakichkolwiek.
2. Paletę wyciągnij próbkowaniem z ich zdjęć i logo, nie wybieraj jej z głowy.
   Kolor, który już u nich występuje, wygląda jak ich marka, a nie jak cudzy szablon.
3. Kadruj kodem pod docelowe proporcje i podbij lekko kontrast i nasycenie.
   Zdjęcia HDR z aparatu są płaskie i w surowej postaci wyglądają blado.
4. Jeden plik HTML, samodzielny: zdjęcia wbudowane jako data URI, żadnych odwołań do sieci
   poza fontami. Ten plik ma się otworzyć u klienta bez internetu i wejść w kolumnę mockup_html.

Twarde wymagania, każde sprawdź po wyrenderowaniu, nie na oko:

- Hero mieści się w pierwszym ekranie, nagłówek najwyżej w DWÓCH liniach, podtytuł najwyżej 20 słów.
  Zmierz liczbę linii w przeglądarce, nie zgaduj.
- Zero przepełnienia w poziomie przy 1440 px i przy 390 px. Zmierz
  scrollWidth minus clientWidth, ma wyjść 0 na obu.
- Telefon klikalny (tel:) w nagłówku i w stopce, na każdej szerokości.
- Tekst na zdjęciu ma czytelną zasłonę tam, gdzie leży, a zdjęcie zostaje zdjęciem tam, gdzie tekstu nie ma.
- Bez myślników i półpauz (znaki — i –).

Czego nie wolno:

- NIE WYMYŚLAJ ŻADNYCH FAKTÓW. Adres, ulica, godziny otwarcia, doba hotelowa, ceny, liczba pokoi,
  lata działalności, oceny: jeśli nie ma tego w materiale wyżej, tego nie ma na stronie.
  To idzie do prawdziwej firmy i jeden zmyślony szczegół kończy rozmowę.
- Nie wklejaj opinii klientów ani cytatów, nawet jeśli są na ich obecnej stronie.
- Nie używaj zdjęć z internetu ani stockowych. Tylko ich własne.
- Nie kopiuj ich tekstów słowo w słowo, przepisz je krócej i konkretniej, zachowując fakty.

Na koniec wyrenderuj stronę w 1440 i w 390, obejrzyj oba zrzuty i popraw to, co siada.
Pierwsza wersja zwykle ma zły kadr hero albo nagłówek łamiący się na trzy linie."""


KROK_AUDYTU = """KROK 3: AUDYT NA WŁASNEJ MAKIECIE. Osobnym wywołaniem, nie w tej samej głowie.

Otwórz nowe wywołanie modelu, czyli świeży kontekst albo podagenta, i daj mu dokładnie dwie rzeczy:
gotowy HTML z wyciętymi data URI, żeby się zmieścił, oraz listę swoich reguł z konstytucji.
Nie dawaj mu uzasadnień ani opowieści o tym, co chciałeś osiągnąć, bo wtedy audyt kończy się
przytaknięciem.

Pytanie brzmi: GDZIE ta reguła jest złamana. Nie brzmi: czy to ładne, bo na tamto pytanie model
odpowiada, że tak. Audytujący ma dla każdej reguły podać nazwę sekcji i miejsce złamania,
a przy regule dotrzymanej napisać wprost, że sprawdził.

Sprawdź przy okazji te cztery, niezależnie od twoich reguł:
- motyw przewodni wraca minimum pięć razy, wypisz gdzie,
- da się usunąć 20 procent: jedna sekcja, połowa ikon, nagłówek w rodzaju "Dlaczego my",
- rytm jest zmienny, nie ma pasa sekcji równomiernie średnio wypełnionych,
- makieta trzyma się na telefonie, nie tylko na desktopie.

Popraw wszystko, co audyt wskazał, i dopiero wtedy oddaj plik. Jeśli poprawka łamie inną regułę,
wróć do audytu. Konsola po wgraniu puści ten sam audyt jeszcze raz i pokaże, co zostało."""


KONSTYTUCJA_START = "<!--KONSTYTUCJA"
KONSTYTUCJA_KONIEC = "KONIEC KONSTYTUCJI-->"


KROK_ODDANIA = f"""KROK 4: ODDANIE PLIKU.

Plik zaczyna się blokiem konstytucji, dokładnie w tym formacie, bo konsola go z pliku wyciąga
i puszcza audyt po raz drugi:

{KONSTYTUCJA_START}
REFERENCJE: trzy, każda w jednej linii, z tym co z niej bierzesz
REGULY:
1. pierwsza reguła, sformułowana tak, że da się orzec złamanie
2. druga
3. trzecia
4. czwarta
5. piąta
MOTYW: jaki, a po przecinku pięć miejsc, w których wraca
TOKENY: kroje, trzy rozmiary, trzy szarości plus akcent, jednostka odstępu, krawędzie
ODRZUCONE: czego świadomie nie ma
{KONSTYTUCJA_KONIEC}

Potem cały HTML. Bloku konstytucji nie widać na stronie, bo to komentarz, więc nie zmienia
niczego dla odbiorcy, a bez niego konsola nie ma czego sprawdzać."""


def build_prompt(lead, website_data=None, analysis=None) -> str:
    website_data = website_data or {}
    ma_strone = bool((lead.get("website_url") or "").strip())
    branza = lead.get("business_type") or "lokalna firma"
    miasto = lead.get("city") or ""

    czesci = []
    if ma_strone:
        czesci.append(
            f"Zaprojektuj i zbuduj makietę NOWEJ strony głównej dla firmy {lead.get('business_name', '')}. "
            "Firma ma już stronę, ale słabą. Makieta ma pokazać właścicielowi, o ile lepiej może to wyglądać "
            "przy tych samych materiałach, które już ma."
        )
    else:
        czesci.append(
            f"Zaprojektuj i zbuduj makietę strony głównej dla firmy {lead.get('business_name', '')} "
            f"({branza}, {miasto}). Firma NIE MA własnej strony. Makieta ma pokazać właścicielowi, "
            "jak jego usługi mogłyby wyglądać w sieci."
        )

    czesci.append("Materiał, którym dysponujesz:\n" + "\n".join(_fakty_firmy(lead, website_data)))

    tresc = _naglowki_i_tresc(website_data)
    if tresc:
        czesci.append("\n".join(tresc))

    zdjecia = _spis_zdjec(website_data)
    if zdjecia:
        czesci.append("\n".join(zdjecia))
    else:
        czesci.append(
            "Nie mamy ich zdjęć ze strony. Weź zdjęcia z wizytówki Google (Places Photos) dla tego miejsca. "
            "Jeśli i tam ich nie ma, zbuduj stronę bez fotografii, na typografii, kolorze i układzie. "
            "Nie podstawiaj zdjęć stockowych ani cudzych."
        )

    logo = (website_data.get("logo_url") or "").strip()
    if logo:
        czesci.append(f"Logo: {logo}\nPobierz je i użyj w nagłówku oraz w stopce. Nie rysuj nowego znaku, "
                      "makieta ma wyglądać jak ich strona zrobiona lepiej, a nie jak cudza marka.")
    else:
        czesci.append("Nie znaleźliśmy ich logo. Nie rysuj żadnego znaku graficznego ani sygnetu. "
                      "Zamiast tego złóż samą nazwę firmy krojem strony jako sygnaturę słowną.")

    ustalenia = _ustalenia_audytu(analysis)
    if ustalenia:
        czesci.append("\n".join(ustalenia))

    if not ma_strone:
        czesci.append(BEZ_MATERIALU)

    czesci.append(_krok_konstytucji(lead, _rama_projektu(lead)))
    czesci.append(ZASADY)
    czesci.append(KROK_AUDYTU)
    czesci.append(KROK_ODDANIA)
    return "\n\n".join(czesci)


def wyodrebnij_konstytucje(html: str) -> str:
    """Blok, który model zostawia na początku pliku. Bez niego nie ma pod co audytować makiety."""
    poczatek = (html or "").find(KONSTYTUCJA_START)
    if poczatek == -1:
        return ""
    koniec = html.find(KONSTYTUCJA_KONIEC, poczatek)
    if koniec == -1:
        return ""
    return html[poczatek + len(KONSTYTUCJA_START):koniec].strip()


NAGLOWEK_KONSTYTUCJI = re.compile(r"^[A-ZĄĆĘŁŃÓŚŹŻ ]{4,}:")
NUMER_REGULY = re.compile(r"^\s*\d+[.)]\s+(.+)$")


def reguly_z_konstytucji(konstytucja: str) -> list[str]:
    reguly = []
    w_regulach = False
    for linia in (konstytucja or "").splitlines():
        if linia.strip().upper().startswith("REGULY") or linia.strip().upper().startswith("REGUŁY"):
            w_regulach = True
            continue
        if w_regulach and NAGLOWEK_KONSTYTUCJI.match(linia.strip()):
            break
        dopasowanie = NUMER_REGULY.match(linia)
        if w_regulach and dopasowanie:
            reguly.append(dopasowanie.group(1).strip())
    return reguly


DATA_URI = re.compile(r"data:[a-zA-Z0-9.+/-]+;base64,[A-Za-z0-9+/=\s]{200,}")


def html_bez_obrazow(html: str) -> str:
    """Makieta ma zdjęcia wklejone jako data URI i waży megabajty. Do audytu jedzie sam układ."""
    lzejszy = DATA_URI.sub("data:zdjecie-wyciete", html or "")
    if len(lzejszy) <= MAX_ZNAKOW_HTML_W_AUDYCIE:
        return lzejszy
    return lzejszy[:MAX_ZNAKOW_HTML_W_AUDYCIE] + "\n(dalsza część pliku ucięta, audytuj to, co widzisz)"


DEKLARACJE_KROJU = re.compile(r"font-family\s*:[^;}\"']+|fonts\.googleapis\.com/[^\"'>\s]+")
MYSLNIKI = ("—", "–")


def html_bez_konstytucji(html: str) -> str:
    """Sam kod strony. Blok konstytucji wymienia zakazane kroje i chwyty, więc szukanie ich
    w całym pliku znajdowałoby je w spisie odrzuconych."""
    poczatek = (html or "").find(KONSTYTUCJA_START)
    koniec = (html or "").find(KONSTYTUCJA_KONIEC, poczatek)
    if poczatek == -1 or koniec == -1:
        return html or ""
    return html[:poczatek] + html[koniec + len(KONSTYTUCJA_KONIEC):]


def _naruszenia_bez_modelu(html: str, konstytucja: str) -> list[dict]:
    """Trzy rzeczy da się orzec bez pytania modelu, więc nie ma po co za nie płacić."""
    naruszenia = []
    strona = html_bez_konstytucji(html)
    kroje = " ".join(DEKLARACJE_KROJU.findall(strona)).lower()
    for nazwa_kroju in ZAKAZANE_KROJE:
        if nazwa_kroju.lower() in kroje:
            naruszenia.append({
                "regula": "Krój spoza pierwszej dwudziestki Google Fonts",
                "sekcja": "tokeny",
                "dowod": f"w krojach strony siedzi {nazwa_kroju}",
            })
    if any(znak in html_bez_obrazow(strona) for znak in MYSLNIKI):
        naruszenia.append({
            "regula": "Bez myślników i półpauz",
            "sekcja": "treść",
            "dowod": "w tekście makiety jest znak — albo –",
        })
    liczba_regul = len(reguly_z_konstytucji(konstytucja))
    if konstytucja and not 5 <= liczba_regul <= 7:
        naruszenia.append({
            "regula": "Od pięciu do siedmiu reguł łamliwych",
            "sekcja": "konstytucja",
            "dowod": f"reguł jest {liczba_regul}",
        })
    return naruszenia


BRAK_KONSTYTUCJI = {
    "regula": "Plik zaczyna się blokiem konstytucji",
    "sekcja": "cały plik",
    "dowod": "nie ma bloku KONSTYTUCJA, więc nie wiadomo, jakich reguł ta makieta miała się trzymać",
}


def build_audit_prompt(html: str, konstytucja: str) -> str:
    reguly = reguly_z_konstytucji(konstytucja)
    spis_regul = "\n".join(f"{numer}. {regula}" for numer, regula in enumerate(reguly, start=1))
    return f"""Sprawdzasz makietę strony pod jej własną konstytucją. Nie oceniasz, czy jest ładna,
bo na takie pytanie odpowiedź zawsze brzmi "tak" i nic z niej nie wynika. Pytanie brzmi:
GDZIE ta reguła jest złamana.

Konstytucja tej makiety, spisana przez jej autora:

{konstytucja}

Reguły do sprawdzenia, po kolei:

{spis_regul or "(autor nie wypisał reguł numerami, wyciągnij je z konstytucji wyżej)"}

Sprawdź dodatkowo, niezależnie od reguł autora:
- czy motyw przewodni wraca minimum pięć razy,
- czy rytm jest zmienny, czy sekcje są równomiernie średnio wypełnione,
- czy nie wróciły chwyty odrzucone w konstytucji.

Zasady orzekania:
- Idziesz sekcja po sekcji i każde złamanie wskazujesz po nazwie sekcji oraz po fragmencie kodu
  albo selektorze, z którego to widać.
- Nie zgłaszasz przeczuć. Zgłaszasz to, co widać w kodzie.
- Regułę, której dotrzymano, wpisujesz na listę sprawdzonych, a nie przemilczasz.

Kod makiety (zdjęcia wycięte, zostały same znaczniki i style):

{html_bez_obrazow(html)}

Odpowiedz wyłącznie JSON-em, bez zdania przed ani po:
{{"zlamane": [{{"regula": "...", "sekcja": "...", "dowod": "..."}}], "sprawdzone": ["...", "..."]}}"""


def _parse_audyt(raw: str) -> dict:
    poczatek = raw.find("{")
    koniec = raw.rfind("}")
    if poczatek == -1 or koniec == -1:
        return {"zlamane": [], "sprawdzone": [], "blad": "audyt nie zwrócił JSON-a"}
    try:
        odczytane = json.loads(raw[poczatek:koniec + 1])
    except Exception as e:
        return {"zlamane": [], "sprawdzone": [], "blad": f"audyt zwrócił zepsuty JSON: {e}"}
    zlamane = [z for z in odczytane.get("zlamane", []) if isinstance(z, dict)]
    return {"zlamane": zlamane, "sprawdzone": odczytane.get("sprawdzone", []) or [], "blad": ""}


def audyt_makiety(html: str) -> dict:
    """Osobne wywołanie na gotowym pliku: nie wie, co autor chciał osiągnąć, widzi sam kod i reguły."""
    konstytucja = wyodrebnij_konstytucje(html)
    if not konstytucja:
        return {"zlamane": [BRAK_KONSTYTUCJI], "sprawdzone": [], "blad": ""}

    wynik = {"zlamane": list(_naruszenia_bez_modelu(html, konstytucja)), "sprawdzone": [], "blad": ""}
    message = _client().messages.create(
        model=AUDIT_MODEL,
        max_tokens=4000,
        output_config={"effort": AUDIT_EFFORT},
        messages=[{"role": "user", "content": build_audit_prompt(html, konstytucja)}],
    )
    _record(message, "audyt makiety")

    od_modelu = _parse_audyt(_text(message))
    wynik["zlamane"].extend(od_modelu["zlamane"])
    wynik["sprawdzone"] = od_modelu["sprawdzone"]
    wynik["blad"] = od_modelu["blad"]
    return wynik
