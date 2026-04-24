import datetime
import time
import random
import itertools
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright
from dataclasses import dataclass, asdict, field
import pandas as pd
import argparse
import os
import sys

BUSINESS_CATEGORIES = [
    "restaurants", "cafes", "coffee shops", "bars", "pubs",
    "gyms", "fitness centers", "yoga studios", "pilates studios",
    "dentists", "doctors", "pharmacies", "opticians", "chiropractors",
    "plumbers", "electricians", "roofers", "contractors", "painters",
    "lawyers", "accountants", "financial advisors", "insurance agents",
    "hair salons", "barbershops", "nail salons", "spas",
    "auto repair shops", "car dealerships", "car washes",
    "hotels", "bed and breakfast",
    "real estate agents", "estate agents",
    "photographers", "videographers",
    "tutors", "driving schools",
    "pet grooming", "veterinarians",
    "cleaners", "laundromats",
    "locksmiths", "pest control",
    "florists", "bakeries", "butchers",
    "architects", "interior designers",
    "mortgage brokers", "tax consultants",
]


@dataclass
class Business:
    name: str = None
    address: str = None
    domain: str = None
    website: str = None
    has_website: bool = None
    phone_number: str = None
    category: str = None
    location: str = None
    reviews_count: int = None
    reviews_average: float = None
    latitude: float = None
    longitude: float = None

    def __hash__(self):
        hash_fields = [self.name]
        if self.domain:
            hash_fields.append(f"domain:{self.domain}")
        if self.website:
            hash_fields.append(f"website:{self.website}")
        if self.phone_number:
            hash_fields.append(f"phone:{self.phone_number}")
        return hash(tuple(hash_fields))


@dataclass
class BusinessList:
    business_list: list[Business] = field(default_factory=list)
    _seen_businesses: set = field(default_factory=set, init=False)

    def add_business(self, business: Business):
        business_hash = hash(business)
        if business_hash not in self._seen_businesses:
            self.business_list.append(business)
            self._seen_businesses.add(business_hash)

    def dataframe(self):
        return pd.json_normalize(
            (asdict(business) for business in self.business_list), sep="_"
        )

    def append_to_csv(self, filepath):
        """Append results to a CSV, creating it with headers if it doesn't exist yet."""
        if not self.business_list:
            return 0
        df = self.dataframe()
        file_exists = os.path.exists(filepath)
        df.to_csv(filepath, mode='a' if file_exists else 'w', header=not file_exists, index=False)
        return len(self.business_list)


def extract_coordinates_from_url(url: str) -> tuple[float, float]:
    coordinates = url.split('/@')[-1].split('/')[0]
    return float(coordinates.split(',')[0]), float(coordinates.split(',')[1])


def scrape_query(page, search_for: str, total: int, session_csv: str, log_fn=print) -> int:
    """Run one search query, scrape results, append to session CSV. Returns count added."""
    search_url = f"https://www.google.com/maps/search/{quote_plus(search_for)}"
    page.goto(search_url, timeout=60000)
    page.wait_for_timeout(4000)

    place_locator = '//a[contains(@href, "https://www.google.com/maps/place")]'

    try:
        page.wait_for_selector(place_locator, timeout=15000)
        page.hover(place_locator)
    except Exception:
        log_fn("  No results found for this query.")
        return 0

    previously_counted = 0
    while True:
        page.mouse.wheel(0, 10000)
        page.wait_for_timeout(3000)

        count = page.locator(place_locator).count()
        if count >= total:
            listings = page.locator(place_locator).all()[:total]
            listings = [listing.locator("xpath=..") for listing in listings]
            log_fn(f"  Collected {len(listings)} listings")
            break
        elif count == previously_counted:
            listings = page.locator(place_locator).all()
            log_fn(f"  Reached end of results: {len(listings)} listings")
            break
        else:
            previously_counted = count
            print(f"  Scrolling... {count} so far", end='\r')

    business_list = BusinessList()

    for listing in listings:
        try:
            listing.click()
            page.wait_for_timeout(2000)

            name_attribute = 'h1.DUwDvf'
            address_xpath = '//button[@data-item-id="address"]//div[contains(@class, "fontBodyMedium")]'
            website_xpath = '//a[@data-item-id="authority"]//div[contains(@class, "fontBodyMedium")]'
            phone_number_xpath = '//button[contains(@data-item-id, "phone:tel:")]//div[contains(@class, "fontBodyMedium")]'
            review_count_xpath = '//div[@jsaction="pane.reviewChart.moreReviews"]//span'
            reviews_average_xpath = '//div[@jsaction="pane.reviewChart.moreReviews"]//div[@role="img"]'

            business = Business()

            if name_value := page.locator(name_attribute).inner_text():
                business.name = name_value.strip()
            else:
                business.name = ""

            if page.locator(address_xpath).count() > 0:
                business.address = page.locator(address_xpath).all()[0].inner_text()
            else:
                business.address = ""

            if page.locator(website_xpath).count() > 0:
                business.domain = page.locator(website_xpath).all()[0].inner_text()
                business.website = f"https://www.{page.locator(website_xpath).all()[0].inner_text()}"
                business.has_website = True
            else:
                business.domain = ""
                business.website = ""
                business.has_website = False

            if page.locator(phone_number_xpath).count() > 0:
                business.phone_number = page.locator(phone_number_xpath).all()[0].inner_text()
            else:
                business.phone_number = ""

            if page.locator(review_count_xpath).count() > 0:
                business.reviews_count = int(
                    page.locator(review_count_xpath).inner_text().split()[0].replace(',', '').strip()
                )
            else:
                business.reviews_count = ""

            if page.locator(reviews_average_xpath).count() > 0:
                business.reviews_average = float(
                    page.locator(reviews_average_xpath).get_attribute('aria-label').split()[0].replace(',', '.').strip()
                )
            else:
                business.reviews_average = ""

            business.category = search_for.split(' in ')[0].strip()
            business.location = search_for.split(' in ')[-1].strip()
            business.latitude, business.longitude = extract_coordinates_from_url(page.url)

            business_list.add_business(business)
        except Exception as e:
            print(f'  Error on listing: {e}', end='\r')

    return business_list.append_to_csv(session_csv)


def generate_queries(locations: list[str]):
    """Yield shuffled combinations of all business categories × provided locations."""
    pairs = list(itertools.product(BUSINESS_CATEGORIES, locations))
    random.shuffle(pairs)
    for category, location in pairs:
        yield f"{category} in {location}"


def main():
    parser = argparse.ArgumentParser(description="Google Maps Business Scraper")
    parser.add_argument("-s", "--search", type=str, help="Single search term")
    parser.add_argument("-t", "--total", type=int, help="Max listings per search query")
    parser.add_argument(
        "-d", "--duration", type=int,
        help="Run continuously for this many minutes, auto-generating queries"
    )
    args = parser.parse_args()

    total = args.total if args.total else 1_000_000
    continuous_mode = args.duration is not None

    # --- Build query list / generator ---
    if args.search:
        search_list = [args.search]
        continuous_mode = False
    else:
        input_file_path = os.path.join(os.getcwd(), 'input.txt')
        raw_lines = []
        if os.path.exists(input_file_path):
            with open(input_file_path, 'r') as f:
                raw_lines = [line.strip() for line in f if line.strip()]

        full_queries = [l for l in raw_lines if ' in ' in l.lower()]
        bare_locations = [l for l in raw_lines if ' in ' not in l.lower()]
        search_list = full_queries

        if continuous_mode:
            # In continuous mode, extract locations from both sources
            locations = bare_locations[:]
            for q in full_queries:
                loc = q.split(' in ')[-1].strip()
                if loc not in locations:
                    locations.append(loc)

            if not locations:
                print("Error: Add locations (e.g. 'London') to input.txt for continuous mode.")
                sys.exit()
        else:
            if not search_list:
                print("Error: Pass -s, add searches to input.txt, or use -d for continuous mode.")
                sys.exit()

    # --- Set up session output CSV ---
    session_start = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    save_dir = os.path.join('GMaps Data', today)
    os.makedirs(save_dir, exist_ok=True)
    session_csv = os.path.join(save_dir, f"session_{session_start}.csv")

    end_time = time.time() + (args.duration * 60) if args.duration else None

    print(f"\nOutput file: {session_csv}")
    if end_time:
        stop_at = datetime.datetime.fromtimestamp(end_time).strftime('%H:%M:%S')
        print(f"Running for {args.duration} minute(s) — will stop at {stop_at}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(locale="en-GB")
        page.goto("https://www.google.com/maps", timeout=20000)

        total_businesses = 0
        queries_done = 0

        if continuous_mode:
            query_stream = itertools.chain(full_queries, generate_queries(locations))

            for search_for in query_stream:
                if end_time and time.time() >= end_time:
                    print("\nTime limit reached.")
                    break

                remaining_min = (end_time - time.time()) / 60 if end_time else None
                time_info = f"  ({remaining_min:.1f} min left)" if remaining_min is not None else ""
                print(f"[Query {queries_done + 1}]{time_info} {search_for}")

                try:
                    added = scrape_query(page, search_for, total, session_csv)
                    total_businesses += added
                    queries_done += 1
                    print(f"  +{added} businesses | session total: {total_businesses}")
                except Exception as e:
                    print(f"  Query failed: {e}")
        else:
            for i, search_for in enumerate(search_list):
                print(f"[{i + 1}/{len(search_list)}] {search_for}")
                try:
                    added = scrape_query(page, search_for, total, session_csv)
                    total_businesses += added
                    queries_done += 1
                    print(f"  +{added} businesses | total: {total_businesses}")
                except Exception as e:
                    print(f"  Query failed: {e}")

        print(f"\nFinished. {queries_done} queries run, {total_businesses} businesses saved.")
        print(f"CSV: {session_csv}")
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as e:
        print(f'Fatal error: {e}')
