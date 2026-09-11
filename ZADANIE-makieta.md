# Zadanie: druga ścieżka outreachu oparta na makiecie

## Kontekst

System znajduje lokalne firmy (Google Places), scrapuje ich stronę, robi audyt agentowy
i pisze cold maila opartego na znalezionych usterkach. Działa na produkcji, wysłane 11 maili,
segment: pensjonaty w Wiśle i Szczyrku.

Dziś `run_analysis` w `pipeline.py` ma cztery gałęzie: firma na zewnętrznej platformie,
strona nieaktywna, audyt agentowy, fallback. Makieta (`mockup.py`, `db.mockup_html`,
`db.mockup_image`) powstaje tylko dla firm bez własnej strony albo ze stroną martwą.

## Cel

Dodać drugą ścieżkę: dla firmy, która **ma** działającą stronę, wygenerować makietę lepszej
wersji i wysłać maila opartego na makiecie zamiast na liście usterek.

To nie jest przebudowa. Makieta i mail z makietą już istnieją, trzeba zdjąć warunek, poprawić
jakość generowania i dołożyć pomiar.

## Zasada nadrzędna

**Nie psuć ścieżki audytowej.** Ona chodzi na produkcji i wysyła. Każda zmiana ma być
addytywna. W szczególności nie ruszać promptu maila audytowego w `analyzer.py` (ok. linia 752)
ani jego reguł antyhalucynacyjnych, bo są przemyślane i zwalidowane.

---

## ETAP 1: konstytucja w generowaniu makiety

Najważniejszy etap. Dotyczy obu ścieżek i bez niego reszta nie ma sensu.

**Problem.** Model bez ograniczeń zwraca środek rozkładu: Inter, ciemny hero z poświatą,
gradientowa plama, trzy karty z ikonkami, pasek logotypów, FAQ w akordeonie, CTA na fioletowym
tle. Każda z tych decyzji z osobna jest poprawna i dlatego całość jest nijaka.

Przy ścieżce audytowej słaba makieta jest tylko słaba. Przy ścieżce makietowej **makieta jest
ofertą**, więc przeciętna makieta to dowód, że nadawca nie umie projektować. Sygnał zmienia się
z neutralnego na ujemny.

**Do zrobienia.** Przeczytać `~/warsztat/zasady/konstytucja.md` i wpiąć jej logikę
w `mockup.build_prompt` (`mockup.py`, ok. linia 116). Prompt ma wymuszać:

- **trzy referencje spoza web designu**, wyprowadzone z branży i charakteru firmy (pensjonat
  w Beskidach to nie jest ten sam świat co kancelaria), opisane w prompcie jako źródło systemu
- **pięć do siedmiu reguł łamliwych**, czyli takich, o których da się orzec, że zostały złamane.
  "Elegancko", "minimalistycznie", "nowocześnie", "premium" nie są regułami, bo model uważa,
  że już je spełnia. Regułą jest: dokładnie trzy rozmiary tekstu, zero cieni, wszystkie krawędzie
  ostre, jeden akcent użyty najwyżej cztery razy, odstępy wyłącznie z ciągu 8/16/32/64/128
- **jeden motyw przewodni** powtórzony minimum pięć razy: coś arbitralnego, na przykład wielkie
  numery sekcji na marginesie, obrócona etykieta przy krawędzi, jeden element zawsze wychodzący
  poza siatkę
- **wąskie tokeny**: trzy szarości, nie dziewięć. Trzy rozmiary tekstu, nie sześć. Kompletna
  paleta to paleta bez wyboru
- **kolory wyciągnięte ze zdjęć firmy** (są już zbierane przez `scraper._photo_urls`),
  nie z generatora palet
- **krój spoza pierwszej dwudziestki Google Fonts**, chyba że jest powód, żeby inaczej
- **zmienny rytm**: sekcja bardzo pusta, potem bardzo gęsta. Równomiernie średnio wypełnione
  sekcje czyta się jak metronom

Dołożyć po wygenerowaniu **przebieg audytowy na własnej makiecie**: osobne wywołanie, które
dostaje wygenerowany HTML plus listę reguł i wskazuje, gdzie reguła została złamana, po nazwie
sekcji. Naprawić i dopiero wtedy zapisać. Pytanie brzmi "gdzie złamałeś regułę", nie "czy to ładne".

**Kryterium odbioru.** Wygenerować makiety dla trzech różnych firm z bazy. Mają się od siebie
różnić systemem, a nie tylko treścią i zdjęciami. Jeśli wszystkie trzy mają tę samą strukturę
sekcji i tę samą typografię, etap jest niezaliczony.

---

## ETAP 2: makieta pod linkiem, nie w załączniku

**Problem.** `pipeline.mockup_attachment` wysyła makietę jako JPG w załączniku. Załącznik
w cold mailu obniża dostarczalność, a obrazek gubi to, co w makiecie najlepsze, czyli ruch
i zachowanie.

**Do zrobienia.**

1. Dodać `leads.mockup_token` (TEXT, losowy, nieodgadywalny, ok. 22 znaki).
2. Dodać publiczną trasę `GET /m/<token>`, serwującą `mockup_html`.
3. `auth_check` w `app.py` (`@app.before_request`, linia 20) blokuje wszystko. **Zwolnić z niego
   wyłącznie `/m/<token>`**, nic więcej. Reszta konsoli zostaje za hasłem.
4. Logować wejścia na `/m/<token>` (czas, lead_id). Bez pikseli śledzących i bez skryptów
   analitycznych w makiecie: to jedyny sygnał, jaki jest przed odpowiedzią, i ma być prosty.
5. W mailu ścieżki B: jeden link, zero załączników.

Załącznik zostaje jako mechanizm dla ścieżek, które go dziś używają. Nie usuwać
`mockup_attachment`.

---

## ETAP 3: ścieżka B dla firm z działającą stroną

**Do zrobienia.**

1. W `pipeline.run_analysis` (ok. linia 169) rozszerzyć wybór gałęzi: firma z działającą stroną
   może pójść ścieżką makietową zamiast audytowej, sterowane ustawieniem kampanii.
2. Makieta powstaje ze strony głównej: `scraper.scrape_website` daje już treść, nagłówki,
   zdjęcia, logo i wykrytą technologię. Audyt agentowy przy tej ścieżce **nie jest potrzebny**,
   co obniża koszt na leada.
3. Nowy prompt maila w `analyzer.generate_email`, osobna gałąź. Reguły:
   - **Zero oceny adresata.** Zdania w rodzaju "zrobiłem Pana stronę lepiej" są zakazane:
     zawierają ocenę i zmuszają odbiorcę do obrony. Rama jest ciekawostkowa i bezpretensjonalna,
     na przykład: "Zrobiłem podgląd, jak mogłaby wyglądać strona Willi X. Nic Państwo nie
     zamawiali, wrzucam link, jakby był ciekawy."
   - Zero listy wad. Ścieżka B nie zarzuca niczego, ona pokazuje.
   - Najwyżej 80 słów, bo cała treść to link.
   - Jedna prośba, nie dwie.
   - Zachować istniejące reguły stylu: per Pan/Pani, bez emoji, bez wypunktowań,
     bez kwot i widełek.
   - Podpis i opt-out jak dotąd.

4. **Bramka człowieka.** Autopilot (`worker.py`) przy ścieżce B przygotowuje i kolejkuje,
   ale **nie wysyła sam**. Makieta ma być obejrzana przed wysyłką. Przy ścieżce A automat
   zostaje bez zmian.

---

## ETAP 4: pomiar

Bez tego całość jest zgadywaniem.

1. Dodać `leads.sciezka` (TEXT: `audyt` albo `makieta`), ustawiane w momencie przygotowania.
2. Dodać kampanii tryb: `audyt`, `makieta`, `split`. W trybie `split` przydział pół na pół.
3. W `/api/stats` i w konsoli pokazać osobno dla każdej ścieżki: wysłane, wejścia w link,
   odpowiedzi, klienci.

**Uwaga do interpretacji, ważna.** Przy cold mailu odpowiedzi są rzędu kilku procent, więc
z jedenastu wysyłek oczekiwana liczba odpowiedzi to ułamek. Zero po kilku dniach nie znaczy
nic. Żeby odróżnić ścieżkę dwuprocentową od dziesięcioprocentowej, trzeba grubo ponad setki
wysyłek na ścieżkę. Nie wyciągać wniosków wcześniej i nie przebudowywać niczego na podstawie
pierwszych kilkunastu maili.

---

## Czego nie robić

- Nie ruszać promptu maila audytowego ani jego reguł antyhalucynacyjnych.
- Nie usuwać ścieżki audytowej ani wysyłki z załącznikiem.
- Nie otwierać konsoli publicznie. Wyjątkiem od `auth_check` jest wyłącznie `/m/<token>`.
- Nie dokładać pikseli śledzących ani zewnętrznej analityki do makiety.
- Nie zwiększać wolumenu wysyłki przy okazji tych zmian. To osobna decyzja i osobne ryzyko
  (dostarczalność, domena, art. 10 ustawy o świadczeniu usług drogą elektroniczną).

## Kolejność

Etap 1 jest wart zrobienia nawet gdyby reszta nie powstała, bo poprawia też makiety w ścieżkach,
które już działają. Etapy 2 i 3 idą razem. Etap 4 przed pierwszą realną kampanią ścieżki B.
