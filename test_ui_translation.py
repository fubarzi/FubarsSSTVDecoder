"""Language cache survives restart and keeps translated labels offline."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ui_translation import TranslationCache


class TranslationCacheTest(unittest.TestCase):
    def test_cached_ui_strings(self):
        with TemporaryDirectory() as folder:
            cache = TranslationCache(Path(folder) / 'translations.json')
            response = BytesIO(b'[[["AGGIUNGI SATELLITE\\nLINGUA",null,null,null]] ]')
            with patch('ui_translation.urlopen', return_value=response) as request:
                self.assertIsNone(cache.fetch('it', ['ADD SATELLITE', 'LANGUAGE']))
                self.assertEqual(request.call_count, 1)
            reopened = TranslationCache(cache.path)
            self.assertEqual(reopened.get('it', 'ADD SATELLITE'), 'AGGIUNGI SATELLITE')
            self.assertEqual(reopened.get('it', 'LANGUAGE'), 'LINGUA')
            self.assertEqual(reopened.get('en', 'LANGUAGE'), 'LANGUAGE')


if __name__ == '__main__':
    unittest.main()
