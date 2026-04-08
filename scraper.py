import os
import re
import time
import requests
from bs4 import BeautifulSoup
from pathlib import Path

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

OUTSOURCED_PLATFORMS = {
    "booksy.com":       {"name": "Booksy",       "pitch": "płacą prowizję od każdej rezerwacji"},
    "fresha.com":       {"name": "Fresha",        "pitch": "płacą prowizję od każdej rezerwacji"},
    "treatwell.pl":     {"name": "Treatwell",     "pitch": "płacą prowizję od każdej rezerwacji"},
    "treatwell.com":    {"name": "Treatwell",     "pitch": "płacą prowizję od każdej rezerwacji"},
    "facebook.com":     {"name": "Facebook",      "pitch": "używają Facebooka zamiast własnej strony"},
    "sites.google.com": {"name": "Google Sites",  "pitch": "używają darmowej strony Google bez własnej domeny"},
    "linktr.ee":        {"name": "Linktree",      "pitch": "używają Linktree zamiast własnej strony"},
    "znanylekarz.pl":   {"name": "Znany Lekarz",  "pitch": "płacą prowizję od każdej wizyty"},
    "goldenline.pl":    {"name": "Goldenline",    "pitch": "mają tylko profil na platformie zamiast własnej strony"},
    "instagram.com":    {"name": "Instagram",     "pitch": "używają Instagrama zamiast własnej strony"},
}


def detect_outsourced_platform(url: str) -> dict | None:
    """Check if URL belongs to a known booking/social platform instead of own website."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().lstrip("www.")
        for platform_domain, info in OUTSOURCED_PLATFORMS.items():
            if domain == platform_domain or domain.endswith("." + platform_domain):
                return info
    except Exception:
        pass
    return None


def _attr(tag, key, default=""):
    """Safe attribute getter — handles BeautifulSoup tags with attrs=None."""
    if tag is None:
        return default
    try:
        val = tag.get(key, default)
        return val if val is not None else default
    except (AttributeError, TypeError):
        return default


def search_leads(business_type: str, city: str, max_results: int = 10) -> list[dict]:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_MAPS_API_KEY not set in .env")

    import googlemaps

    gmaps = googlemaps.Client(key=api_key)
    query = f"{business_type} {city}"
    leads = []
    next_page_token = None

    while len(leads) < max_results:
        kwargs = {"language": "pl"}
        if next_page_token:
            time.sleep(2)
            kwargs["page_token"] = next_page_token

        results = gmaps.places(query=query, **kwargs)

        for place in results.get("results", []):
            if len(leads) >= max_results:
                break
            try:
                details = gmaps.place(
                    place["place_id"],
                    fields=["name", "formatted_phone_number", "website", "formatted_address"],
                )["result"]
                leads.append(
                    {
                        "business_name": details.get("name", ""),
                        "phone": details.get("formatted_phone_number", ""),
                        "website_url": details.get("website", ""),
                        "address": details.get("formatted_address", ""),
                    }
                )
            except Exception as e:
                print(f"Error fetching place details: {e}")

        next_page_token = results.get("next_page_token")
        if not next_page_token:
            break

    return leads


def get_pagespeed_score(url: str) -> dict | None:
    """Fetch Google PageSpeed Insights mobile score (free, no API key needed)."""
    try:
        resp = requests.get(
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
            params={"url": url, "strategy": "mobile"},
            timeout=15,
        )
        data = resp.json()
        categories = data.get("lighthouseResult", {}).get("categories", {})
        audits = data.get("lighthouseResult", {}).get("audits", {})

        score = categories.get("performance", {}).get("score")
        fcp = audits.get("first-contentful-paint", {}).get("displayValue", "")
        tbt = audits.get("total-blocking-time", {}).get("displayValue", "")

        return {
            "performance_score": int(score * 100) if score is not None else None,
            "first_contentful_paint": fcp,
            "total_blocking_time": tbt,
        }
    except Exception:
        return None


def scrape_website(url: str) -> dict | None:
    if not url:
        return None
    # If the URL itself is a known platform, skip scraping entirely
    platform = detect_outsourced_platform(url)
    if platform:
        return {"outsourced_platform": platform["name"], "outsourced_pitch": platform["pitch"], "tech_stack": []}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        # Use raw bytes so BeautifulSoup reads encoding from <meta charset> tag
        # instead of relying on requests' often-wrong ISO-8859-1 default
        soup = BeautifulSoup(resp.content, "html.parser")

        # Extract metadata BEFORE decomposing anything
        title_tag = soup.find("title")
        meta_desc_tag = soup.find("meta", attrs={"name": "description"})
        viewport_tag = soup.find("meta", attrs={"name": "viewport"})
        og_image_tag = soup.find("meta", attrs={"property": "og:image"})

        # Read title text now before head gets decomposed
        title = ""
        try:
            if title_tag:
                title = title_tag.get_text().strip()
        except Exception:
            pass

        meta_description = _attr(meta_desc_tag, "content")

        page_source = resp.content.decode("utf-8", errors="replace")
        has_ua_code = bool(re.search(r"UA-\d{4,}-\d+", page_source))
        has_ga4 = bool(re.search(r"G-[A-Z0-9]{6,}", page_source)) or "gtag" in page_source
        # Fully dead = has UA but no GA4 at all
        has_dead_analytics = has_ua_code and not has_ga4
        # Has leftover legacy UA code even though GA4 exists
        has_legacy_ua = has_ua_code and has_ga4

        # Detect tech stack BEFORE decomposing script/style tags
        tech_stack = _detect_tech(page_source, soup)

        # Get full text before decomposing tags (for email extraction)
        full_text = soup.get_text(separator=" ")

        # Decompose noise tags for clean AI text
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        clean_text = " ".join(soup.get_text().split())[:3000]

        # Checks
        has_ssl = resp.url.startswith("https://")
        if not has_ssl:
            # Site may support HTTPS without forcing a redirect — probe directly
            try:
                https_url = "https://" + resp.url.split("://", 1)[1]
                test = requests.head(https_url, headers=HEADERS, timeout=5)
                has_ssl = test.status_code < 500
            except Exception:
                pass
        has_contact_form = bool(soup.find("form"))
        has_og_image = og_image_tag is not None

        # Table-based layout = likely outdated
        uses_tables = len(soup.find_all("table")) > 3

        # CTA detection (Polish + English)
        cta_words = ["kontakt", "zadzwoń", "zamów", "wyceń", "umów", "zapytaj",
                     "call", "contact", "book", "order", "get a quote"]
        has_cta = bool(
            soup.find(
                lambda t: t.name in ["a", "button"]
                and any(word in (t.get_text(strip=True) or "").lower() for word in cta_words)
            )
        )

        # Social media links
        social_domains = ["facebook.com", "instagram.com", "linkedin.com", "tiktok.com"]
        has_social = bool(
            soup.find("a", href=lambda h: h and any(s in h for s in social_domains))
        )

        images = soup.find_all("img")

        # Try PageSpeed (non-blocking — returns None if fails/slow)
        pagespeed = get_pagespeed_score(url)

        return {
            "title": title,
            "meta_description": meta_description,
            "has_mobile_viewport": bool(viewport_tag),
            "has_ssl": has_ssl,
            "has_contact_form": has_contact_form,
            "has_cta": has_cta,
            "has_social": has_social,
            "has_og_image": has_og_image,
            "uses_tables_layout": uses_tables,
            "has_dead_analytics": has_dead_analytics,
            "has_legacy_ua": has_legacy_ua,
            "tech_stack": tech_stack,
            "image_count": len(images),
            "pagespeed_score": pagespeed.get("performance_score") if pagespeed else None,
            "pagespeed_fcp": pagespeed.get("first_contentful_paint") if pagespeed else None,
            "text_preview": clean_text,
            "full_text": full_text[:5000],
            "status_code": resp.status_code,
        }
    except requests.exceptions.SSLError:
        return {"error": "SSL error", "has_ssl": False}
    except requests.exceptions.ConnectionError:
        return {"error": "Connection failed"}
    except requests.exceptions.Timeout:
        return {"error": "Timeout"}
    except requests.exceptions.HTTPError as e:
        return {"error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def _detect_tech(html: str, soup) -> list[str]:
    """Detect CMS/framework from raw HTML. Returns list of tech names."""
    tech = []

    # WordPress — most reliable signals
    if "wp-content/" in html or "wp-includes/" in html:
        tech.append("WordPress")
        if "elementor" in html.lower():
            tech.append("Elementor")
        elif "/divi/" in html or "et_pb_" in html:
            tech.append("Divi")

    # Website builders
    if "wixstatic.com" in html or "wix.com/_api" in html:
        tech.append("Wix")
    if "squarespace.com" in html or "squarespace-cdn.com" in html:
        tech.append("Squarespace")
    if "cdn.shopify.com" in html:
        tech.append("Shopify")
    if "webflow.com" in html:
        tech.append("Webflow")

    # JS frameworks (only if no builder detected yet)
    if not tech:
        if "__NEXT_DATA__" in html or "/_next/static/" in html:
            tech.append("Next.js")
        elif "__NUXT__" in html or "/_nuxt/" in html:
            tech.append("Nuxt.js")

    # Other CMS via generator meta tag
    if not tech:
        gen = soup.find("meta", attrs={"name": "generator"})
        if gen:
            content = (gen.get("content") or "").lower()
            if "joomla" in content:
                tech.append("Joomla")
            elif "drupal" in content:
                tech.append("Drupal")

    return tech


def screenshot_website(url: str) -> dict[str, bytes | None]:
    """Take desktop + mobile screenshots using Playwright. Returns dict with 'desktop' and 'mobile'."""
    results = {"desktop": None, "mobile": None}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # Desktop screenshot — use "load" + extra wait for JS-rendered sites
            desktop_page = browser.new_page(viewport={"width": 1280, "height": 900})
            desktop_page.goto(url, timeout=30000, wait_until="load")
            desktop_page.wait_for_timeout(3000)
            results["desktop"] = desktop_page.screenshot(type="png")

            # Mobile screenshot (iPhone 12 size)
            mobile_page = browser.new_page(viewport={"width": 390, "height": 844})
            mobile_page.goto(url, timeout=30000, wait_until="load")
            mobile_page.wait_for_timeout(2000)
            results["mobile"] = mobile_page.screenshot(type="png")

            browser.close()
    except Exception as e:
        print(f"Screenshot failed for {url}: {e}")
    return results


def screenshot_html(html: str, width: int = 1280) -> bytes | None:
    """Render an HTML string with Playwright and return a JPEG screenshot."""
    try:
        import tempfile, pathlib
        from playwright.sync_api import sync_playwright

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
            f.write(html)
            tmp_path = pathlib.Path(f.name)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.goto(f"file://{tmp_path}", wait_until="domcontentloaded")
                page.wait_for_timeout(500)
                # Screenshot full page height
                png = page.screenshot(type="png", full_page=True)
                browser.close()
        finally:
            tmp_path.unlink(missing_ok=True)

        # Convert to JPEG
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(png))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue()
        except ImportError:
            return png
    except Exception as e:
        print(f"screenshot_html failed: {e}")
        return None


def extract_email_from_website(website_data: dict | None) -> str:
    if not website_data or website_data.get("error"):
        return ""
    text = website_data.get("full_text", "") + " " + website_data.get("text_preview", "")
    emails = re.findall(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", text)
    skip = ["noreply", "no-reply", "wordpress", "example", "woocommerce", "schema", "sentry", "@2x", "test@"]
    emails = [e for e in emails if not any(s in e.lower() for s in skip)]
    return emails[0] if emails else ""
