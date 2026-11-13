"""Run the 32-bit MMSSTV engine and read its decoded images."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from PIL import Image


class MmsstvBridge:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.engine_dir = self.root / 'mmsstv_engine'
        self.script = self.root / 'mmsstv_engine_bridge.ps1'
        self.work = tempfile.TemporaryDirectory(prefix='fubars-mmsstv-')
        self.output_dir = Path(self.work.name)
        self.process = None
        self.status = {'state': 'starting', 'receiving': False, 'mode': ''}
        self.image = None
        self.image_stamp = None
        self.completed = set()
        self.mode = ''

    def start(self):
        powershell = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'SysWOW64' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        if not powershell.is_file():
            raise RuntimeError('32-bit Windows PowerShell is unavailable')
        if not (self.engine_dir / 'SSTVENG.DLL').is_file() or not self.script.is_file():
            raise RuntimeError('Bundled MMSSTV engine is missing')
        self.process = subprocess.Popen(
            [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(self.script), '-EngineDir', str(self.engine_dir),
             '-OutputDir', str(self.output_dir), '-ParentPid', str(os.getpid())],
            cwd=self.output_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)

    def poll(self):
        path = self.output_dir / 'status.json'
        if path.is_file():
            try:
                self.status = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                pass
        if self.process is not None and self.process.poll() is not None and self.status.get('state') != 'error':
            self.status = {'state': 'error', 'message': f'MMSSTV engine exited ({self.process.returncode})'}
        if self.status.get('state') == 'ready':
            self.mode = self.status.get('mode') or self.mode
            current = self.output_dir / 'current.bmp'
            if current.is_file():
                try:
                    stamp = current.stat().st_mtime_ns
                    if stamp != self.image_stamp:
                        self.image = self._read_image(current)
                        self.image_stamp = stamp
                except (OSError, ValueError):
                    pass
            for completed in sorted(self.output_dir.glob('complete_*.bmp')):
                if completed.name not in self.completed:
                    try:
                        image = self._read_image(completed)
                    except (OSError, ValueError):
                        continue
                    self.completed.add(completed.name)
                    self.image = image
                    return image.copy()
        return None

    def _read_image(self, path):
        with Image.open(path) as bitmap:
            image = bitmap.convert('RGB')
        width = self.status.get('width')
        height = self.status.get('height')
        if isinstance(width, int) and isinstance(height, int) and 0 < width <= image.width and 0 < height <= image.height:
            image = image.crop((0, 0, width, height))
        return image

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.work.cleanup()
