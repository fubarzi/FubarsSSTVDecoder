"""Cached CelesTrak amateur-satellite catalog (OMM GP elements)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
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


def record_epoch(row):
    """UTC epoch of an OMM or TLE record, for keeping the freshest orbit."""
    try:
        if row.get('EPOCH'):
            epoch = datetime.fromisoformat(row['EPOCH'].replace('Z', '+00:00'))
            return epoch if epoch.tzinfo else epoch.replace(tzinfo=timezone.utc)
        line = row['lines'][0]
        year = int(line[18:20])
        year += 2000 if year < 57 else 1900
        return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=float(line[20:32]) - 1)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def merge_latest(base, additions):
    merged = base.copy()
    for number, row in additions.items():
        previous = merged.get(number)
        old_epoch = record_epoch(previous) if previous else None
        new_epoch = record_epoch(row)
        if previous is None or old_epoch is None or (new_epoch is not None and new_epoch >= old_epoch):
            merged[number] = row
    return merged


class SatelliteCatalog:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.manual_path = path.with_name('satellites.manual.json')
        self.records = {}
        self.defaults = {}
        self.manual = {}
        self.checked_at = None
        self.status = 'Catalog not downloaded'
        try:
            saved = json.loads(path.with_name('satellites.default.json').read_text(encoding='utf-8'))
            self.defaults = {str(row['NORAD_CAT_ID']): row for row in saved['records'] if valid_record(row)}
            self.records = self.defaults.copy()
            self.checked_at = datetime.fromisoformat(saved['checked_at'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.records = merge_latest(self.records, {
                str(row['NORAD_CAT_ID']): row for row in saved['records'] if valid_record(row)})
            self.checked_at = datetime.fromisoformat(saved['checked_at'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            saved = json.loads(self.manual_path.read_text(encoding='utf-8'))
            self.manual = {str(row['NORAD_CAT_ID']): row for row in saved['records'] if valid_record(row)}
            self.records.update(self.manual)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if self.records:
            self.status = f'{len(self.records):,} amateur satellites cached'

    def entries(self):
        return sorted(self.records.values(), key=lambda row: (row['OBJECT_NAME'].casefold(), int(row['NORAD_CAT_ID'])))

    def get(self, catalog_number):
        return self.records.get(str(catalog_number))

    def add_manual(self, name, lines):
        """Save a user supplied TLE independently of downloaded catalog data."""
        name = name.strip()
        lines = [line.strip() for line in lines]
        if not name or len(name) > 80:
            raise ValueError('Enter a satellite name of up to 80 characters')
        if not valid_tle(lines):
            raise ValueError('Enter two complete TLE lines with valid checksums and matching NORAD IDs')
        try:
            number = int(lines[0][2:7])
        except ValueError as exc:
            raise ValueError('The TLE must use a numeric NORAD ID') from exc
        if number == 25544:
            raise ValueError('The ISS orbit is managed separately; use Update Selected Orbit Data')
        row = {'NORAD_CAT_ID': number, 'OBJECT_NAME': name, 'lines': lines}
        with self.lock:
            manual = {**self.manual, str(number): row}
            temporary = self.manual_path.with_name(self.manual_path.name + '.tmp')
            try:
                temporary.write_text(json.dumps({'records': list(manual.values())}, indent=2), encoding='utf-8')
                temporary.replace(self.manual_path)
            finally:
                temporary.unlink(missing_ok=True)
            self.manual = manual
            self.records = {**self.records, str(number): row}
            self.status = f'{len(self.records):,} amateur satellites cached · {len(self.manual)} added manually'
        return number

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
            records = merge_latest(self.defaults, records)
            with self.lock:
                self.path.write_text(json.dumps({'checked_at': now.isoformat(), 'records': list(records.values())}),
                                     encoding='utf-8')
                records.update(self.manual)
                self.records, self.checked_at = records, now
                self.status = f'{len(records):,} amateur satellites updated from {source_name} · {now:%d %b %H:%M} UTC'
        except Exception as exc:
            self.status = f'Catalog update failed · using {len(self.records):,} cached satellites · {exc}'
        return self.status
