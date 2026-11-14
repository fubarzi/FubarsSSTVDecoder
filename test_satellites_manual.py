"""Manual satellite records survive restarts and catalog downloads."""
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from satellites import SatelliteCatalog


LUCA = [
    '1 67287U 25313AR  26317.19964205  .00010094  00000-0  38548-3 0  9994',
    '2 67287  97.3803  30.3533 0008986 180.2735 179.8501 15.26796163 48690',
]


class ManualSatelliteTest(unittest.TestCase):
    def test_persistence_and_catalog_refresh(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'satellites.json'
            catalog = SatelliteCatalog(path)
            self.assertEqual(catalog.add_manual('LUCA', LUCA), 67287)
            self.assertEqual(catalog.get(67287)['OBJECT_NAME'], 'LUCA')
            self.assertTrue((Path(folder) / 'satellites.manual.json').is_file())

            downloaded = [{'NORAD_CAT_ID': 67287, 'OBJECT_NAME': 'Downloaded name', 'lines': LUCA}]
            with patch('satellites.urlopen', return_value=BytesIO(json.dumps(downloaded).encode())):
                catalog.refresh()
            self.assertEqual(catalog.get(67287)['OBJECT_NAME'], 'LUCA')
            self.assertEqual(SatelliteCatalog(path).get(67287)['OBJECT_NAME'], 'LUCA')

    def test_bad_tle_is_not_saved(self):
        with TemporaryDirectory() as folder:
            catalog = SatelliteCatalog(Path(folder) / 'satellites.json')
            damaged = [LUCA[0][:-1] + '0', LUCA[1]]
            with self.assertRaisesRegex(ValueError, 'valid checksums'):
                catalog.add_manual('LUCA', damaged)
            self.assertFalse(catalog.manual_path.exists())


if __name__ == '__main__':
    unittest.main()
