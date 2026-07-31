# amzn-rss

Generate an RSS feed from Amazon search-result pages with one request per enabled
search.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python main.py
```

By default this reads `config.json`, writes `state.json`, and writes `feed.xml`.
You can override those paths:

```bash
.venv/bin/python main.py --config config.json --state state.json --feed feed.xml
```

`state.json` updates after each successful run. `feed.xml` is only rewritten
when RSS-visible content changes, such as item links, prices, titles, GUIDs, or
the product list.

## Config

```json
{
  "include_price_changes": false,
  "link_cdata": false,
  "link_style": "canonical",
  "max_items_per_search": 20,
  "searches": [
    {
      "id": "gel-nimbus-x-wide-25.5",
      "title": "Gel Nimbus",
      "enabled": true,
      "url": "https://www.amazon.com/s?k=gel+nimbus",
      "include_words": [],
      "exclude_words": [],
      "min_price": null,
      "max_price": null,
      "include_price_changes": true,
      "link_cdata": true,
      "link_style": "selected_params",
      "max_items_per_search": 10
    }
  ]
}
```

Global options are defaults. Each search can override `include_price_changes`,
`link_cdata`, `link_style`, and `max_items_per_search`.

Config options:

- `include_price_changes`: controls whether price changes are shown in the feed.
  - `false`: do not mark price changes in feed items.
  - `true`: compare against `state.json` and show price drops/increases in item
    descriptions.
- `link_cdata`: controls how RSS item links are serialized.
  - `false`: write RSS item links as normal XML text.
  - `true`: wrap RSS item links in CDATA.
- `link_style`: controls which Amazon URL is used in RSS item links.
  - `canonical`: use clean `https://www.amazon.com/dp/{asin}` links.
  - `selected_params`: use `https://www.amazon.com/dp/{asin}` and keep only
    `sr`, `th`, and `psc` query params from the Amazon result URL.
  - `full_result`: use Amazon's full search-result URL, including query params.
- `max_items_per_search`: maximum number of parsed search-result cards to keep
  from one fetched page.
- `searches`: list of Amazon search URLs to fetch. Entries with
  `"enabled": false` are skipped.
- `id`: stable identifier for one search, used in state keys and RSS GUIDs.
- `title`: human-friendly label for one search, shown in item descriptions.
- `enabled`: controls whether a search entry runs.
- `url`: Amazon search-result URL to fetch.
- Product images: read from the Amazon search-result card and included in RSS
  item descriptions when available.
- `include_words` / `exclude_words`: title-only filters. The script does not
  open individual product pages.
- `min_price` / `max_price`: optional search-result price filters. If either is
  set and a result has no parsed price, that result is omitted.

Filters only inspect Amazon search-result cards. The script does not open
individual product pages.

## Test

```bash
.venv/bin/python -m unittest discover -v
```
