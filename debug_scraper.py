#!/usr/bin/env python3
"""Script to debug and verify Soccerbase scraper data structure"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import json


def fetch_and_debug_page(url, description):
    print(f"\n{'='*60}")
    print(f"DEBUG: {description}")
    print(f"URL: {url}")
    print('='*60)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        print(f"Status: {response.status_code}")
    except Exception as e:
        print(f"Failed to fetch page: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    print(f"Page title: {soup.title.string if soup.title else 'No title'}")

    # Look for common tables/elements we use
    print("\nLooking for key elements:")

    # Check for fixture/results tables
    tables_with_cards = soup.find_all("table", class_="listWithCards")
    print(f"Found {len(tables_with_cards)} table(s) with class 'listWithCards'")

    if tables_with_cards:
        print(f"\nFirst table HTML preview:")
        print(str(tables_with_cards[0])[:2000])  # Print first 2000 chars of first table

    # Save the full HTML to a file for later inspection
    safe_filename = description.replace(" ", "_").lower() + ".html"
    with open(safe_filename, "w", encoding="utf-8") as f:
        f.write(response.text)
    print(f"\nSaved full HTML to '{safe_filename}'")

    return soup


# Test 1: Fetch today's (or yesterday's) fixtures/results
today = datetime.now().strftime("%Y-%m-%d")
yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Fetch a fixtures page
fixtures_soup = fetch_and_debug_page(
    f"https://www.soccerbase.com/matches/results.sd?date={today}",
    f"Fixtures for {today}"
)

if fixtures_soup:
    # Try to parse some matches manually to check
    print(f"\nTesting manual fixture parsing:")
    tables = fixtures_soup.find_all("table", class_="listWithCards")
    for table_idx, table in enumerate(tables[:1]):
        print(f"\nTable {table_idx}:")
        rows = table.find_all("tr")
        for row_idx, row in enumerate(rows[:10]):
            print(f"Row {row_idx}: {row.get_text(strip=True)}")
