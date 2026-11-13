"""Direct Windows RTL-SDR access, independent of SoapySDR discovery."""
from __future__ import annotations

from collections import deque
from collections import Counter
import ctypes as C
import os
from pathlib import Path
import sys
import threading
import winreg

import numpy as np

from fm import (StreamingHFDemodulator, StreamingAudioNoiseFilter, StreamingSquelch,
                StreamingFrequencyShifter, digital_shift_hz, HF_MODES, make_demodulator)


ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


class RTLDriver:
    def __init__(self):
        path = ROOT / 'runtime' / 'bin' / 'librtlsdr.dll'
        self.error = ''
        self.lib = None
        try:
            self.dll_dir = os.add_dll_directory(str(path.parent))
            self.lib = C.CDLL(str(path))
            self._bind()
        except (OSError, AttributeError) as exc:
            self.error = f'RTL-SDR driver unavailable: {exc}'

    def _bind(self):
        d = self.lib
        def bind(name, result, *args):
            fn = getattr(d, 'rtlsdr_' + name)
            fn.restype, fn.argtypes = result, args
            return fn
        p, u, i = C.c_void_p, C.c_uint32, C.c_int
        self.count = bind('get_device_count', u)
        self.name = bind('get_device_name', C.c_char_p, u)
        self.usb = bind('get_device_usb_strings', i, u, C.c_void_p, C.c_void_p, C.c_void_p)
        self.open = bind('open', i, C.POINTER(p), u)
        self.close = bind('close', i, p)
        self.rate = bind('set_sample_rate', i, p, u)
        self.frequency = bind('set_center_freq', i, p, u)
        self.get_frequency = bind('get_center_freq', u, p)
        self.correction = bind('set_freq_correction', i, p, i)
        self.gain_mode = bind('set_tuner_gain_mode', i, p, i)
        self.gain = bind('set_tuner_gain', i, p, i)
        self.gains = bind('get_tuner_gains', i, p, C.POINTER(i))
        self.get_gain = bind('get_tuner_gain', i, p)
        self.tuner_type = bind('get_tuner_type', i, p)
        self.reset = bind('reset_buffer', i, p)
        self.read = bind('read_sync', i, p, p, i, C.POINTER(i))

    def devices(self):
        if not self.lib:
            return []
        friendly = {}
        try:
            locate = C.windll.cfgmgr32.CM_Locate_DevNodeW
            locate.argtypes = [C.POINTER(C.c_ulong), C.c_wchar_p, C.c_ulong]
            locate.restype = C.c_ulong
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'SYSTEM\CurrentControlSet\Enum\USB\VID_0BDA&PID_2838') as parent:
                for child_index in range(winreg.QueryInfoKey(parent)[0]):
                    serial = winreg.EnumKey(parent, child_index)
                    device_node = C.c_ulong()
                    if locate(C.byref(device_node),
                              'USB\\VID_0BDA&PID_2838\\' + serial, 0) != 0:
                        continue
                    with winreg.OpenKey(parent, serial) as child:
                        friendly[serial] = winreg.QueryValueEx(child, 'FriendlyName')[0]
        except (OSError, AttributeError):
            pass
        rows = []
        for index in range(self.count()):
            manufacturer = C.create_string_buffer(256)
            product = C.create_string_buffer(256)
            serial = C.create_string_buffer(256)
            self.usb(index, manufacturer, product, serial)
            rows.append({'index': index, 'serial': serial.value.decode(errors='replace'),
                         'manufacturer': manufacturer.value.decode(errors='replace').strip(),
                         'product': product.value.decode(errors='replace').strip()})
        serial_counts = Counter(row['serial'] for row in rows if row['serial'])
        missing = [row for row in rows if not row['serial']]
        unmatched = [serial for serial in friendly if serial not in {row['serial'] for row in rows}]
        if len(friendly) == len(rows) and len(missing) == len(unmatched) == 1:
            missing[0]['serial'] = unmatched[0]
        found = []
        for row in rows:
            index, serial = row['index'], row['serial']
            # Identical USB serials are common in RTL sticks. A Windows friendly
            # name for that serial cannot identify which stick libusb returned.
            friendly_name = friendly.get(serial, '') if serial_counts[serial] == 1 else ''
            hint = (row['product'] + ' ' + friendly_name).lower()
            model = ('RTL-SDR Blog V4' if 'blog v4' in hint else
                     'RTL-SDR Blog V3' if 'blog v3' in hint else
                     ' '.join(part for part in (row['manufacturer'], row['product']) if part).strip()
                     or friendly_name or 'RTL-SDR')
            tuner_name = ''
            handle = C.c_void_p()
            if self.open(C.byref(handle), index) == 0:
                try:
                    tuner_name = {1: 'E4000', 5: 'R820T', 6: 'R828D'}.get(self.tuner_type(handle), '')
                finally:
                    self.close(handle)
            if tuner_name:
                model += f' ({tuner_name})'
            found.append({'id': f'rtl:{index}',
                          'label': f'{model or "RTL-SDR"} {index + 1} · {serial or "serial unavailable"}',
                          'driver': 'rtl'})
        return found


class RTLReceiver:
    def __init__(self, driver):
        self.driver = driver
        self.lock = threading.Lock()
        self.audio = deque(maxlen=90)
        self.iq = b''
        self.iq_frames = deque(maxlen=40)
        self.running = False
        self.error = ''
        self.thread = None
        self.stop_event = threading.Event()
        self.rate = 240000
        self.audio_rate = 24000
        self.iq_rate = 240000
        self.target_frequency_mhz = 437.55
        self.target_center_mhz = 437.55
        self.commanded_frequency_mhz = None
        self.tuned_frequency_mhz = None
        self.target_bandwidth_khz = 12.5
        self.demod_mode = 'FM'
        self.target_gain_db = 25.0
        self.target_ppm_correction = 0
        self.applied_ppm_correction = None
        self.commanded_gain_db = None
        self.applied_gain_db = None
        self.noise_filter_enabled = True
        self.target_squelch_dbfs = -120.0
        self.signal_level_dbfs = -120.0
        self.squelch_open = True
        self.monitor_sink = None

    def start(self, device_id, frequency_mhz, gain_db, bandwidth_khz=12.5,
              ppm_correction=0, demod_mode='FM', antenna_name='', noise_filter_enabled=True,
              squelch_dbfs=-120, center_frequency_mhz=None):
        self.stop()
        if not self.driver.lib:
            raise RuntimeError(self.driver.error)
        if not device_id.startswith('rtl:'):
            raise ValueError('Select an RTL-SDR')
        self.stop_event.clear()
        self.error = ''
        self.target_frequency_mhz = float(frequency_mhz)
        self.target_center_mhz = (float(center_frequency_mhz) if center_frequency_mhz is not None else
                                  float(frequency_mhz) + (StreamingHFDemodulator.offset_hz / 1e6
                                                          if demod_mode in HF_MODES else 0))
        self.commanded_frequency_mhz = None
        self.tuned_frequency_mhz = None
        self.target_bandwidth_khz = float(bandwidth_khz)
        self.demod_mode = demod_mode
        self.target_gain_db = float(gain_db)
        self.target_ppm_correction = int(ppm_correction)
        self.applied_ppm_correction = None
        self.commanded_gain_db = None
        self.applied_gain_db = None
        self.noise_filter_enabled = bool(noise_filter_enabled)
        self.target_squelch_dbfs = float(squelch_dbfs)
        self.signal_level_dbfs = -120.0
        self.squelch_open = True
        self.thread = threading.Thread(target=self._run,
                                       args=(int(device_id[4:]), float(gain_db)), daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        with self.lock:
            self.running = False
            self.audio.clear()
            self.iq_frames.clear()
            self.iq = b''
        self.thread = None

    def _run(self, index, gain_db):
        d = self.driver
        handle = C.c_void_p()
        if d.open(C.byref(handle), index) != 0:
            self.error = 'RTL-SDR could not open. Check the USB driver or close another SDR program.'
            return
        try:
            ppm_correction = self.target_ppm_correction
            if d.correction(handle, ppm_correction) not in (0, -2):
                raise RuntimeError('RTL-SDR rejected the PPM correction')
            self.applied_ppm_correction = ppm_correction
            last_ppm_attempt = ppm_correction
            if d.rate(handle, self.rate) != 0:
                raise RuntimeError('RTL-SDR rejected 240 kS/s')
            center_mhz = self.target_center_mhz
            if d.frequency(handle, round(center_mhz * 1e6)) != 0:
                raise RuntimeError('RTL-SDR tuning failed')
            self.commanded_frequency_mhz = center_mhz
            self.tuned_frequency_mhz = d.get_frequency(handle) / 1e6
            count = d.gains(handle, None)
            values = (C.c_int * count)() if count > 0 else None
            available_gains = list(values) if values is not None and d.gains(handle, values) > 0 else []
            def apply_gain(requested):
                raw = round(requested * 10)
                chosen = min(available_gains, key=lambda value: abs(value - raw)) if available_gains else raw
                if d.gain(handle, chosen) != 0:
                    return False
                self.commanded_gain_db = requested
                self.applied_gain_db = d.get_gain(handle) / 10
                return True
            if d.gain_mode(handle, 1) != 0 or not apply_gain(gain_db):
                raise RuntimeError('RTL-SDR rejected the RF gain setting')
            d.reset(handle)
            raw = C.create_string_buffer(32768)
            got = C.c_int()
            demod = make_demodulator(self.demod_mode, self.rate, self.target_bandwidth_khz)
            shifter = StreamingFrequencyShifter(self.iq_rate)
            self.audio_rate = demod.audio_rate
            audio_filter = StreamingAudioNoiseFilter(self.audio_rate)
            squelch = StreamingSquelch(self.iq_rate, self.audio_rate)
            filter_enabled = self.noise_filter_enabled
            with self.lock:
                self.running = True
            while not self.stop_event.is_set():
                ppm_target = self.target_ppm_correction
                if ppm_target != last_ppm_attempt:
                    last_ppm_attempt = ppm_target
                    if d.correction(handle, ppm_target) in (0, -2):
                        self.applied_ppm_correction = ppm_target
                        self.tuned_frequency_mhz = d.get_frequency(handle) / 1e6
                        if self.error.startswith('RTL-SDR rejected PPM correction'):
                            self.error = ''
                    else:
                        self.error = f'RTL-SDR rejected PPM correction {ppm_target}'
                gain_target = self.target_gain_db
                if abs(gain_target - self.commanded_gain_db) >= .05:
                    apply_gain(gain_target)
                target = self.target_center_mhz
                if abs(target - self.commanded_frequency_mhz) >= 0.00001:
                    if d.frequency(handle, round(target * 1e6)) == 0:
                        self.commanded_frequency_mhz = target
                        self.tuned_frequency_mhz = d.get_frequency(handle) / 1e6
                        demod.reset()
                        shifter.reset()
                        audio_filter.reset()
                        squelch.reset()
                code = d.read(handle, raw, len(raw), C.byref(got))
                if code != 0:
                    raise RuntimeError(f'RTL-SDR read failed ({code}). The device may have disconnected.')
                if got.value < 4:
                    continue
                iq_bytes = np.frombuffer(raw.raw, dtype=np.uint8, count=got.value)
                values = (iq_bytes.astype(np.float32) - 127.5) / 127.5
                iq = values[0::2] + 1j * values[1::2]
                if demod.bandwidth_khz != self.target_bandwidth_khz:
                    demod = make_demodulator(self.demod_mode, self.rate, self.target_bandwidth_khz)
                shifted_iq = shifter.process(iq, digital_shift_hz(
                    self.tuned_frequency_mhz or self.commanded_frequency_mhz,
                    self.target_frequency_mhz, self.demod_mode))
                audio = demod.process(shifted_iq)
                if self.noise_filter_enabled != filter_enabled:
                    filter_enabled = self.noise_filter_enabled
                    audio_filter.reset()
                if filter_enabled and self.demod_mode != 'WFM':
                    audio = audio_filter.process(audio)
                audio = squelch.process(iq, audio, self.target_squelch_dbfs)
                self.signal_level_dbfs = squelch.level_dbfs
                self.squelch_open = squelch.is_open
                sink = self.monitor_sink
                if sink is not None and len(audio):
                    try:
                        sink(audio, self.audio_rate)
                    except Exception:
                        pass
                pcm = (audio * 32767).astype('<i2').tobytes()
                with self.lock:
                    if pcm:
                        self.audio.append(pcm)
                    self.iq = iq_bytes[:16384].tobytes()
                    self.iq_frames.append(self.iq)
        except Exception as exc:
            self.error = str(exc)
        finally:
            with self.lock:
                self.running = False
            d.close(handle)

    def pull_raw(self):
        with self.lock:
            chunks = list(self.audio)
            self.audio.clear()
            frames = list(self.iq_frames)
            self.iq_frames.clear()
            return {'audio': chunks, 'iq': self.iq, 'running': self.running,
                    'error': self.error, 'sample_rate': self.audio_rate,
                    'iq_rate': self.iq_rate, 'iq_frames': frames,
                    'tuned_frequency_mhz': self.tuned_frequency_mhz,
                    'applied_gain_db': self.applied_gain_db,
                    'applied_ppm_correction': self.applied_ppm_correction,
                    'signal_level_dbfs': self.signal_level_dbfs, 'squelch_open': self.squelch_open}
