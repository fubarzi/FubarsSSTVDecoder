"""One-time source fetch for the vendored pure-Python SGP4 wheel."""
import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
VERSION = '2.27'
with urlopen(f'https://pypi.org/pypi/sgp4/{VERSION}/json', timeout=15) as response:
    metadata = json.load(response)
file = next(item for item in metadata['urls'] if item['filename'].endswith('py3-none-any.whl'))
with urlopen(file['url'], timeout=20) as response:
    wheel = response.read()
digest = hashlib.sha256(wheel).hexdigest()
assert digest == file['digests']['sha256'], 'Wheel digest mismatch'
destination = ROOT / 'vendor'
destination.mkdir(exist_ok=True)
with ZipFile(BytesIO(wheel)) as archive:
    for name in archive.namelist():
        parts = PurePosixPath(name).parts
        if not parts or '..' in parts or name.endswith('/'):
            continue
        if parts[0] != 'sgp4' and not (parts[0].startswith('sgp4-') and 'LICENSE' in name):
            continue
        path = destination.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive.read(name))
(destination / 'README.txt').write_text(
    f'Bundled sgp4 {VERSION} pure-Python wheel from PyPI.\n'
    f'SHA256: {digest}\n'
    'Project: https://pypi.org/project/sgp4/\n'
    'License: MIT (see bundled license file).\n', encoding='utf-8')
print('Vendored', file['filename'], digest)
