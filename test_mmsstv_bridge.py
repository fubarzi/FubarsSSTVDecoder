"""MMSSTV helper startup and bitmap import checks."""
import json
import time

from PIL import Image

from core import ROOT
from mmsstv_bridge import MmsstvBridge


def main():
    bridge = MmsstvBridge(ROOT)
    try:
        bridge.start()
        deadline = time.monotonic() + 8
        while bridge.status['state'] == 'starting' and time.monotonic() < deadline:
            bridge.poll()
            time.sleep(.1)
        assert bridge.status['state'] == 'ready', bridge.status
        assert bridge.status['input_device'].startswith('CABLE Output')
        assert f"Device={bridge.status['input_index']}" in (
            bridge.output_dir / 'sstveng.ini').read_text(encoding='ascii')
        bitmap = Image.new('RGB', (800, 616), (11, 22, 33))
        bitmap.save(bridge.output_dir / 'current.bmp')
        (bridge.output_dir / 'status.json').write_text(json.dumps({
            'state': 'ready', 'receiving': True, 'mode': 'Robot 36',
            'width': 320, 'height': 240, 'updated': 1}), encoding='utf-8')
        bridge.poll()
        assert bridge.image.size == (320, 240)
        assert bridge.image.getpixel((10, 10)) == (11, 22, 33)
        assert bridge.mode == 'Robot 36'
        bitmap.save(bridge.output_dir / 'complete_test.bmp')
        complete = bridge.poll()
        assert complete is not None and complete.size == (320, 240)
        assert bridge.poll() is None
        print('MMSSTV helper started and bitmap import passed')
    finally:
        bridge.close()


if __name__ == '__main__':
    main()
