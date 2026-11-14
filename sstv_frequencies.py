"""SSTV downlinks from SatNOGS DB, cached by NORAD catalog number."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from urllib.request import Request, urlopen


API_URL = 'https://db.satnogs.org/api/transmitters/?satellite__norad_cat_id='
ALL_SSTV_URL = 'https://db.satnogs.org/api/transmitters/?status=active&alive=true'
MAX_AGE = timedelta(hours=24)


def sstv_downlinks(rows, catalog_number):
    """Only use active, confirmed, single-frequency SSTV downlinks."""
    choices = {}
    for row in rows:
        try:
            if str(row.get('norad_cat_id')) != str(catalog_number):
                continue
            description = str(row.get('description') or '')
            if str(row.get('mode') or '').upper() != 'SSTV' and 'SSTV' not in description.upper():
                continue
            if row.get('status') != 'active' or row.get('alive') is not True or row.get('unconfirmed') is True:
                continue
            low = int(row['downlink_low'])
            high = row.get('downlink_high')
            if high is not None and int(high) != low:
                continue
            if not 1_000 <= low <= 1_700_000_000:
                continue
            mhz = round(low / 1e6, 6)
            choices[mhz] = {'frequency_mhz': mhz, 'description': description or 'SSTV',
                            'source': 'SatNOGS DB'}
        except (TypeError, ValueError, KeyError):
            continue
    return [choices[mhz] for mhz in sorted(choices)]


class SSTVFrequencies:
    def __init__(self, path: Path):
        self.path = path
        self.all_path = path.with_name('sstv_frequencies_all.json')
        self.entries = {}
        self.all_entries = {}
        self.all_checked_at = None
        self.lock = threading.Lock()
        try:
            source = path if path.exists() else path.with_name('sstv_frequencies.default.json')
            self.entries = json.loads(source.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        try:
            saved = json.loads(self.all_path.read_text(encoding='utf-8'))
            self.all_entries = saved['downlinks']
            self.all_checked_at = datetime.fromisoformat(saved['checked_at'])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def cached(self, catalog_number):
        return self.entries.get(str(catalog_number), {}).get('downlinks', [])

    def cached_all(self):
        return self.all_entries.copy()

    def lookup_all(self, force=False):
        """Get active SSTV transmitters, including those labeled SSTV in descriptions."""
        with self.lock:
            now = datetime.now(timezone.utc)
            if self.all_checked_at and not force and now - self.all_checked_at < MAX_AGE:
                return self.cached_all(), 'SatNOGS DB · cached'
            try:
                request = Request(ALL_SSTV_URL, headers={'User-Agent': 'FubarsSSTVDecoder/1.0',
                                                          'Accept': 'application/json'})
                with urlopen(request, timeout=20) as response:
                    raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ValueError('SSTV transmitter response too large')
                rows = json.loads(raw)
                if not isinstance(rows, list):
                    raise ValueError('Invalid SSTV transmitter response')
                rows = [row for row in rows if isinstance(row, dict) and
                        (str(row.get('mode') or '').upper() == 'SSTV' or
                         'SSTV' in str(row.get('description') or '').upper())]
                numbers = {str(row['norad_cat_id']) for row in rows if row.get('norad_cat_id') is not None}
                downlinks = {number: sstv_downlinks(rows, number) for number in numbers}
                downlinks = {number: values for number, values in downlinks.items() if values}
                temporary = self.all_path.with_name(self.all_path.name + '.tmp')
                try:
                    temporary.write_text(json.dumps({'checked_at': now.isoformat(), 'downlinks': downlinks}, indent=2),
                                         encoding='utf-8')
                    temporary.replace(self.all_path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.all_entries, self.all_checked_at = downlinks, now
                return self.cached_all(), 'SatNOGS DB · updated'
            except Exception as exc:
                return self.cached_all(), f'SatNOGS DB unavailable · {exc}'

    def lookup(self, catalog_number, force=False):
        with self.lock:
            return self._lookup(catalog_number, force)

    def _lookup(self, catalog_number, force):
        key = str(catalog_number)
        cached = self.entries.get(key, {})
        checked = cached.get('checked_at')
        if checked and not force:
            try:
                if datetime.now(timezone.utc) - datetime.fromisoformat(checked) < MAX_AGE:
                    return self.cached(key), 'SatNOGS DB · cached'
            except ValueError:
                pass
        try:
            request = Request(API_URL + key, headers={'User-Agent': 'FubarsSSTVDecoder/1.0',
                                                       'Accept': 'application/json'})
            with urlopen(request, timeout=15) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('Transmitter response too large')
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise ValueError('Invalid transmitter response')
            downlinks = sstv_downlinks(rows, key)
            self.entries[key] = {'checked_at': datetime.now(timezone.utc).isoformat(),
                                 'downlinks': downlinks}
            temp = self.path.with_suffix(self.path.suffix + '.tmp')
            temp.write_text(json.dumps(self.entries, indent=2), encoding='utf-8')
            temp.replace(self.path)
            return downlinks, 'SatNOGS DB · updated'
        except Exception as exc:
            return self.cached(key), f'SatNOGS DB unavailable · {exc}'
