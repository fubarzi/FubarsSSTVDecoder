"""Online UI translations with a local cache for repeat use."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TRANSLATE_URL = 'https://translate.googleapis.com/translate_a/single?'


class TranslationCache:
    def __init__(self, path: Path):
        self.path = path
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.data = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, language, original):
        if language == 'en':
            return original
        return self.data.get(language, {}).get(original)

    @staticmethod
    def _translate(language, original):
        query = urlencode({'client': 'gtx', 'sl': 'en', 'tl': language,
                           'dt': 't', 'q': original})
        request = Request(TRANSLATE_URL + query,
                          headers={'User-Agent': 'Mozilla/5.0 (FubarsSSTVDecoder)'})
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
        translated = ''.join(part[0] or '' for part in result[0]).strip()
        if not translated:
            raise ValueError('Translation service returned an empty response')
        return translated

    def fetch(self, language, originals):
        """Translate missing labels in batches; keep usable cached labels on errors."""
        if language == 'en':
            return None
        missing = [text for text in dict.fromkeys(originals)
                   if text and len(text) <= 300 and not self.get(language, text)]
        if not missing:
            return None
        translations = {}
        for start in range(0, len(missing), 12):
            chunk = missing[start:start + 12]
            try:
                translated = self._translate(language, '\n'.join(chunk)).splitlines()
                if len(translated) != len(chunk):
                    translated = [self._translate(language, text) for text in chunk]
                translations.update(zip(chunk, translated))
            except Exception as exc:
                if not translations:
                    return str(exc)
                break
        if translations:
            self.data.setdefault(language, {}).update(translations)
            temporary = self.path.with_name(self.path.name + '.tmp')
            try:
                temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return None
