"""Receive-only SSTV tuning references by country and IARU region."""
from __future__ import annotations

import json
from pathlib import Path


PLAN_URLS = {
    1: 'https://www.iaru-r1.org/on-the-air/band-plans/',
    2: 'https://www.iaru-r2.org/en/reference/band-plans/',
    3: 'https://www.iaru.org/wp-content/uploads/2020/01/R3-004-IARU-Region-3-Bandplan-rev.2.pdf',
}
AU_URL = 'https://www.wia.org.au/members/bandplans/data/'
US_URL = 'https://www.arrl.org/band-plan'


def load_countries(path: Path):
    rows = json.loads(path.read_text(encoding='utf-8'))['countries']
    return {row['code']: row for row in rows if row['region'] in (1, 2, 3)}


def load_languages(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))['languages']


def sstv_plan(country_code, countries):
    """National AU/US image centres where verified, regional reference elsewhere."""
    country = countries.get(country_code, countries['AU'])
    if country['code'] == 'AU':
        frequency, source, url = 7.171, 'WIA Australia 2026', AU_URL
    elif country['code'] == 'US':
        frequency, source, url = 7.171, 'ARRL United States', US_URL
    else:
        frequency = 7.165
        source = f"IARU Region {country['region']} reference"
        url = PLAN_URLS[country['region']]
    return {
        'country': country,
        'source': source,
        'url': url,
        'presets': [
            ('40 m', frequency, 'LSB'),
            ('20 m', 14.230, 'USB'),
            ('15 m', 21.340, 'USB'),
            ('10 m', 28.680, 'USB'),
        ],
    }
