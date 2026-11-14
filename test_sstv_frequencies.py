"""SatNOGS transmitter filtering and local cache checks."""
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sstv_frequencies import SSTVFrequencies, sstv_downlinks


def main():
    rows = [
        {'norad_cat_id': 25544, 'mode': 'SSTV', 'description': 'Mode V Imaging',
         'status': 'active', 'alive': True, 'unconfirmed': False,
         'downlink_low': 145800000, 'downlink_high': None},
        {'norad_cat_id': 25544, 'mode': 'SSTV', 'description': 'Old SSTV',
         'status': 'inactive', 'alive': False, 'downlink_low': 145480000},
        {'norad_cat_id': 25544, 'mode': 'FM', 'description': 'Voice',
         'status': 'active', 'alive': True, 'downlink_low': 145800000},
        {'norad_cat_id': 12345, 'mode': 'SSTV', 'description': 'Different satellite',
         'status': 'active', 'alive': True, 'downlink_low': 437550000},
        {'norad_cat_id': 25544, 'mode': 'SSTV', 'description': 'Range',
         'status': 'active', 'alive': True, 'downlink_low': 437500000,
         'downlink_high': 437600000},
    ]
    assert [item['frequency_mhz'] for item in sstv_downlinks(rows, 25544)] == [145.8]
    with TemporaryDirectory() as folder:
        cache = SSTVFrequencies(Path(folder) / 'frequencies.json')
        with patch('sstv_frequencies.urlopen', side_effect=lambda *_args, **_kwargs:
                   BytesIO(json.dumps(rows).encode())) as get:
            downlinks, status = cache.lookup(25544)
            assert get.call_count == 1 and 'updated' in status
            assert downlinks[0]['frequency_mhz'] == 145.8
            assert cache.lookup(25544)[0] == downlinks
            assert get.call_count == 1
            all_downlinks, status = cache.lookup_all()
            assert 'updated' in status
            assert set(all_downlinks) == {'25544', '12345'}
            assert cache.lookup_all()[0] == all_downlinks
            assert get.call_count == 2
        reopened = SSTVFrequencies(cache.path)
        assert reopened.cached(25544) == downlinks
        assert reopened.cached_all() == all_downlinks
    print('SatNOGS SSTV filtering and cache passed')


if __name__ == '__main__':
    main()
