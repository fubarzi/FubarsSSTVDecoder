"""Cached CelesTrak amateur-satellite catalog (OMM GP elements)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.request import Request, urlopen


CATALOG_URL = 'https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT='
AMSAT_URL = 'https://www.amsat.org/tle/current/nasabare.txt'
MIN_REFRESH = timedelta(hours=2)
REQUIRED = ('NORAD_CAT_ID', 'OBJECT_NAME', 'EPOCH', 'MEAN_MOTION', 'ECCENTRICITY',
            'INCLINATION', 'RA_OF_ASC_NODE', 'ARG_OF_PERICENTER', 'MEAN_ANOMALY',
            'BSTAR', 'MEAN_MOTION_DOT', 'MEAN_MOTION_DDOT', 'CLASSIFICATION_TYPE',
            'OBJECT_ID', 'EPHEMERIS_TYPE', 'ELEMENT_SET_NO', 'REV_AT_EPOCH')


def valid_record(row):
    return (isinstance(row, dict) and
            (all(row.get(key) is not None for key in REQUIRED) or
             (row.get('NORAD_CAT_ID') and row.get('OBJECT_NAME') and valid_tle(row.get('lines', [])))))


def valid_tle(lines):
    if not isinstance(lines, list) or len(lines) != 2 or not lines[0].startswith('1 ') or not lines[1].startswith('2 '):
        return False
    if lines[0][2:7] != lines[1][2:7]:
        return False
    for line in lines:
        if len(line) < 69 or not line[68].isdigit():
            return False
        checksum = sum(int(char) for char in line[:68] if char.isdigit()) + line[:68].count('-')
        if checksum % 10 != int(line[68]):
            return False
    return True


def parse_tle_catalog(raw):
    rows = [line.strip() for line in raw.splitlines() if line.strip()]
    result = []
    for index in range(len(rows) - 2):
        lines = rows[index + 1:index + 3]
        if valid_tle(lines) and not rows[index].startswith(('1 ', '2 ')):
            result.append({'NORAD_CAT_ID': int(lines[0][2:7]), 'OBJECT_NAME': rows[index], 'lines': lines})
    return result


class SatelliteCatalog:
    def __init__(self, path: Path):
        self.path = path
        self.records = {}
        self.checked_at = None
        self.status = 'Catalog not downloaded'
        try:
            source = path if path.exists() else path.with_name('satellites.default.json')
            saved = json.loads(source.read_text(encoding='utf-8'))
            self.records = {str(row['NORAD_CAT_ID']): row for row in saved['records'] if valid_record(row)}
            self.checked_at = datetime.fromisoformat(saved['checked_at'])
            self.status = f'{len(self.records):,} amateur satellites cached'
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def entries(self):
        return sorted(self.records.values(), key=lambda row: (row['OBJECT_NAME'].casefold(), int(row['NORAD_CAT_ID'])))

    def get(self, catalog_number):
        return self.records.get(str(catalog_number))

    def refresh(self):
        now = datetime.now(timezone.utc)
        if self.records and self.checked_at and now - self.checked_at < MIN_REFRESH:
            self.status = f'{len(self.records):,} amateur satellites cached · checked {self.checked_at:%d %b %H:%M} UTC'
            return self.status
        try:
            errors = []
            rows = None
            source_name = None
            for source_name, format_name, url in (
                    ('CelesTrak', 'JSON', CATALOG_URL + 'JSON'),
                    ('CelesTrak', 'TLE', CATALOG_URL + 'TLE'),
                    ('AMSAT', 'TLE', AMSAT_URL)):
                try:
                    request = Request(url,
                                      headers={'User-Agent': 'FubarsSSTVDecoder/1.0'})
                    with urlopen(request, timeout=30) as response:
                        raw = response.read(32_000_001)
                    if len(raw) > 32_000_000:
                        raise ValueError('Catalog exceeds 32 MB')
                    rows = (json.loads(raw) if format_name == 'JSON' else
                            parse_tle_catalog(raw.decode('utf-8', errors='replace')))
                    if rows:
                        break
                    raise ValueError('Empty catalog')
                except Exception as exc:
                    errors.append(f'{source_name} {format_name}: {exc}')
            if rows is None:
                raise ValueError('; '.join(errors))
            if not isinstance(rows, list):
                raise ValueError('Invalid catalog response')
            records = {str(row['NORAD_CAT_ID']): row for row in rows if valid_record(row)}
            if not records:
                raise ValueError('No usable satellite records')
            self.path.write_text(json.dumps({'checked_at': now.isoformat(), 'records': list(records.values())}),
                                 encoding='utf-8')
            self.records, self.checked_at = records, now
            self.status = f'{len(records):,} amateur satellites updated from {source_name} · {now:%d %b %H:%M} UTC'
        except Exception as exc:
            self.status = f'Catalog update failed · using {len(self.records):,} cached satellites · {exc}'
        return self.status
