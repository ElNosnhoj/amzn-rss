from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET


PROJECT_DIR = Path(__file__).resolve().parent

CONFIG_PATH = PROJECT_DIR / "config.json"
STATE_PATH = PROJECT_DIR / "state.json"
FEED_PATH = PROJECT_DIR / "feed.xml"

AMAZON_BASE_URL = "https://www.amazon.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class SearchConfig:
    id: str
    title: str
    url: str


@dataclass
class Product:
    asin: str
    title: str
    url: str
    price: str | None
    image_url: str | None
    discovered_at: str
    search_id: str
    search_title: str


class ScraperError(Exception):
    """Raised when the scraper cannot safely continue."""


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError as exc:
        raise ScraperError(f"Invalid JSON in {path.name}: {exc}") from exc
    except OSError as exc:
        raise ScraperError(f"Could not read {path.name}: {exc}") from exc


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)

        os.replace(temporary_path, path)

    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def download_search_page(url: str, timeout_seconds: int) -> str:
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise ScraperError(f"Amazon request failed: {exc}") from exc

    if response.status_code in {429, 503}:
        raise ScraperError(
            f"Amazon returned HTTP {response.status_code}. "
            "The request was probably rate-limited or blocked."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise ScraperError(
            f"Amazon returned HTTP {response.status_code}."
        ) from exc

    return response.text


def looks_like_bot_check(html: str) -> bool:
    normalized = html.lower()

    known_markers = (
        "enter the characters you see below",
        "type the characters you see in this image",
        "sorry, we just need to make sure you're not a robot",
        "automated access to amazon data",
        "api-services-support@amazon.com",
        "/errors/validatecaptcha",
        "captcha",
    )

    return any(marker in normalized for marker in known_markers)


def is_sponsored_result(container: Any) -> bool:
    sponsored_selectors = (
        "[aria-label='Sponsored']",
        "[aria-label^='Sponsored']",
        ".s-sponsored-label-info-icon",
        ".puis-sponsored-label-text",
    )

    for selector in sponsored_selectors:
        if container.select_one(selector):
            return True

    visible_text = container.get_text(" ", strip=True).lower()
    return visible_text.startswith("sponsored ")


def extract_title_and_link(container: Any) -> tuple[str | None, str | None]:
    selectors = (
        "h2 a.a-link-normal",
        "a.a-link-normal.s-line-clamp-2",
        "a.a-link-normal.s-line-clamp-4",
    )

    link_element = None

    for selector in selectors:
        link_element = container.select_one(selector)

        if link_element:
            break

    if not link_element:
        return None, None

    href = link_element.get("href")

    if not href:
        return None, None

    title_element = link_element.select_one("span")

    if title_element:
        title = title_element.get_text(" ", strip=True)
    else:
        title = link_element.get_text(" ", strip=True)

    if not title:
        return None, None

    return title, urljoin(AMAZON_BASE_URL, href)


def extract_price(container: Any) -> str | None:
    offscreen_price = container.select_one(".a-price .a-offscreen")

    if offscreen_price:
        price = offscreen_price.get_text(" ", strip=True)
        return price or None

    whole = container.select_one(".a-price-whole")
    fraction = container.select_one(".a-price-fraction")
    symbol = container.select_one(".a-price-symbol")

    if not whole:
        return None

    symbol_text = symbol.get_text(strip=True) if symbol else "$"
    whole_text = whole.get_text(strip=True).rstrip(".")
    fraction_text = fraction.get_text(strip=True) if fraction else "00"

    return f"{symbol_text}{whole_text}.{fraction_text}"


def extract_image_url(container: Any) -> str | None:
    image = container.select_one("img.s-image")

    if not image:
        return None

    source = image.get("src")
    return source.strip() if source else None


def parse_products(
    html: str,
    search: SearchConfig,
) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")

    result_containers = soup.select(
        "[data-component-type='s-search-result'][data-asin]"
    )

    discovered_at = datetime.now(timezone.utc).isoformat()
    products: list[Product] = []
    seen_asins: set[str] = set()

    for container in result_containers:
        try:
            asin = str(container.get("data-asin", "")).strip()

            if not asin or asin in seen_asins:
                continue

            if is_sponsored_result(container):
                continue

            title, product_url = extract_title_and_link(container)

            if not title or not product_url:
                continue

            products.append(
                Product(
                    asin=asin,
                    title=title,
                    url=product_url,
                    price=extract_price(container),
                    image_url=extract_image_url(container),
                    discovered_at=discovered_at,
                    search_id=search.id,
                    search_title=search.title,
                )
            )

            seen_asins.add(asin)

        except (AttributeError, TypeError, ValueError):
            continue

    return products


def validate_config(
    config: Any,
) -> tuple[dict[str, Any], list[SearchConfig]]:
    if not isinstance(config, dict):
        raise ScraperError("config.json must contain a JSON object.")

    required_fields = (
        "searches",
        "feed_title",
        "feed_description",
        "feed_link",
    )

    missing = [
        field
        for field in required_fields
        if not config.get(field)
    ]

    if missing:
        raise ScraperError(
            "Missing config value(s): " + ", ".join(missing)
        )

    raw_searches = config["searches"]

    if not isinstance(raw_searches, list) or not raw_searches:
        raise ScraperError(
            "config.json 'searches' must be a non-empty list."
        )

    searches: list[SearchConfig] = []
    seen_ids: set[str] = set()

    for index, raw_search in enumerate(raw_searches, start=1):
        if not isinstance(raw_search, dict):
            raise ScraperError(
                f"Search entry {index} must be a JSON object."
            )

        search_id = str(raw_search.get("id", "")).strip()
        title = str(raw_search.get("title", "")).strip()
        url = str(raw_search.get("url", "")).strip()

        if not search_id or not title or not url:
            raise ScraperError(
                f"Search entry {index} must contain id, title, and url."
            )

        if search_id in seen_ids:
            raise ScraperError(
                f"Duplicate search id in config.json: {search_id}"
            )

        if not url.startswith(("https://www.amazon.com/", "http://www.amazon.com/")):
            raise ScraperError(
                f"Search '{search_id}' does not contain an amazon.com URL."
            )

        searches.append(
            SearchConfig(
                id=search_id,
                title=title,
                url=url,
            )
        )

        seen_ids.add(search_id)

    return config, searches


def load_state() -> dict[str, Any]:
    state = load_json(
        STATE_PATH,
        default={
            "updated_at": None,
            "searches": {},
        },
    )

    if not isinstance(state, dict):
        raise ScraperError("state.json must contain a JSON object.")

    searches = state.get("searches", {})

    if not isinstance(searches, dict):
        raise ScraperError(
            "state.json has an invalid 'searches' value."
        )

    return state


def get_seen_asins(
    state: dict[str, Any],
    search_id: str,
) -> set[str]:
    search_state = state.get("searches", {}).get(search_id, {})

    if not isinstance(search_state, dict):
        return set()

    products = search_state.get("products", {})

    if not isinstance(products, dict):
        return set()

    return set(products.keys())


def build_updated_state(
    previous_state: dict[str, Any],
    search_results: dict[str, list[Product]],
) -> dict[str, Any]:
    updated_searches = dict(previous_state.get("searches", {}))

    for search_id, products in search_results.items():
        search_title = products[0].search_title if products else search_id

        updated_searches[search_id] = {
            "title": search_title,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "products": {
                product.asin: asdict(product)
                for product in products
            },
        }

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "searches": updated_searches,
    }


def create_empty_feed(config: dict[str, Any]) -> ET.Element:
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
        },
    )

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = config["feed_title"]
    ET.SubElement(channel, "link").text = config["feed_link"]
    ET.SubElement(channel, "description").text = config["feed_description"]
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )

    return rss


def load_or_create_feed(config: dict[str, Any]) -> ET.Element:
    if not FEED_PATH.exists():
        return create_empty_feed(config)

    try:
        tree = ET.parse(FEED_PATH)
        root = tree.getroot()
    except ET.ParseError as exc:
        raise ScraperError(
            f"Existing feed.xml is malformed: {exc}"
        ) from exc
    except OSError as exc:
        raise ScraperError(
            f"Could not read feed.xml: {exc}"
        ) from exc

    if root.tag != "rss":
        raise ScraperError(
            "Existing feed.xml does not appear to be an RSS document."
        )

    if root.find("channel") is None:
        raise ScraperError(
            "Existing feed.xml does not contain an RSS channel."
        )

    return root


def update_channel_metadata(
    channel: ET.Element,
    config: dict[str, Any],
) -> None:
    metadata = {
        "title": config["feed_title"],
        "link": config["feed_link"],
        "description": config["feed_description"],
        "language": "en-us",
        "lastBuildDate": format_datetime(datetime.now(timezone.utc)),
    }

    for tag_name, value in metadata.items():
        element = channel.find(tag_name)

        if element is None:
            element = ET.Element(tag_name)
            channel.insert(0, element)

        element.text = value


def product_description(product: Product) -> str:
    lines = [
        f"<p><strong>Search:</strong> {product.search_title}</p>"
    ]

    if product.price:
        lines.append(
            f"<p><strong>Price:</strong> {product.price}</p>"
        )
    else:
        lines.append(
            "<p><strong>Price:</strong> Not available</p>"
        )

    if product.image_url:
        lines.append(
            f'<p><img src="{product.image_url}" '
            f'alt="{product.title}" /></p>'
        )

    lines.append(
        f'<p><a href="{product.url}">View on Amazon</a></p>'
    )

    return "".join(lines)


def create_feed_item(product: Product) -> ET.Element:
    item = ET.Element("item")

    ET.SubElement(item, "title").text = (
        f"[{product.search_title}] {product.title}"
    )

    ET.SubElement(item, "link").text = product.url

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "false"},
    )

    # The same ASIN can be new in two different searches.
    guid.text = f"{product.search_id}:{product.asin}"

    discovered_at = datetime.fromisoformat(product.discovered_at)

    ET.SubElement(item, "pubDate").text = format_datetime(
        discovered_at
    )

    ET.SubElement(item, "category").text = product.search_title
    ET.SubElement(item, "description").text = product_description(
        product
    )

    return item


def update_feed(
    config: dict[str, Any],
    new_products: list[Product],
) -> ET.Element:
    root = load_or_create_feed(config)
    channel = root.find("channel")

    if channel is None:
        raise ScraperError("RSS channel could not be found.")

    update_channel_metadata(channel, config)

    existing_guids = {
        guid.text
        for guid in channel.findall("./item/guid")
        if guid.text
    }

    insertion_index = 0

    for index, child in enumerate(list(channel)):
        if child.tag == "item":
            insertion_index = index
            break
    else:
        insertion_index = len(channel)

    for product in reversed(new_products):
        product_guid = f"{product.search_id}:{product.asin}"

        if product_guid in existing_guids:
            continue

        channel.insert(
            insertion_index,
            create_feed_item(product),
        )

        existing_guids.add(product_guid)

    max_entries = int(config.get("max_feed_entries", 200))
    items = channel.findall("item")

    for old_item in items[max_entries:]:
        channel.remove(old_item)

    return root


def serialize_xml(root: ET.Element) -> str:
    ET.indent(root, space="  ")

    xml_bytes = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )

    return xml_bytes.decode("utf-8") + "\n"


def main() -> int:
    try:
        raw_config = load_json(CONFIG_PATH, default={})
        config, searches = validate_config(raw_config)

        state = load_state()

        timeout_seconds = int(
            config.get("request_timeout_seconds", 30)
        )

        request_delay_seconds = float(
            config.get("request_delay_seconds", 2)
        )

        all_new_products: list[Product] = []
        search_results: dict[str, list[Product]] = {}

        for index, search in enumerate(searches, start=1):
            print(
                f"[{index}/{len(searches)}] "
                f"Downloading: {search.title}"
            )

            html = download_search_page(
                search.url,
                timeout_seconds,
            )

            if looks_like_bot_check(html):
                raise ScraperError(
                    f"Amazon returned a CAPTCHA or bot-check page "
                    f"for search '{search.title}'. "
                    "Existing state.json and feed.xml were not changed."
                )

            products = parse_products(html, search)

            if not products:
                raise ScraperError(
                    f"No valid products were found for search "
                    f"'{search.title}'. Amazon may have changed its "
                    "HTML or blocked the request. Existing files were "
                    "not changed."
                )

            seen_asins = get_seen_asins(state, search.id)

            new_products = [
                product
                for product in products
                if product.asin not in seen_asins
            ]

            search_results[search.id] = products
            all_new_products.extend(new_products)

            print(
                f"  Found {len(products)} products, "
                f"{len(new_products)} new."
            )

            if index < len(searches) and request_delay_seconds > 0:
                time.sleep(request_delay_seconds)

        updated_feed = update_feed(
            config,
            all_new_products,
        )

        updated_state = build_updated_state(
            state,
            search_results,
        )

        feed_xml = serialize_xml(updated_feed)

        state_json = json.dumps(
            updated_state,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"

        # All requests and parsing complete successfully before writes begin.
        atomic_write_text(FEED_PATH, feed_xml)
        atomic_write_text(STATE_PATH, state_json)

        print()
        print(f"Processed {len(searches)} searches.")
        print(f"Discovered {len(all_new_products)} new products.")
        print(f"Updated {STATE_PATH.name} and {FEED_PATH.name}.")

        for product in all_new_products:
            price = product.price or "price unavailable"

            print(
                f"  NEW [{product.search_title}]: "
                f"{product.title} — {price}"
            )

        return 0

    except ScraperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"ERROR: Could not write output files: {exc}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(
            f"ERROR: Invalid configuration value: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())