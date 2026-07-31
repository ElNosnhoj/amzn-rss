from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import format_datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_STATE_FILE = "state.json"
DEFAULT_FEED_FILE = "feed.xml"
DEFAULT_MAX_ITEMS_PER_SEARCH = 20
DEFAULT_INCLUDE_PRICE_CHANGES = False
DEFAULT_LINK_STYLE = "canonical"
DEFAULT_LINK_CDATA = False
SELECTED_LINK_PARAMS = ("sr", "th", "psc")
VALID_LINK_STYLES = {"canonical", "full_result", "selected_params"}

REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass(frozen=True)
class SearchConfig:
    id: str
    title: str
    url: str
    enabled: bool
    include_words: list[str]
    exclude_words: list[str]
    min_price: Decimal | None
    max_price: Decimal | None
    include_price_changes: bool
    max_items_per_search: int
    link_style: str
    link_cdata: bool


@dataclass(frozen=True)
class Product:
    search_id: str
    search_title: str
    asin: str
    title: str
    url: str
    result_url: str
    image_url: str | None
    price: Decimal | None
    position: int
    link_cdata: bool

    @property
    def state_key(self) -> str:
        return f"{self.search_id}:{self.asin}"


@dataclass(frozen=True)
class FeedProduct:
    product: Product
    previous_price: Decimal | None = None

    @property
    def price_change(self) -> str | None:
        if self.previous_price is None or self.product.price is None:
            return None
        if self.product.price < self.previous_price:
            return "drop"
        if self.product.price > self.previous_price:
            return "increase"
        return None

    @property
    def guid(self) -> str:
        change = self.price_change
        if change:
            old_price = price_to_state(self.previous_price)
            new_price = price_to_state(self.product.price)
            return (
                f"amazon-rss:{self.product.search_id}:{self.product.asin}:"
                f"price:{old_price}->{new_price}"
            )
        return f"amazon-rss:{self.product.search_id}:{self.product.asin}"


def load_config(path: Path) -> list[SearchConfig]:
    with path.open("r", encoding="utf-8") as file:
        raw_config = json.load(file)

    global_include_price_changes = bool(
        raw_config.get("include_price_changes", DEFAULT_INCLUDE_PRICE_CHANGES)
    )
    global_max_items = int(
        raw_config.get("max_items_per_search", DEFAULT_MAX_ITEMS_PER_SEARCH)
    )
    global_link_style = parse_link_style(raw_config.get("link_style", DEFAULT_LINK_STYLE))
    global_link_cdata = bool(raw_config.get("link_cdata", DEFAULT_LINK_CDATA))

    searches = []
    for index, raw_search in enumerate(raw_config.get("searches", []), start=1):
        search_id = str(raw_search.get("id") or f"search-{index}")
        title = str(raw_search.get("title") or search_id)
        url = str(raw_search.get("url") or "")
        if not url:
            raise ValueError(f"Search {search_id!r} is missing a url")

        searches.append(
            SearchConfig(
                id=search_id,
                title=title,
                url=url,
                enabled=bool(raw_search.get("enabled", True)),
                include_words=list(raw_search.get("include_words") or []),
                exclude_words=list(raw_search.get("exclude_words") or []),
                min_price=parse_config_price(raw_search.get("min_price")),
                max_price=parse_config_price(raw_search.get("max_price")),
                include_price_changes=bool(
                    raw_search.get(
                        "include_price_changes", global_include_price_changes
                    )
                ),
                max_items_per_search=int(
                    raw_search.get("max_items_per_search", global_max_items)
                ),
                link_style=parse_link_style(
                    raw_search.get("link_style", global_link_style)
                ),
                link_cdata=bool(raw_search.get("link_cdata", global_link_cdata)),
            )
        )

    return searches


def parse_config_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def parse_link_style(value: Any) -> str:
    link_style = str(value or DEFAULT_LINK_STYLE).strip().casefold()
    if link_style not in VALID_LINK_STYLES:
        valid_values = ", ".join(sorted(VALID_LINK_STYLES))
        raise ValueError(
            f"Invalid link_style {value!r}; expected one of: {valid_values}"
        )
    return link_style


def fetch_html(url: str) -> str:
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.text


def parse_search_results(html: str, search: SearchConfig) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[Product] = []
    seen_asins: set[str] = set()

    for card in soup.select("[data-component-type='s-search-result']"):
        asin = (card.get("data-asin") or "").strip()
        if is_sponsored_result(card):
            continue

        title, result_url = extract_title_and_link(card)
        if not asin:
            asin = extract_asin(result_url)
        if asin in seen_asins:
            continue
        if not asin or not title or not result_url:
            continue

        url = build_product_url(asin, result_url, search.link_style)
        price = parse_price_from_card(card)
        image_url = extract_image_url(card)
        products.append(
            Product(
                search_id=search.id,
                search_title=search.title,
                asin=asin,
                title=title,
                url=url,
                result_url=result_url,
                image_url=image_url,
                price=price,
                position=len(products) + 1,
                link_cdata=search.link_cdata,
            )
        )
        seen_asins.add(asin)

        if len(products) >= search.max_items_per_search:
            break

    return products


def is_sponsored_result(card: Any) -> bool:
    for selector in (
        "[aria-label='Sponsored']",
        "[aria-label^='Sponsored']",
        ".s-sponsored-label-info-icon",
        ".puis-sponsored-label-text",
    ):
        if card.select_one(selector):
            return True

    return card.get_text(" ", strip=True).casefold().startswith("sponsored ")


def extract_title_and_link(card: Any) -> tuple[str | None, str | None]:
    for selector in (
        "h2 a.a-link-normal[href]",
        "h2 a[href]",
        "a.a-link-normal.s-line-clamp-2[href]",
        "a.a-link-normal.s-line-clamp-4[href]",
        "a.a-link-normal[href*='/dp/']",
        "a.a-link-normal[href*='/gp/product/']",
    ):
        link_node = card.select_one(selector)
        if not link_node:
            continue

        href = str(link_node.get("href") or "")
        title_node = link_node.select_one("span")
        title = (
            title_node.get_text(" ", strip=True)
            if title_node
            else link_node.get_text(" ", strip=True)
        )
        title = collapse_space(title)

        if href and title:
            return title, normalize_amazon_url(href)

    return None, None


def looks_like_amazon_block(html: str) -> bool:
    lowered = html.casefold()
    return any(
        marker in lowered
        for marker in (
            "captcha",
            "robot check",
            "enter the characters you see below",
            "type the characters you see in this image",
            "sorry, we just need to make sure you're not a robot",
            "automated access to amazon data",
            "api-services-support@amazon.com",
            "/errors/validatecaptcha",
            "bm-verify",
            "/_sec/verify",
            "provider=interstitial",
            "triggerinterstitialchallenge",
        )
    )


def collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_amazon_url(href: str) -> str:
    return urljoin("https://www.amazon.com", href)


def build_product_url(asin: str, result_url: str, link_style: str) -> str:
    if link_style == "full_result":
        return result_url
    if link_style == "selected_params":
        return build_selected_params_url(asin, result_url)
    return f"https://www.amazon.com/dp/{asin}"


def build_selected_params_url(asin: str, result_url: str) -> str:
    parsed = urlsplit(result_url)
    selected_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key in SELECTED_LINK_PARAMS
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/dp/{asin}",
            urlencode(selected_params),
            "",
        )
    )


def extract_asin(url: str) -> str:
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url)
    return match.group(1) if match else ""


def extract_image_url(card: Any) -> str | None:
    image = card.select_one("img.s-image")
    if not image:
        return None

    src = str(image.get("src") or "").strip()
    if src:
        return src

    srcset = str(image.get("srcset") or "").strip()
    if not srcset:
        return None

    first_candidate = srcset.split(",", maxsplit=1)[0].strip()
    return first_candidate.split(" ", maxsplit=1)[0] or None


def parse_price_from_card(card: Any) -> Decimal | None:
    price_node = card.select_one(".a-price .a-offscreen")
    if price_node:
        return parse_price_text(price_node.get_text(strip=True))

    whole = card.select_one(".a-price-whole")
    if not whole:
        return None

    fraction = card.select_one(".a-price-fraction")
    whole_text = whole.get_text(strip=True).rstrip(".")
    fraction_text = fraction.get_text(strip=True) if fraction else "00"
    return parse_price_text(f"{whole_text}.{fraction_text}")


def parse_price_text(raw_price: str) -> Decimal | None:
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]{2})?)", raw_price)
    if not match:
        return None

    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def title_matches(product: Product, search: SearchConfig) -> bool:
    title = product.title.casefold()
    include_words = [word.casefold() for word in search.include_words if word]
    exclude_words = [word.casefold() for word in search.exclude_words if word]

    if include_words and not all(word in title for word in include_words):
        return False
    if exclude_words and any(word in title for word in exclude_words):
        return False
    return True


def price_matches(product: Product, search: SearchConfig) -> bool:
    has_price_filter = search.min_price is not None or search.max_price is not None
    if has_price_filter and product.price is None:
        return False
    if search.min_price is not None and product.price is not None:
        if product.price < search.min_price:
            return False
    if search.max_price is not None and product.price is not None:
        if product.price > search.max_price:
            return False
    return True


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": {}}
    with path.open("r", encoding="utf-8") as file:
        state = json.load(file)
    if not isinstance(state.get("items"), dict):
        state["items"] = {}
    return state


def build_feed_products(
    products_by_search: list[tuple[SearchConfig, list[Product]]],
    previous_state: dict[str, Any],
) -> tuple[list[FeedProduct], dict[str, Any]]:
    previous_items = previous_state.get("items", {})
    feed_products: list[FeedProduct] = []
    next_items: dict[str, dict[str, Any]] = {}

    for search, products in products_by_search:
        title_filtered = [product for product in products if title_matches(product, search)]

        for product in title_filtered:
            next_items[product.state_key] = product_to_state(product)

        for product in title_filtered:
            if not price_matches(product, search):
                continue

            previous_price = None
            previous_item = previous_items.get(product.state_key, {})
            if search.include_price_changes:
                previous_price = parse_config_price(previous_item.get("price"))

            feed_products.append(
                FeedProduct(product=product, previous_price=previous_price)
            )

    next_state = {
        "generated_at": utc_now_iso(),
        "items": next_items,
    }
    return feed_products, next_state


def product_to_state(product: Product) -> dict[str, Any]:
    return {
        "asin": product.asin,
        "search_id": product.search_id,
        "title": product.title,
        "url": product.url,
        "result_url": product.result_url,
        "image_url": product.image_url,
        "link_cdata": product.link_cdata,
        "price": price_to_state(product.price),
        "last_seen_at": utc_now_iso(),
    }


def price_to_state(price: Decimal | None) -> str | None:
    if price is None:
        return None
    return format(price, ".2f")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_rss(feed_products: list[FeedProduct], output_path: Path) -> bool:
    now = datetime.now(timezone.utc)
    cdata_values: dict[str, str] = {}
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Amazon Search RSS"
    ET.SubElement(channel, "link").text = "https://www.amazon.com/"
    ET.SubElement(channel, "description").text = (
        "Current matching Amazon search results."
    )
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)

    for feed_product in feed_products:
        product = feed_product.product
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = product.title
        link = ET.SubElement(item, "link")
        if product.link_cdata:
            placeholder = f"__AMZN_RSS_LINK_CDATA_{len(cdata_values)}__"
            cdata_values[placeholder] = product.url
            link.text = placeholder
        else:
            link.text = product.url
        ET.SubElement(item, "guid", isPermaLink="false").text = feed_product.guid
        ET.SubElement(item, "pubDate").text = format_datetime(now)
        ET.SubElement(item, "description").text = build_description(feed_product)

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    xml_text = serialize_xml(tree, cdata_values)
    if output_path.exists():
        existing_xml = output_path.read_text(encoding="utf-8")
        if normalize_feed_for_compare(existing_xml) == normalize_feed_for_compare(xml_text):
            return False

    write_text_atomically(xml_text, output_path)
    return True


def build_description(feed_product: FeedProduct) -> str:
    product = feed_product.product
    lines = [
        f"Search: {product.search_title}",
        f"ASIN: {product.asin}",
    ]
    if product.price is not None:
        lines.append(f"Price: ${price_to_state(product.price)}")
    if product.image_url:
        image_url = escape(product.image_url, quote=True)
        alt = escape(product.title, quote=True)
        lines.append(f'<img src="{image_url}" alt="{alt}" />')

    change = feed_product.price_change
    if change:
        old_price = price_to_state(feed_product.previous_price)
        new_price = price_to_state(product.price)
        label = "Price drop" if change == "drop" else "Price increase"
        lines.append(f"{label}: ${old_price} -> ${new_price}")

    return "<br>".join(lines)


def write_json_atomically(data: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)
        file.write("\n")
    os.replace(temp_path, output_path)


def serialize_xml(
    tree: ET.ElementTree,
    cdata_values: dict[str, str] | None = None,
) -> str:
    xml_text = ET.tostring(
        tree.getroot(),
        encoding="utf-8",
        xml_declaration=True,
    ).decode("utf-8")
    for placeholder, value in (cdata_values or {}).items():
        xml_text = xml_text.replace(placeholder, format_cdata(value))
    return xml_text


def normalize_feed_for_compare(xml_text: str) -> str:
    xml_text = re.sub(
        r"<lastBuildDate>.*?</lastBuildDate>",
        "<lastBuildDate></lastBuildDate>",
        xml_text,
        flags=re.DOTALL,
    )
    return re.sub(
        r"<pubDate>.*?</pubDate>",
        "<pubDate></pubDate>",
        xml_text,
        flags=re.DOTALL,
    )


def write_text_atomically(content: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        file.write(content)
    os.replace(temp_path, output_path)


def format_cdata(value: str) -> str:
    return f"<![CDATA[{value.replace(']]>', ']]]]><![CDATA[>')}]]>"


def run(config_path: Path, state_path: Path, feed_path: Path) -> None:
    searches = load_config(config_path)
    previous_state = load_state(state_path)
    products_by_search = []
    enabled_count = 0

    for search in searches:
        if not search.enabled:
            print(f"Skipping disabled search: {search.id}")
            continue
        enabled_count += 1
        html = fetch_html(search.url)
        products = parse_search_results(html, search)
        if not products and looks_like_amazon_block(html):
            raise RuntimeError(
                f"Amazon returned a robot/captcha page for search {search.id!r}. "
                "No product pages were requested, and state/feed were not updated."
            )
        print(f"Fetched {search.id}: parsed {len(products)} result card(s)")
        products_by_search.append((search, products))

    feed_products, next_state = build_feed_products(products_by_search, previous_state)
    feed_written = write_rss(feed_products, feed_path)
    write_json_atomically(next_state, state_path)
    feed_action = "Wrote" if feed_written else "Skipped"
    print(
        f"{feed_action} {feed_path} with {len(feed_products)} item(s); "
        f"wrote {state_path} with {len(next_state['items'])} tracked item(s) "
        f"from {enabled_count} enabled search(es)."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an RSS feed from configured Amazon search URLs."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--state", default=DEFAULT_STATE_FILE)
    parser.add_argument("--feed", default=DEFAULT_FEED_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(Path(args.config), Path(args.state), Path(args.feed))


if __name__ == "__main__":
    main()
