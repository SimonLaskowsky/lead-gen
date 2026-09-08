import base64
import io
import json
import os
import re
import html as html_lib

import ipaddress
import socket
from urllib.parse import urlparse

import anthropic
import requests
from bs4 import BeautifulSoup

import scraper

STEP_MODEL = os.getenv("AUDIT_STEP_MODEL", "claude-sonnet-5")
MEMO_MODEL = os.getenv("AUDIT_MEMO_MODEL", "claude-opus-5")
STEP_EFFORT = os.getenv("AUDIT_STEP_EFFORT", "medium")
MAX_TOOL_ROUNDS = int(os.getenv("AUDIT_MAX_STEPS", "14"))
MAX_PAGES = 5
MAX_SCREENSHOTS = 16
MAX_SCREENS_PER_SCROLL = 4

DESKTOP_WIDTH = 1280
VIEWPORT_HEIGHTS = {1280: 800, 1024: 768, 390: 844}
MAX_SCROLL_THROUGH_PX = 20000
NAVIGATION_TIMEOUT_MS = 30000

on_usage = None


def _client():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _record(message, purpose):
    if on_usage:
        on_usage(purpose, message.model, message.usage.input_tokens, message.usage.output_tokens)


def _bare_host(host):
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _same_site(target_host, base_host):
    target, base = _bare_host(target_host), _bare_host(base_host)
    return bool(base) and (target == base or target.endswith("." + base))


def _public_ip(host):
    """Zwraca publiczny adres IP hosta albo None, gdy host jest adresem IP, nie rozwiazuje sie
    albo wskazuje na siec prywatna, loopback, link-local."""
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    try:
        resolved = [info[4][0] for info in socket.getaddrinfo(host, None)]
    except socket.gaierror:
        return None
    for address in resolved:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return None
    return resolved[0] if resolved else None


def _url_allowed(url, base_url):
    """Agent oglada tylko strone audytowanej firmy: ten sam host (bez www) albo jego poddomena,
    http lub https, nazwa zamiast IP, rozwiazywana na adres publiczny. Chroni przed
    naprowadzeniem modelu przez cudza strone na siec wewnetrzna serwera."""
    try:
        target = urlparse(url)
        base = urlparse(base_url)
    except Exception:
        return False
    if target.scheme not in ("http", "https") or not target.hostname or not base.hostname:
        return False
    if not _same_site(target.hostname, base.hostname):
        return False
    return _public_ip(target.hostname) is not None


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


def _fetch_within_site(url, hops=5):
    """Pobiera strone bez automatycznych przekierowan: kazdy kolejny adres przechodzi te same
    sprawdzenia co adres wpisany przez model."""
    current = url
    for _ in range(hops):
        if not _url_allowed(current, url):
            raise ValueError("przekierowanie poza domenę firmy")
        response = requests.get(current, timeout=20, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
            current = _absolute(current, response.headers["Location"])
            continue
        _fix_missing_charset(response)
        return response
    raise ValueError("za dużo przekierowań")


def _fix_missing_charset(response):
    content_type = response.headers.get("Content-Type", "")
    if "charset" not in content_type.lower():
        response.encoding = "utf-8"


def _page_facts(url):
    """Fakty ze strony w postaci krotkiego tekstu dla modelu: to, co czlowiek sprawdzilby w kodzie."""
    data = scraper.scrape_website(url) or {}
    if data.get("outsourced_platform"):
        return f"To nie jest wlasna strona, tylko profil na platformie {data['outsourced_platform']}.", data, []
    if data.get("inactive"):
        return f"Pod tym adresem nie ma dzialajacej strony firmy: {data['inactive_reason']}.", data, []
    if data.get("error"):
        return f"Nie udalo sie otworzyc strony: {data['error']}", data, []
    try:
        response = _fetch_within_site(url)
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


def _image_block(jpg):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpg).decode("utf-8")}}


def _megabytes(size_bytes):
    return f"{size_bytes / 1_000_000:.1f} MB"


NO_MOTION_CSS = """
*, *::before, *::after {
  animation-duration: 0s !important; animation-delay: 0s !important;
  transition-duration: 0s !important; transition-delay: 0s !important;
}
html { scroll-behavior: auto !important; }
[data-aos], .aos-init, .wow, [data-sal], [data-scroll], .animate__animated {
  opacity: 1 !important; transform: none !important; visibility: visible !important;
}
"""

SCROLL_ANIMATION_LIBRARIES = {
    "aos": r"aos\.js|aos\.css|AOS\.init|data-aos=",
    "wow.js": r"wow\.min\.js|new WOW\(",
    "sal.js": r"sal\.js|data-sal=",
    "ScrollTrigger (GSAP)": r"ScrollTrigger",
    "ScrollReveal": r"ScrollReveal",
    "animate.css": r"animate\.css|animate__animated",
}

PINNED_JS_HELPER = """
  const isPinned = el => {
    let node = el;
    while (node && node !== document.documentElement) {
      const position = getComputedStyle(node).position;
      if (position === 'fixed' || position === 'sticky') return true;
      node = node.parentElement;
    }
    return false;
  };
"""

LOW_CONTRAST_JS = """() => {""" + PINNED_JS_HELPER + """
  const channel = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  const luminance = (r, g, b) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  const parseColor = s => {
    const m = s && s.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const backgroundOf = el => {
    let node = el;
    while (node && node !== document.documentElement) {
      const cs = getComputedStyle(node);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return { image: true };
      const c = parseColor(cs.backgroundColor);
      if (c && c.a > 0.9) return c;
      node = node.parentElement;
    }
    const bodyColor = parseColor(getComputedStyle(document.body).backgroundColor);
    return (bodyColor && bodyColor.a > 0.9) ? bodyColor : { r: 255, g: 255, b: 255, a: 1 };
  };
  const effectiveOpacity = el => {
    let opacity = 1, node = el;
    while (node && node !== document.documentElement) { opacity *= parseFloat(getComputedStyle(node).opacity); node = node.parentElement; }
    return opacity;
  };
  const weak = [];
  const seen = new Set();
  let checked = 0, onImage = 0;
  const candidates = document.querySelectorAll('h1,h2,h3,h4,p,li,a,span,button,td,th,label,strong,b,em,small,div');
  for (const el of candidates) {
    const text = Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').replace(/\\s+/g, ' ').trim();
    if (text.length < 3) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const fg = parseColor(cs.color);
    if (!fg) continue;
    const bg = backgroundOf(el);
    if (bg.image) { onImage += 1; continue; }
    checked += 1;
    const alpha = fg.a * effectiveOpacity(el);
    const r = fg.r * alpha + bg.r * (1 - alpha), g = fg.g * alpha + bg.g * (1 - alpha), b = fg.b * alpha + bg.b * (1 - alpha);
    const l1 = luminance(r, g, b), l2 = luminance(bg.r, bg.g, bg.b);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight) >= 700;
    const large = size >= 24 || (size >= 18.66 && bold);
    const required = large ? 3 : 4.5;
    if (ratio >= required) continue;
    const key = text.slice(0, 40);
    if (seen.has(key)) continue;
    seen.add(key);
    weak.push({ tekst: text.slice(0, 60), kontrast: Math.round(ratio * 10) / 10, wymagane: required, y: Math.round(rect.top + window.scrollY), px: Math.round(size), przyklejony: isPinned(el) });
    if (weak.length >= 15) break;
  }
  return { sprawdzone: checked, na_zdjeciu: onImage, slabe: weak };
}"""

CLICKABLES_JS = """() => {""" + PINNED_JS_HELPER + """
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('a, button, [role=button]')) {
    const label = (el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    if (!label) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const href = el.getAttribute('href') || '';
    const key = label + '|' + href;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ tekst: label, href: href, target: el.getAttribute('target') || '', y: Math.round(rect.top + window.scrollY), wysokosc: Math.round(rect.height), przyklejony: isPinned(el) });
    if (out.length >= 40) break;
  }
  return out;
}"""

MOBILE_METRICS_JS = """() => {
  let smallTargets = 0, tinyText = 0;
  for (const el of document.querySelectorAll('a, button')) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    if (rect.height < 32 || rect.width < 32) smallTargets += 1;
  }
  for (const el of document.querySelectorAll('p, li, span, a, td')) {
    if (!(el.innerText || '').trim()) continue;
    if (parseFloat(getComputedStyle(el).fontSize) < 14) tinyText += 1;
  }
  return {
    wysokosc: document.documentElement.scrollHeight,
    szerokosc_widoku: window.innerWidth,
    szerokosc_tresci: document.documentElement.scrollWidth,
    male_cele: smallTargets,
    drobny_tekst: tinyText,
  };
}"""

PAGE_METRICS_JS = """() => ({
  wysokosc: document.documentElement.scrollHeight,
  szerokosc_widoku: window.innerWidth,
  szerokosc_tresci: document.documentElement.scrollWidth,
})"""


class _Browser:
    def __init__(self, base_url):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.base_url = base_url
        self.browser = None
        self.page = None
        self.host = None
        self.width = DESKTOP_WIDTH
        self.responses = []
        self.html_cache = {}

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            self.playwright.stop()

    def _launch_for(self, host):
        pinned_ip = _public_ip(host)
        if not pinned_ip:
            raise ValueError("host nie rozwiązuje się na adres publiczny")
        if self.browser:
            self.browser.close()
        self.browser = self.playwright.chromium.launch(headless=True, args=[f"--host-resolver-rules=MAP {host} {pinned_ip}"])
        self.page = self.browser.new_page(viewport={"width": self.width, "height": VIEWPORT_HEIGHTS[self.width]})
        self.page.emulate_media(reduced_motion="reduce")
        self.page.on("response", self._remember_response)
        self.host = host

    def _remember_response(self, response):
        self.responses.append(response)

    @property
    def url(self):
        return self.page.url if self.page else ""

    @property
    def has_page(self):
        return bool(self.page) and self.url.startswith(("http://", "https://"))

    @property
    def viewport_height(self):
        return VIEWPORT_HEIGHTS[self.width]

    def goto(self, url):
        host = urlparse(url).hostname or ""
        if host != self.host:
            self._launch_for(host)
        self.responses.clear()
        try:
            self.page.goto(url, timeout=NAVIGATION_TIMEOUT_MS, wait_until="networkidle")
        except Exception:
            self.page.goto(url, timeout=NAVIGATION_TIMEOUT_MS, wait_until="load")
            self.page.wait_for_timeout(2000)
        self.settle()

    def settle(self):
        try:
            scraper._dismiss_cookie_banner(self.page)
        except Exception:
            pass
        try:
            self.page.add_style_tag(content=NO_MOTION_CSS)
        except Exception:
            pass
        self.scroll_through_page()

    def scroll_through_page(self):
        height = min(self.scroll_height(), MAX_SCROLL_THROUGH_PX)
        position = 0
        while position < height:
            position += self.viewport_height
            self.page.evaluate("y => window.scrollTo(0, y)", position)
            self.page.wait_for_timeout(150)
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.wait_for_timeout(300)

    def set_width(self, width):
        if width == self.width:
            return
        self.width = width
        self.page.set_viewport_size({"width": width, "height": VIEWPORT_HEIGHTS[width]})
        self.page.wait_for_timeout(500)
        self.scroll_through_page()

    def scroll_height(self):
        return int(self.page.evaluate("document.documentElement.scrollHeight"))

    def screen_count(self):
        return max(1, -(-self.scroll_height() // self.viewport_height))

    def screenshot_screen(self, screen_number):
        total = self.screen_count()
        number = max(1, min(screen_number, total))
        position = (number - 1) * self.viewport_height
        self.page.evaluate("y => window.scrollTo(0, y)", position)
        self.page.wait_for_timeout(500)
        png = self.page.screenshot(type="png")
        return png, number, total, position

    def metrics(self):
        return self.page.evaluate(PAGE_METRICS_JS)

    def mobile_metrics(self):
        return self.page.evaluate(MOBILE_METRICS_JS)

    def low_contrast(self):
        return self.page.evaluate(LOW_CONTRAST_JS)

    def clickables(self):
        return self.page.evaluate(CLICKABLES_JS)

    def visible_text(self, limit=3500):
        text = self.page.evaluate("document.body.innerText") or ""
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:limit]

    def resource_weights(self):
        sizes_by_type = {}
        heaviest = []
        for response in list(self.responses)[:300]:
            size = self._response_size(response)
            if size is None:
                continue
            kind = response.request.resource_type
            sizes_by_type[kind] = sizes_by_type.get(kind, 0) + size
            heaviest.append((size, response.url))
        heaviest.sort(reverse=True)
        return sizes_by_type, heaviest[:5]

    def failed_assets(self):
        failures = []
        for response in list(self.responses)[:300]:
            try:
                reason = _asset_failure_reason(response)
            except Exception:
                continue
            if reason:
                failures.append((response.request.resource_type, reason, response.url))
        return failures

    @staticmethod
    def _response_size(response):
        header = response.headers.get("content-length", "")
        if header.isdigit():
            return int(header)
        try:
            return len(response.body())
        except Exception:
            return None

    def find_clickable(self, text):
        for role in ("link", "button"):
            locator = self.page.get_by_role(role, name=text, exact=False).first
            try:
                if locator.is_visible(timeout=500):
                    return locator
            except Exception:
                continue
        locator = self.page.get_by_text(text, exact=False).first
        try:
            if locator.is_visible(timeout=500):
                return locator
        except Exception:
            pass
        return None


def _shortened_url(url):
    if len(url) <= 95:
        return url
    return url[:45] + "…" + url[-45:]


def _asset_failure_reason(response):
    kind = response.request.resource_type
    if kind not in ("stylesheet", "script"):
        return ""
    if response.status >= 400:
        return f"odpowiedź {response.status}"
    if kind != "stylesheet":
        return ""
    declared_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if declared_type == "text/css":
        return ""
    return f"serwer oddaje go jako {declared_type or 'typ nieznany'}, więc przeglądarka odrzuca go jako arkusz stylów"


def _broken_assets_line(browser):
    failures = browser.failed_assets()
    if not failures:
        return "Arkusze stylów i skrypty: wszystkie wczytały się poprawnie."
    listing = "\n".join(f"  {kind}, {reason}: {_shortened_url(url)}" for kind, reason, url in failures[:8])
    broken_stylesheets = [item for item in failures if item[0] == "stylesheet"]
    if broken_stylesheets:
        headline = ("STRONA JEST USZKODZONA: nie wczytał się arkusz stylów, więc gość widzi rozsypany układ, "
                    f"a nie projekt strony. Niewczytane pliki ({len(failures)}):")
    else:
        headline = f"Nie wczytały się skrypty strony, część funkcji może nie działać. Niewczytane pliki ({len(failures)}):"
    return headline + "\n" + listing


def _weights_line(browser):
    sizes_by_type, heaviest = browser.resource_weights()
    total = sum(sizes_by_type.values())
    if total == 0:
        return "Waga: brak pomiaru (przeglądarka nie zarejestrowała odpowiedzi)."
    images = sizes_by_type.get("image", 0)
    scripts = sizes_by_type.get("script", 0)
    fonts = sizes_by_type.get("font", 0)
    line = f"Waga pobrana od wejścia na tę stronę: {_megabytes(total)} (zdjęcia {_megabytes(images)}, skrypty {_megabytes(scripts)}, fonty {_megabytes(fonts)})."
    if heaviest:
        size, url = heaviest[0]
        line += f" Najcięższy plik: {url[-80:]} ({_megabytes(size)})."
    line += f" Szacowany czas pobrania: {_seconds_at(total, 10)} przy 10 Mb/s (słabe LTE), {_seconds_at(total, 30)} przy 30 Mb/s (dobre LTE)."
    return line


def _seconds_at(size_bytes, megabits_per_second):
    seconds = size_bytes * 8 / (megabits_per_second * 1_000_000)
    if seconds < 1:
        return "poniżej sekundy"
    return f"ok. {seconds:.0f} s"


def _animation_libraries(page_html):
    found = [name for name, pattern in SCROLL_ANIMATION_LIBRARIES.items() if re.search(pattern, page_html)]
    return found


def _browser_measurements(browser, page_html):
    metrics = browser.metrics()
    screens = -(-metrics["wysokosc"] // browser.viewport_height)
    lines = [_broken_assets_line(browser),
             f"Pomiar w przeglądarce ({browser.width}px): wysokość {metrics['wysokosc']} px, czyli {screens} ekranów."]
    if metrics["szerokosc_tresci"] > metrics["szerokosc_widoku"] + 2:
        lines.append(f"Treść wystaje poza ekran w poziomie: {metrics['szerokosc_tresci']} px przy oknie {metrics['szerokosc_widoku']} px.")
    lines.append(_weights_line(browser))
    libraries = _animation_libraries(page_html)
    if libraries:
        lines.append(f"Animacje przy przewijaniu: {', '.join(libraries)}. Zrzuty robię z wyłączonymi animacjami, więc pokazują stan końcowy, nie wjeżdżające elementy.")
    return "\n".join(lines)


def _code_facts(url):
    try:
        response = _fetch_within_site(url)
    except Exception as error:
        return f"Nie udało się pobrać kodu: {error}"
    page_html = response.text
    soup = BeautifulSoup(page_html, "html.parser")
    lines = []

    title = soup.title.get_text(strip=True) if soup.title else ""
    description = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description_text = (description.get("content") or "").strip() if description else ""
    lines.append(f"Tytuł: {title or 'BRAK'} ({len(title)} znaków)")
    lines.append(f"Meta description: {description_text[:160] or 'BRAK'} ({len(description_text)} znaków)")

    h1_texts = [h.get_text(" ", strip=True)[:70] for h in soup.find_all("h1")]
    lines.append(f"H1: {len(h1_texts)} sztuk" + (": " + " | ".join(h1_texts) if h1_texts else ""))
    h2_texts = [h.get_text(" ", strip=True)[:60] for h in soup.find_all("h2")][:10]
    if h2_texts:
        lines.append("H2: " + " | ".join(h2_texts))

    images = soup.find_all("img")
    without_alt = [img for img in images if not (img.get("alt") or "").strip()]
    background_images = len(re.findall(r"url\(", page_html))
    lines.append(f"Znaczniki img: {len(images)}, bez alt: {len(without_alt)}; tła CSS z obrazkiem w kodzie: {background_images}")

    lang = (soup.html.get("lang") if soup.html else "") or "BRAK"
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    og_image = soup.find("meta", property="og:image")
    schema = bool(soup.find("script", type="application/ld+json"))
    lines.append(f"lang: {lang}; canonical: {'jest' if canonical else 'brak'}; og:image: {'jest' if og_image else 'brak'}; dane strukturalne: {'są' if schema else 'brak'}")

    analytics = []
    for name, pattern in (("Google Analytics/GTM", r"gtag\(|googletagmanager|google-analytics"), ("Facebook Pixel", r"fbq\(|connect\.facebook\.net"), ("Hotjar", r"hotjar"), ("Clarity", r"clarity\.ms"), ("Matomo", r"matomo|piwik")):
        if re.search(pattern, page_html, re.I):
            analytics.append(name)
    lines.append("Analityka: " + (", ".join(analytics) if analytics else "nie wykryto"))

    lines.append("Klikalny telefon (tel:): " + ("jest" if soup.find("a", href=re.compile("^tel:")) else "BRAK"))
    lines.append("Klikalny e-mail (mailto:): " + ("jest" if soup.find("a", href=re.compile("^mailto:")) else "BRAK"))
    lines.append(f"Formularze: {len(soup.find_all('form'))}")
    lines.append("Mapa Google osadzona: " + ("jest" if re.search(r"google\.com/maps|maps\.google", page_html) else "brak"))

    lines.extend(_site_level_facts(url))
    return "\n".join(lines)


def _site_level_facts(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    root = f"{parsed.scheme}://{host}"
    lines = []
    http_root = f"http://{host}/"
    if _url_allowed(http_root, url):
        try:
            response = requests.get(http_root, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=False)
            location = response.headers.get("Location", "")
            if location.startswith("https://"):
                lines.append("Przekierowanie http na https: jest")
            else:
                lines.append(f"Przekierowanie http na https: BRAK (http odpowiada {response.status_code} bez przekierowania)")
        except Exception:
            lines.append("Przekierowanie http na https: http nie odpowiada")
    for name, path in (("robots.txt", "/robots.txt"), ("sitemap.xml", "/sitemap.xml")):
        try:
            status = requests.get(root + path, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).status_code
            lines.append(f"{name}: {'jest' if status == 200 else 'brak'}")
        except Exception:
            lines.append(f"{name}: nie sprawdzono")
    return lines


def _screen_label(browser, item):
    if item.get("przyklejony"):
        return "przyklejony do ekranu"
    return f"ekran {item['y'] // browser.viewport_height + 1}"


def _contrast_report(browser):
    result = browser.low_contrast()
    weak = result["slabe"]
    header = f"Kontrast policzony z kolorów w przeglądarce ({browser.width}px): sprawdzono {result['sprawdzone']} elementów tekstowych, pominięto {result['na_zdjeciu']} leżących na zdjęciach."
    if not weak:
        return header + " Żaden nie jest poniżej normy WCAG (4,5:1 dla zwykłego tekstu, 3:1 dla dużego). Jeśli na zrzucie coś wyglądało blado, to nie był kontrast."
    rows = [f"- \"{item['tekst']}\" ({item['px']}px, {_screen_label(browser, item)}, pozycja {item['y']} px): {item['kontrast']}:1, wymagane {item['wymagane']}:1" for item in weak]
    return header + f" Poniżej normy: {len(weak)}.\n" + "\n".join(rows)


def _clickables_report(browser):
    items = browser.clickables()
    if not items:
        return "Brak widocznych linków i przycisków z tekstem."
    rows = []
    for item in items:
        href = item["href"]
        if not href or href == "#":
            destination = "bez adresu (przycisk skryptowy albo pusty #)"
        elif href.startswith("#"):
            destination = f"kotwica {href} na tej stronie"
        elif href.startswith("tel:"):
            destination = f"telefon {href[4:]}"
        elif href.startswith("mailto:"):
            destination = f"e-mail {href[7:]}"
        else:
            full = _absolute(browser.url, href)
            inside = _same_site(urlparse(full).hostname or "", urlparse(browser.base_url).hostname or "")
            destination = full[:90] + ("" if inside else " (POZA DOMENĄ FIRMY)")
        new_tab = " w nowej karcie" if item["target"] == "_blank" else ""
        rows.append(f"- \"{item['tekst']}\" ({_screen_label(browser, item)}, {item['wysokosc']}px wys.): {destination}{new_tab}")
    return f"Widoczne linki i przyciski ({browser.width}px):\n" + "\n".join(rows)


def _mobile_report(browser):
    browser.set_width(390)
    metrics = browser.mobile_metrics()
    screens = -(-metrics["wysokosc"] // browser.viewport_height)
    lines = [f"Pomiar na telefonie (390px): wysokość {metrics['wysokosc']} px, czyli {screens} ekranów do przewinięcia."]
    if metrics["szerokosc_tresci"] > metrics["szerokosc_widoku"] + 2:
        lines.append(f"Treść wystaje poza ekran w poziomie: {metrics['szerokosc_tresci']} px przy oknie 390 px. Strona nie jest w pełni responsywna.")
    else:
        lines.append("Nic nie wystaje poza ekran w poziomie.")
    lines.append(f"Linki i przyciski mniejsze niż 32 px (trudne do trafienia palcem): {metrics['male_cele']}.")
    lines.append(f"Elementy tekstowe poniżej 14 px: {metrics['drobny_tekst']}.")
    lines.append(_weights_line(browser))
    return "\n".join(lines)


TOOLS = [
    {
        "name": "otworz_strone",
        "description": "Ładuje stronę w przeglądarce (szerokość 1280) i zwraca: fakty z kodu (tytuł, nagłówki, telefon, formularz, ceny, linki z etykietami), pomiar wysokości i wagi oraz zrzut pierwszego ekranu. Używaj do strony głównej i do podstron z właściwą treścią (oferta, cennik, kontakt).",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "przewin",
        "description": "Pokazuje wybrane ekrany bieżącej strony, tak jak widzi je człowiek po przewinięciu. Ekran 1 to pierwszy ekran, 2 to następny i tak dalej; wynik mówi, ile ekranów ma strona. Podaj od 1 do 4 numerów naraz, np. [2, 3, 4], żeby obejrzeć kilka ekranów w jednej rundzie. Szerokość 1280 (desktop), 1024 (laptop) albo 390 (telefon). Tym samym narzędziem wracasz, żeby spojrzeć drugi raz.",
        "input_schema": {"type": "object", "properties": {"ekrany": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 4}, "szerokosc": {"type": "integer", "enum": [1280, 1024, 390]}}, "required": ["ekrany", "szerokosc"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "kliknij",
        "description": "Klika widoczny link albo przycisk o podanym tekście na bieżącej stronie i mówi, co się stało: przewinięcie do sekcji, przejście na podstronę (wtedy dostajesz jej fakty i pierwszy ekran) albo wyjście poza domenę firmy. Zrzut nie mówi, dokąd prowadzi przycisk, kliknięcie mówi.",
        "input_schema": {"type": "object", "properties": {"tekst": {"type": "string"}}, "required": ["tekst"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "zweryfikuj",
        "description": "Twarde sprawdzenie bieżącej strony, którym potwierdzasz albo odrzucasz to, co zobaczyłeś na zrzucie. kontrast: kontrast każdego tekstu policzony z kolorów, z pozycją. przyciski: wszystkie widoczne linki i przyciski z adresami, także te poza domeną. kod: SEO i technikalia (tytuł, H1, alt, analityka, tel:, mailto:, formularze, przekierowanie https, robots, sitemap). telefon: pomiary w 390px (wysokość w ekranach, wystawanie poza ekran, małe cele, drobny tekst, waga). tekst: pełna widoczna treść strony do literówek i tonu. zasoby: czy arkusze stylów i skrypty strony w ogóle się wczytały, bo plik CSS z odpowiedzią 404 rozsypuje cały układ.",
        "input_schema": {"type": "object", "properties": {"co": {"type": "string", "enum": ["kontrast", "przyciski", "kod", "telefon", "tekst", "zasoby"]}}, "required": ["co"], "additionalProperties": False},
        "strict": True,
    },
]

SYSTEM_STEPS = """Jesteś doświadczonym projektantem UI/UX i konsultantem, który ocenia stronę lokalnej firmy tak, jak zrobiłby to człowiek siedzący przed ekranem: otwiera, przewija ekran po ekranie, klika w to, w co kliknąłby gość, a gdy coś wygląda podejrzanie, sprawdza, zanim to zapisze.

Narzędzia:
- otworz_strone: ładuje stronę, daje fakty z kodu, kontrolę wczytania arkuszy stylów i skryptów, pomiar wagi i wysokości oraz zrzut pierwszego ekranu w 1280.
- przewin: pokazuje wybrany ekran (1 = pierwszy) w 1280, 1024 albo 390. Tak oglądasz stronę dalej i tak wracasz, żeby spojrzeć drugi raz.
- kliknij: klika link albo przycisk o podanym tekście i mówi, dokąd prowadzi.
- zweryfikuj: twarde sprawdzenia bieżącej strony: kontrast, przyciski z adresami, kod i SEO, pomiary na telefonie, pełny tekst, zasoby (czy pliki CSS i JS strony się wczytały).

Pętla, którą powtarzasz: zauważ, zwątp, sprawdź.
Zrzut ekranu to podejrzenie, nie wniosek. Zanim uznasz, że tekst jest blady, przycisk prowadzi donikąd, cennika nie ma, układ się łamie albo strona jest ciężka, sprawdź to drugim narzędziem: kontrast przez zweryfikuj kontrast, przycisk przez kliknij albo zweryfikuj przyciski, brak treści przez zweryfikuj tekst albo przewin do innego ekranu, wagę przez pomiar. Zrzuty są robione z wyłączonymi animacjami, ale lazy-load, karuzele, menu i przyklejone nagłówki potrafią oszukać. W dzienniku zapisuj trzy rzeczy: co zauważyłeś, czym sprawdziłeś, co wyszło. Podejrzenie, które się nie potwierdziło, też zapisz, żeby nie trafiło do notatki.

Zanim ocenisz wygląd, przeczytaj w pomiarze linię o arkuszach stylów i skryptach. Gdy stoi tam "STRONA JEST USZKODZONA", plik CSS strony nie wczytał się i to, co widzisz na zrzucie, nie jest projektem, tylko jego ruinami. Puste sekcje, ucięte nagłówki, napisy wychodzące poza krawędź, nachodzące elementy, znikające tła i rozjechany układ są wtedy skutkiem tej jednej awarii, a nie decyzjami projektanta, więc nie opisuj ich jako uwag o designie ani nie zgaduj, że autor czegoś nie dokończył. Nazwij awarię, podaj nazwę niewczytanego pliku, sprawdź narzędziem zweryfikuj zasoby, czy dotyczy też podstron, i na tym oprzyj podsumowanie. Odwrotnie też: pustej sekcji nie zgłaszaj jako awarii, dopóki ta linia mówi, że wszystko wczytało się poprawnie.

Co oglądasz:
1. Strona główna, pierwszy ekran w 1280, trzy sekundy: czym firma się zajmuje, gdzie, co ma zrobić gość, z którego roku to wygląda. Potem przewijaj kolejne ekrany aż do stopki. Nie zgaduj, co jest niżej.
2. Główny przycisk i najważniejszy link: kliknij. Strona główna bywa rozdzielnią, właściwa treść jest o klik dalej. Oceniaj stronę, którą zobaczy gość.
3. Praca gościa: 3 do 5 rzeczy, po które przychodzi klient tej branży (cena, termin, kontakt, dojazd, oferta). Dla każdej: czy da się załatwić na stronie i ile to klików.
4. Telefon: pierwszy ekran i co najmniej jeden głębszy ekran w 390 oraz zweryfikuj telefon.
5. Liczby wyłącznie z narzędzi: waga, czas pobrania, wysokość w ekranach, kontrast, liczba słów. Czas ładowania jest policzony w pomiarze wagi, nie przeliczaj go sam. Prędkości ani kontrastu nie oceniasz na oko. Klikalność telefonu bierzesz z faktów, nie ze zrzutu.
6. Tekst: na stronie z właściwą treścią zawsze wywołaj zweryfikuj tekst i przeczytaj całość pod kątem literówek, zdań bez sensu, tonu (zakazy i dopłaty przed zaletami) i obietnic bez konkretu. Wypisz znalezione literówki dosłownie.
7. Zdjęcia: przy każdym ekranie ze zdjęciami nazwij, czy to zdjęcia własne (obiekt, ludzie, produkty firmy), czy stockowe, czy są spójne jakością i proporcjami, i czy układ zostawia puste połacie.
8. Co jest dobre, zapisz równie konkretnie jak to, co złe.

Budżet: najwyżej {max_steps} rund narzędzi, więc oglądaj to, co rozstrzyga, ale nie kończ, zanim nie zobaczysz stopki, telefonu i celu głównego przycisku. Przed każdym wywołaniem napisz jedno krótkie zdanie po polsku: co sprawdzasz i dlaczego (to jest twój dziennik). Gdy wiesz dość, napisz "GOTOWE" i podsumuj w kilku zdaniach: która strona jest właściwa, co jest potwierdzonym największym problemem, co wyglądało na problem, ale nim nie jest, i co jest dobre. Nie pisz jeszcze pełnej notatki."""

MEMO_PROMPT = """Napisz notatkę o tej stronie dla programisty, który ma zdecydować, czy pisać do właściciela z propozycją poprawek, i co mu powiedzieć. Pisz jak konsultant po obejrzeniu strony, nie jak formularz. Wzór stylu, tak ma to brzmieć:

--- WZÓR (inna firma) ---
Werdykt: dobra, świeżo zrobiona strona, prawdopodobnie z tego roku. 8 na 10 na tle branży. Jedyna realna luka: brak rezerwacji online i kalendarza dostępności, więc każda rezerwacja to mail albo telefon, a w sezonie część gości nie czeka na odpowiedź.

Strona główna to rozdzielnia z dwoma przyciskami, Restauracja i Pensjonat, 39 słów. Właściwa treść jest o klik dalej: 5 tysięcy słów, cennik sezonowy z datami do 2027, telefon klikalny w nagłówku, formularz z polami od-do. Gość załatwia pokoje w jeden klik, cenę na tej samej stronie, termin przez formularz. Rezerwacji online nie ma: "Zapytaj o termin" prowadzi do formularza, nie do kalendarza.

Wizualnie: spójna paleta ciemnej zieleni i złota, jeden krój, dobre zdjęcia, oddech. Jedyna uwaga: nagłówek na zdjęciu ma miejscami słaby kontrast na jasnych fragmentach tła.

Najcenniejsza zmiana: silnik rezerwacji z kalendarzem. Analityki też nie ma, ale to drobiazg.
--- KONIEC WZORU ---

Zasady:
- Gdy w faktach stoi "STRONA JEST USZKODZONA", to jest cała notatka. Napisz, że strona w tej chwili nie wyświetla się poprawnie, bo nie wczytuje się jej plik ze stylami (podaj nazwę pliku i której podstrony dotyczy), i że gość widzi rozsypany układ zamiast projektu. Nie oceniaj wtedy kolorów, typografii, zdjęć ani układu, bo oceniasz ruiny, a nie projekt. W OCENA daj "werdykt": "napisz", historię o awarii, a w polach design i mobile oceń to, co gość faktycznie widzi teraz.
- Zacznij od werdyktu w jednym akapicie: ocena 1-10 na tle dobrych stron tej branży w 2026 roku, rok, z którego strona wygląda, i jedna dominująca historia, czyli to, co naprawdę kosztuje firmę klientów albo powód, dla którego nie ma czego poprawiać.
- Potem 2 do 4 krótkich akapitów bez nagłówków i bez wypunktowań: pierwsze wrażenie, praca gościa (co da się załatwić, za ile klików i dokąd prowadzi główny przycisk), jakość wizualna, fakty techniczne, tylko te, które mają znaczenie. Każde spostrzeżenie z dowodem, gdzie to widać na ekranie, tak żeby właściciel odnalazł to w dziesięć sekund.
- Do notatki trafia tylko to, co dziennik potwierdził narzędziem albo co stoi w faktach. Podejrzenie ze zrzutu, które sprawdzenie odrzuciło, pomijasz. Liczby (waga, ekrany, kontrast, słowa) przytaczasz z faktów i wyników sprawdzeń, nie z oka.
- Jeśli coś jest dobre, napisz to wprost. Jeśli strona jest dobra, powiedz, że nie ma sensu pisać z propozycją poprawek, albo że jedyny sensowny temat to X.
- Nie oceniaj po rozdzielni, jeśli właściwa treść jest na podstronie. Nie zgaduj klikalności ze zrzutu, klikalność jest w faktach.
- Bez emoji, bez słów "brzydka", "amatorska", "katastrofa". Najwyżej 350 słów.
- Bez myślników i półpauz (znaki — i –). Zamiast nich przecinek, dwukropek, nawias albo osobne zdanie. Dywiz w słowach jest w porządku.
- Czas ładowania i wagę podawaj tak, jak stoi w pomiarze. Nie zaokrąglaj w górę do "minuty" ani nie dopisuj własnych szacunków.
- Jeśli dziennik wymienia literówki albo zdjęcia stockowe, wspomnij o nich jednym zdaniem, bo właściciel od razu je rozpozna.
- Ostatnia linia notatki, dokładnie w tym formacie i w jednej linii:
OCENA: {"pierwsze_wrazenie": 1-10, "rok_wygladu": "RRRR", "werdykt": "napisz" albo "pomin", "historia": "jedno zdanie", "wlasciwa_strona": "url strony z trescia", "design": 1-10, "mobile": 1-10, "seo": 1-10, "cta": 1-10}"""


def _screenshot_block(state, browser, screen_number, label):
    if state["shots"] >= MAX_SCREENSHOTS:
        return [{"type": "text", "text": "Limit zrzutów wyczerpany. Dalej pracuj na faktach i sprawdzeniach."}]
    state["shots"] += 1
    png, number, total, position = browser.screenshot_screen(screen_number)
    if browser.width == 390:
        jpg = _jpeg(png, 390)
    else:
        jpg = _jpeg(png, 1000)
    caption = f"{label}: {browser.url}, {browser.width}px, ekran {number} z {total} (pozycja {position} px)"
    block = _image_block(jpg)
    state["images"].append((f"{browser.url} ekran {number}", browser.width, block))
    return [{"type": "text", "text": caption}, block]


def _open_page(state, url):
    if state["pages"] >= MAX_PAGES:
        return [{"type": "text", "text": "Limit otwieranych stron wyczerpany. Oceniaj na podstawie tego, co masz."}]
    state["pages"] += 1
    facts, data, links = _page_facts(url)
    if not state.get("primary_data"):
        state["primary_data"] = data
    browser = state.get("browser")
    if browser is None:
        state["facts"][url] = facts
        return [{"type": "text", "text": facts + "\n\nPrzeglądarka nie działa, więc bez zrzutów i pomiarów."}]
    try:
        browser.set_width(DESKTOP_WIDTH)
        browser.goto(url)
        page_html = browser.page.content()
        measurements = _browser_measurements(browser, page_html)
    except Exception as error:
        state["facts"][url] = facts
        return [{"type": "text", "text": facts + f"\n\nZrzut nie wyszedł: {str(error)[:120]}"}]
    state["facts"][url] = facts + "\n" + measurements
    state["log"].append("pomiar: " + measurements.replace("\n", " ")[:300])
    return [{"type": "text", "text": facts + "\n\n" + measurements}] + _screenshot_block(state, browser, 1, "Pierwszy ekran")


def _scroll_to_screens(state, screen_numbers, width):
    browser = state.get("browser")
    if browser is None or not browser.has_page:
        return [{"type": "text", "text": "Najpierw otwórz stronę narzędziem otworz_strone."}]
    browser.set_width(width)
    content = []
    for screen_number in screen_numbers[:MAX_SCREENS_PER_SCROLL]:
        content.extend(_screenshot_block(state, browser, screen_number, "Zrzut"))
    return content


def _destination_kind(current_url, destination):
    same_host = (urlparse(current_url).hostname or "") == (urlparse(destination).hostname or "")
    if same_host:
        return "podstronę"
    return "osobną poddomenę firmy (inna strona, bez menu i treści strony głównej)"


def _click(state, text):
    browser = state.get("browser")
    if browser is None or not browser.has_page:
        return [{"type": "text", "text": "Najpierw otwórz stronę narzędziem otworz_strone."}]
    locator = browser.find_clickable(text)
    if locator is None:
        return [{"type": "text", "text": f"Nie widzę linku ani przycisku z tekstem \"{text}\". Sprawdź zweryfikuj przyciski, żeby zobaczyć dokładne etykiety."}]
    href = locator.get_attribute("href") or ""
    target = locator.get_attribute("target") or ""
    if href and not href.startswith(("#", "javascript:")):
        if href.startswith("tel:"):
            return [{"type": "text", "text": f"\"{text}\" to klikalny telefon: {href[4:]}."}]
        if href.startswith("mailto:"):
            return [{"type": "text", "text": f"\"{text}\" to klikalny e-mail: {href[7:]}."}]
        destination = _absolute(browser.url, href)
        if not _url_allowed(destination, state["base_url"]):
            return [{"type": "text", "text": f"\"{text}\" prowadzi POZA domenę firmy: {destination}. Gość ląduje na obcej stronie{' w nowej karcie' if target == '_blank' else ''}; nie wchodzę tam."}]
        return [{"type": "text", "text": f"\"{text}\" prowadzi na {_destination_kind(browser.url, destination)} {destination}{' w nowej karcie' if target == '_blank' else ''}."}] + _open_page(state, destination)
    before_url = browser.url
    before_y = int(browser.page.evaluate("window.scrollY"))
    try:
        locator.click(timeout=5000)
        browser.page.wait_for_timeout(1500)
    except Exception as error:
        return [{"type": "text", "text": f"Kliknięcie nie wyszło: {str(error)[:120]}"}]
    after_url = browser.url
    if after_url.split("#")[0] != before_url.split("#")[0]:
        if not _url_allowed(after_url, state["base_url"]):
            browser.page.go_back()
            return [{"type": "text", "text": f"\"{text}\" przeniosło POZA domenę firmy: {after_url}. Wróciłem."}]
        browser.settle()
        return [{"type": "text", "text": f"\"{text}\" przeniosło na {after_url}."}] + _open_page(state, after_url)
    after_y = int(browser.page.evaluate("window.scrollY"))
    if abs(after_y - before_y) > 50:
        screen_number = after_y // browser.viewport_height + 1
        return [{"type": "text", "text": f"\"{text}\" przewinęło stronę do pozycji {after_y} px (ekran {screen_number})."}] + _screenshot_block(state, browser, screen_number, "Po kliknięciu")
    return [{"type": "text", "text": f"\"{text}\" nic widocznego nie zrobiło: ten sam adres, brak przewinięcia. Możliwe menu rozwijane albo skrypt. Oto ekran po kliknięciu."}] + _screenshot_block(state, browser, before_y // browser.viewport_height + 1, "Po kliknięciu")


def _verify(state, what):
    browser = state.get("browser")
    if browser is None or not browser.has_page:
        if what == "kod" and state.get("last_url"):
            report = _code_facts(state["last_url"])
            state["checks"].append(f"[kod] {state['last_url']}\n{report}")
            return [{"type": "text", "text": report}]
        return [{"type": "text", "text": "Najpierw otwórz stronę narzędziem otworz_strone."}]
    try:
        if what == "kontrast":
            report = _contrast_report(browser)
        elif what == "przyciski":
            report = _clickables_report(browser)
        elif what == "kod":
            report = _code_facts(browser.url)
        elif what == "telefon":
            report = _mobile_report(browser)
        elif what == "zasoby":
            report = _broken_assets_line(browser)
        elif what == "tekst":
            report = "Widoczny tekst strony:\n" + browser.visible_text()
        else:
            report = "Nieznane sprawdzenie."
    except Exception as error:
        report = f"Sprawdzenie nie wyszło: {str(error)[:120]}"
    state["checks"].append(f"[{what}] {browser.url}\n{report}")
    return [{"type": "text", "text": report}]


def _tool_result_content(name, args, state):
    if name == "otworz_strone":
        url = args.get("url", "")
        if not _url_allowed(url, state["base_url"]):
            return [{"type": "text", "text": "Ten adres jest poza domeną audytowanej firmy, pomijam. Oglądaj tylko strony w tej domenie."}]
        state["last_url"] = url
        return _open_page(state, url)
    if name == "przewin":
        screen_numbers = [int(number) for number in (args.get("ekrany") or [1])]
        return _scroll_to_screens(state, screen_numbers, int(args.get("szerokosc", DESKTOP_WIDTH)))
    if name == "kliknij":
        return _click(state, str(args.get("tekst", "")).strip())
    if name == "zweryfikuj":
        return _verify(state, args.get("co", ""))
    return [{"type": "text", "text": "Nieznane narzędzie."}]


def _result_summary(content):
    texts = [block["text"] for block in content if block.get("type") == "text"]
    return " ".join(texts).replace("\n", " ")[:300]


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


def _start_browser(base_url, log):
    try:
        return _Browser(base_url)
    except Exception as error:
        log.append(f"przeglądarka nie wystartowała: {str(error)[:120]}")
        return None


def explore(lead):
    """Etap 1: model oglada strone narzedziami w jednej sesji przegladarki i prowadzi dziennik."""
    client = _client()
    state = {"pages": 0, "shots": 0, "facts": {}, "checks": [], "images": [], "log": [], "summary": "",
             "base_url": lead.get("website_url", ""), "last_url": ""}
    state["browser"] = _start_browser(state["base_url"], state["log"])
    try:
        _explore_with_tools(client, lead, state)
    finally:
        if state["browser"]:
            try:
                state["browser"].close()
            except Exception:
                pass
        state["browser"] = None
    return state


def _explore_with_tools(client, lead, state):
    messages = [{"role": "user", "content": [{"type": "text", "text":
        f"Firma: {lead.get('business_name', '')}\nTyp biznesu: {lead.get('business_type', '')}\nMiasto: {lead.get('city', '')}\nStrona: {lead.get('website_url', '')}\n\nZacznij od otwarcia strony głównej."}]}]
    system = SYSTEM_STEPS.format(max_steps=MAX_TOOL_ROUNDS)
    for _ in range(MAX_TOOL_ROUNDS + 1):
        message = client.messages.create(
            model=STEP_MODEL,
            max_tokens=4000,
            system=system,
            tools=TOOLS,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": STEP_EFFORT},
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
            content = _tool_result_content(call.name, args, state)
            if call.name != "otworz_strone":
                state["log"].append("wynik: " + _result_summary(content))
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
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


def _choose_memo_images(images):
    if len(images) <= 3:
        return images
    return images[:1] + images[-2:]


def write_memo(lead, state):
    """Etap 2: Opus pisze notatke na podstawie faktow, sprawdzen, dziennika i 2-3 obrazow."""
    client = _client()
    content = [{"type": "text", "text": f"Firma: {lead.get('business_name', '')}, typ: {lead.get('business_type', '')}, miasto: {lead.get('city', '')}, strona: {lead.get('website_url', '')}\n\n=== FAKTY ZE STRON (z narzędzi) ===\n" + "\n\n".join(state["facts"].values())}]
    if state.get("checks"):
        content.append({"type": "text", "text": "=== WYNIKI SPRAWDZEŃ (twarde pomiary z przeglądarki i kodu) ===\n" + "\n\n".join(state["checks"])})
    content.append({"type": "text", "text": "=== DZIENNIK OGLĄDANIA (co sprawdzał model eksplorujący i co ustalił) ===\n" + "\n".join(state["log"]) + "\n\nPodsumowanie eksploracji: " + state["summary"]})
    for label, width, block in _choose_memo_images(state["images"]):
        content.append({"type": "text", "text": f"Zrzut: {label}, {width}px"})
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
        "primary_url": (scores.get("wlasciwa_strona") if _url_allowed(str(scores.get("wlasciwa_strona") or ""), lead.get("website_url", "")) else None) or lead.get("website_url", ""),
        "website_data": state.get("primary_data") or {},
        "log": log,
    }
