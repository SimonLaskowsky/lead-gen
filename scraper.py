import os
import re
import time
import requests
from bs4 import BeautifulSoup
from pathlib import Path

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


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

        # Get full text before decomposing tags (for email extraction)
        full_text = soup.get_text(separator=" ")

        # Decompose noise tags for clean AI text
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        clean_text = " ".join(soup.get_text().split())[:3000]

        # Checks
        has_ssl = url.startswith("https://")
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


def screenshot_website(url: str) -> dict[str, bytes | None]:
    """Take desktop + mobile screenshots using Playwright. Returns dict with 'desktop' and 'mobile'."""
    results = {"desktop": None, "mobile": None}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # Desktop screenshot
            desktop_page = browser.new_page(viewport={"width": 1280, "height": 900})
            desktop_page.goto(url, timeout=20000, wait_until="domcontentloaded")
            desktop_page.wait_for_timeout(1500)
            results["desktop"] = desktop_page.screenshot(type="png")

            # Mobile screenshot (iPhone 12 size)
            mobile_page = browser.new_page(viewport={"width": 390, "height": 844})
            mobile_page.goto(url, timeout=20000, wait_until="domcontentloaded")
            mobile_page.wait_for_timeout(1000)
            results["mobile"] = mobile_page.screenshot(type="png")

            browser.close()
    except Exception as e:
        print(f"Screenshot failed for {url}: {e}")
    return results


def extract_email_from_website(website_data: dict | None) -> str:
    if not website_data or website_data.get("error"):
        return ""
    text = website_data.get("full_text", "") + " " + website_data.get("text_preview", "")
    emails = re.findall(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", text)
    skip = ["noreply", "no-reply", "wordpress", "example", "woocommerce", "schema", "sentry", "@2x", "test@"]
    emails = [e for e in emails if not any(s in e.lower() for s in skip)]
    return emails[0] if emails else ""
