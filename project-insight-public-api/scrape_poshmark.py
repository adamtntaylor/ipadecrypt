#!/usr/bin/env python3
"""Build Project Insight's public catalog from public, unauthenticated Poshmark pages.

No login, cookies, private API, CAPTCHA bypass, or anti-bot evasion is used. If Poshmark
returns 403/429, the refresh stops and leaves the previous catalog intact.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "v1" / "listings"
IMG_DIR = ROOT / "images" / "poshmark"
BASE = "https://poshmark.com"
PUBLIC_IMAGE_BASE = "https://raw.githubusercontent.com/adamtntaylor/ipadecrypt/main/project-insight-public-api/images/poshmark"

CATEGORY_PAGES = [
    ("Bags", "https://poshmark.com/category/Women-Bags"),
    ("Shoes", "https://poshmark.com/category/Women-Shoes"),
    ("Clothing", "https://poshmark.com/category/Women-Tops"),
    ("Watches", "https://poshmark.com/category/Women-Accessories-Watches"),
    ("Tech", "https://poshmark.com/category/Electronics"),
    ("Accessories", "https://poshmark.com/category/Women-Accessories"),
]

MAX_TOTAL = int(os.environ.get("POSHMARK_MAX_LISTINGS", "48"))
MAX_PER_CATEGORY = max(3, MAX_TOTAL // len(CATEGORY_PAGES))
TIMEOUT = 20
HEADERS = {
    "User-Agent": "ProjectInsightPublicCatalog/1.1 (+https://github.com/adamtntaylor/ipadecrypt)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)


def clean_text(value, limit: int | None = None) -> str:
    text = html_unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def fetch(url: str, binary: bool = False):
    response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    if response.status_code in (403, 429):
        raise RuntimeError(f"Public refresh declined with HTTP {response.status_code}; not bypassing it.")
    response.raise_for_status()
    return response.content if binary else response.text


def listing_links(page_html: str) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    links: list[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href.startswith("/listing/"):
            continue
        full = urljoin(BASE, href.split("?")[0])
        if full not in seen:
            seen.add(full)
            links.append(full)
    if links:
        return links
    for match in re.findall(r"(?:https://poshmark\.com)?(/listing/[A-Za-z0-9_%\-]+)", page_html):
        full = urljoin(BASE, match)
        if full not in seen:
            seen.add(full)
            links.append(full)
    return links


def meta(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        node = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if node and node.get("content"):
            return clean_text(node["content"])
    return ""


def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_json(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_json(value)


def product_json(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for node in walk_json(data):
            kind = node.get("@type")
            if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
                return node
    return {}


def number(value, default=0) -> int:
    if isinstance(value, (int, float)):
        return int(round(value))
    if isinstance(value, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", value)
        if m:
            return int(round(float(m.group(0).replace(",", ""))))
    return default


def seller_from_data(product: dict, soup: BeautifulSoup) -> str:
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    seller = offers.get("seller") if isinstance(offers, dict) else None
    if isinstance(seller, dict) and seller.get("name"):
        return clean_text(seller["name"]).lstrip("@").strip()
    if isinstance(seller, str):
        return clean_text(seller).lstrip("@").strip()
    body = soup.get_text(" ", strip=True)
    m = re.search(r"@([A-Za-z0-9_]{2,30})", body)
    return m.group(1) if m else "Poshmark seller"


def brand_from_data(product: dict) -> str:
    brand = product.get("brand")
    if isinstance(brand, dict):
        return clean_text(brand.get("name") or "Unbranded")
    if isinstance(brand, str) and brand.strip():
        return clean_text(brand)
    return "Unbranded"


def condition_from_data(product: dict, soup: BeautifulSoup) -> str:
    offers = product.get("offers") or {}
    item_condition = product.get("itemCondition") or (offers.get("itemCondition") if isinstance(offers, dict) else "")
    raw = str(item_condition).lower()
    page_text = soup.get_text(" ", strip=True)
    if "newcondition" in raw or re.search(r"\bNew With Tags\b|\bNew Without Tags\b", page_text, flags=re.I):
        return "New"
    if re.search(r"\bLike New\b", page_text, flags=re.I):
        return "Excellent"
    if re.search(r"\bVery Good\b", page_text, flags=re.I):
        return "Very Good"
    if re.search(r"\bGood\b", page_text, flags=re.I):
        return "Good"
    if "used" in raw:
        return "Pre-Owned"
    return "Available"


def size_from_data(product: dict, soup: BeautifulSoup) -> str:
    for key in ("size", "itemSize"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = clean_text(value, 28)
            if cleaned:
                return cleaned

    text = soup.get_text(" ", strip=True)
    m = re.search(r"\bSize\s*:?\s*([^|•\n]{1,80})", text, flags=re.I)
    if not m:
        return "See listing"

    value = clean_text(m.group(1))
    value = re.split(
        r"\b(?:Buy Now|Make an Offer|Like and save|Pay in 4|Shipping/Discount|Add To Bundle|Seller Discount)\b",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    value = value.strip(" :-•")
    if not value:
        return "See listing"
    return value[:28]


def listing_id(url: str) -> str:
    slug = urlparse(url).path.rsplit("/", 1)[-1]
    m = re.search(r"-([0-9a-fA-F]{20,})$", slug)
    if m:
        return "posh-" + m.group(1).lower()
    return "posh-" + hashlib.sha1(url.encode()).hexdigest()[:20]


def download_cover(remote_url: str, item_id: str) -> str | None:
    if not remote_url.startswith("https://"):
        return None
    try:
        data = fetch(remote_url, binary=True)
    except Exception as exc:
        print(f"image skipped {item_id}: {exc}")
        return None
    if len(data) < 500 or len(data) > 8_000_000:
        return None

    if data.startswith(b"\x89PNG"):
        ext = ".png"
    elif data[:4] == b"RIFF" and b"WEBP" in data[:16]:
        ext = ".webp"
    else:
        ext = ".jpg"

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    path = IMG_DIR / f"{item_id}{ext}"
    path.write_bytes(data)
    return f"{PUBLIC_IMAGE_BASE}/{path.name}"


def parse_listing(url: str, category: str) -> dict | None:
    page_html = fetch(url)
    soup = BeautifulSoup(page_html, "html.parser")
    product = product_json(soup)

    title = clean_text(product.get("name") or meta(soup, "og:title", "twitter:title"), 180)
    description = clean_text(product.get("description") or meta(soup, "og:description", "description"), 1800)
    image = product.get("image") or meta(soup, "og:image", "twitter:image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl") or ""

    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = number(offers.get("price") if isinstance(offers, dict) else None)
    if not price:
        price = number(meta(soup, "product:price:amount"))
    if not price:
        m = re.search(r"\$([\d,]+)", soup.get_text(" ", strip=True))
        price = number(m.group(1)) if m else 0
    if not title or price <= 0:
        return None

    item_id = listing_id(url)
    hosted_image = download_cover(str(image), item_id) if image else None
    original_price = price
    text = soup.get_text(" ", strip=True)
    prices = [number(x) for x in re.findall(r"\$([\d,]+)", text)[:16]]
    larger = [p for p in prices if p > price and p < price * 20]
    if larger:
        original_price = min(larger)

    return {
        "id": item_id,
        "title": title,
        "brand": clean_text(brand_from_data(product), 80),
        "price": price,
        "originalPrice": original_price,
        "size": size_from_data(product, soup),
        "seller": clean_text(seller_from_data(product, soup), 40),
        "symbol": {"Bags": "bag", "Shoes": "shoe", "Clothing": "tshirt", "Watches": "watch.analog", "Tech": "laptopcomputer", "Accessories": "sparkles"}.get(category, "tag"),
        "category": category,
        "condition": condition_from_data(product, soup),
        "detail": description or "Open the original Poshmark listing for complete seller-provided details.",
        "sellerRating": 0.0,
        "sellerLastActive": "See Poshmark",
        "shippingPrice": 0,
        "listedDaysAgo": 0,
        "isAvailable": True,
        "imageURL": hosted_image,
        "sourceURL": url,
        "sourceName": "Poshmark",
    }


def prune_stale_images(listings: list[dict]) -> None:
    if not IMG_DIR.exists():
        return
    keep = set()
    for item in listings:
        image_url = item.get("imageURL") or ""
        if image_url:
            keep.add(Path(urlparse(image_url).path).name)
    for path in IMG_DIR.iterdir():
        if path.is_file() and path.name not in keep:
            path.unlink()
            print(f"removed stale cover {path.name}")


def main() -> int:
    candidates: list[tuple[str, str]] = []
    seen = set()
    for category, page in CATEGORY_PAGES:
        print(f"discovering {category}: {page}")
        page_html = fetch(page)
        links = listing_links(page_html)
        print(f"  found {len(links)} public listing links")
        for link in links[:MAX_PER_CATEGORY * 3]:
            if link not in seen:
                seen.add(link)
                candidates.append((category, link))
            if sum(1 for c, _ in candidates if c == category) >= MAX_PER_CATEGORY * 2:
                break
        time.sleep(0.35)

    listings = []
    per_category: dict[str, int] = {}
    for category, url in candidates:
        if len(listings) >= MAX_TOTAL:
            break
        if per_category.get(category, 0) >= MAX_PER_CATEGORY:
            continue
        try:
            item = parse_listing(url, category)
        except RuntimeError:
            raise
        except Exception as exc:
            print(f"listing skipped {url}: {exc}")
            continue
        if item:
            listings.append(item)
            per_category[category] = per_category.get(category, 0) + 1
            print(f"  + {item['title'][:70]} (${item['price']})")
        time.sleep(0.25)

    minimum_publishable = min(12, max(6, MAX_TOTAL // 4))
    if len(listings) < minimum_publishable:
        raise RuntimeError(
            f"Only parsed {len(listings)} listings; preserving the previous catalog instead of publishing a bad refresh."
        )

    payload = {
        "listings": listings,
        "nextCursor": None,
        "totalCount": len(listings),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceName": "Poshmark public catalog",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)
    prune_stale_images(listings)
    print(f"published {len(listings)} public Poshmark listings to {OUT}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"REFRESH FAILED SAFELY: {exc}", file=sys.stderr)
        raise
