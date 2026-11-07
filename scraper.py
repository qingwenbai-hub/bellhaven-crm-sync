"""Scrape every Bellhaven community from the public website using Scrapy.

Crawls the home page, /about and every page of /communities, follows every
/communities/<slug> link it finds anywhere on the site, and parses each
detail page into one record.

Usage:
    python scraper.py                 # writes data/locations.json
    python scraper.py -o other.json
"""
import argparse
import json
import os
import re

import scrapy
from scrapy.crawler import CrawlerProcess

BASE = "https://analyst-assessment-production.up.railway.app"


def clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


class BellhavenSpider(scrapy.Spider):
    name = "bellhaven"
    allowed_domains = ["analyst-assessment-production.up.railway.app"]
    start_urls = [f"{BASE}/", f"{BASE}/about", f"{BASE}/communities"]

    def parse(self, response):
        """Listing / generic page: follow detail links and pagination."""
        for href in response.css("a::attr(href)").getall():
            path = href.split("#")[0]
            if re.fullmatch(r"/communities/[^/?]+", path):
                yield response.follow(path, callback=self.parse_detail)
            elif path.startswith("/communities?page="):
                yield response.follow(path, callback=self.parse)

    def parse_detail(self, response):
        fields = {}
        for dt in response.css("dl.detail dt"):
            key = clean(dt.css("::text").get()).lower()
            dd = dt.xpath("following-sibling::dd[1]")
            fields[key] = dd

        addr_lines = [clean(t) for t in fields["address"].css("::text").getall() if clean(t)]
        street = addr_lines[0] if addr_lines else ""
        city = state = zip_code = ""
        if len(addr_lines) > 1:
            m = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", addr_lines[-1])
            if m:
                city, state, zip_code = m.groups()

        care = []
        if "care offerings" in fields:
            care = [clean(t) for t in fields["care offerings"].css(".badge::text").getall()]

        def text_of(key):
            return clean(" ".join(fields[key].css("::text").getall())) if key in fields else ""

        yield {
            "slug": response.url.rstrip("/").split("/")[-1],
            "url": response.url,
            "name": clean(response.css("h1::text").get()),
            "address": street,
            "city": city,
            "state": state,
            "zip": zip_code,
            "care_offerings": care,
            "administrator": text_of("administrator"),
            "phone": text_of("phone"),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="data/locations.json")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    tmp = args.output + ".tmp.jsonl"
    if os.path.exists(tmp):
        os.remove(tmp)

    process = CrawlerProcess(settings={
        "FEEDS": {tmp: {"format": "jsonlines"}},
        "LOG_LEVEL": "WARNING",
        "ROBOTSTXT_OBEY": False,
        "DOWNLOAD_DELAY": 0.1,
        "USER_AGENT": "bellhaven-crm-sync/1.0",
    })
    process.crawl(BellhavenSpider)
    process.start()

    with open(tmp) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    os.remove(tmp)
    # de-dupe by slug, stable order
    rows = sorted({r["slug"]: r for r in rows}.values(), key=lambda r: r["name"])
    with open(args.output, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"Scraped {len(rows)} locations -> {args.output}")


if __name__ == "__main__":
    main()
