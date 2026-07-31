from urllib.parse import parse_qs, urlencode, urlparse


def simplify_amazon_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # Keep only the useful Amazon search parameters.
    allowed_params = ("k", "rh")
    clean_params = {
        key: params[key][0]
        for key in allowed_params
        if key in params
    }

    short_url = f"https://www.amazon.com/s?{urlencode(clean_params)}"

    search_term = clean_params.get("k", "Amazon Search")
    title = search_term.replace("+", " ").replace("-", " ").title()

    return short_url, title


url = "https://www.amazon.com/s?k=gel+nimbus&rh=p_n_pt_nav_size_men_shoe%3A1285097011%2Cp_n_pt_nav_size_men_shoe_width%3A1285105011&dc&crid=1SMOLOFXGC198&qid=1785520862&rnid=121075130011&sprefix=gel+nimbu%2Caps%2C179&ref=sr_nr_p_n_g-101015233022111_1&ds=v1%3A1JiTdIbUP3S0TSIwsrDkuo8yO977AbmSoKSv5UuKNm0"
short_url, title = simplify_amazon_url(url)

print("URL:", short_url)
print("Potential title:", title)