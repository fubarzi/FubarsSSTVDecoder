"""Native ISS SSTV receiver core."""
from __future__ import annotations

import base64
from collections import deque
import configparser
import ctypes as C
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, urlopen

import numpy as np

from orbit import position, look_angles, footprint_contains
from fm import (StreamingHFDemodulator, StreamingAudioNoiseFilter, StreamingSquelch,
                StreamingFrequencyShifter, digital_shift_hz, HF_MODES, make_demodulator)
from rtl import RTLDriver, RTLReceiver
from satellites import SatelliteCatalog, valid_tle as valid_generic_tle

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / 'settings.json'
INI_FILE = ROOT / 'FubarsSSTVDecoder.ini'
TLE_FILE = ROOT / 'iss.tle.json'
DEFAULT_TLE_FILE = ROOT / 'iss.tle.default.json'
CATALOG_FILE = ROOT / 'satellites.json'
RECEIVED_DIR = ROOT / 'received_sstv'
RECEIVED_DIR.mkdir(exist_ok=True)
TLE_URL = 'https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE'
TLE_SOURCES = (
    ('CelesTrak', TLE_URL, 'text'),
    ('AMSAT', 'https://www.amsat.org/tle/current/nasabare.txt', 'text'),
    ('Where the ISS at', 'https://api.wheretheiss.at/v1/satellites/25544/tles', 'json'),
)
DEFAULT_SETTINGS = {'frequency_mhz': 437.55, 'home_lat': None, 'home_lon': None,
                    'auto_receive': False, 'doppler_enabled': True, 'gain_db': 25.0,
                    'bandwidth_khz': 12.5, 'device_id': '', 'decoder_mode': 'Auto',
                    'ppm_correction': 0,
                    'demod_mode': 'FM', 'antenna_name': '', 'noise_filter_enabled': True,
                    'monitor_volume': 70,
                    'audio_output_name': '',
                    'monitor_enabled': False, 'window_geometry': '', 'window_maximized': False,
                    'pane_positions': {},
                    'thumbnail_cleared_at_ns': 0,
                    'map_zoom': 1.0, 'map_pan_x': 0.0, 'map_pan_y': 0.0, 'selected_tab': 0,
                    'spectrum_floor_dbfs': -105, 'squelch_dbfs': -120,
                    'receiver_center_mhz': None, 'selected_satellite': '25544',
                    'tracked_satellites': ['25544'], 'monitored_satellites': [],
                    'pass_satellites': None}


def safe_receiver_center(frequency_mhz, center_mhz, bandwidth_khz, demod_mode):
    """Keep the selected channel inside the useful part of a 240 kHz IQ capture."""
    frequency = float(frequency_mhz)
    default = frequency + (StreamingHFDemodulator.offset_hz / 1e6
                           if demod_mode in HF_MODES else 0)
    if center_mhz is None:
        return round(default, 6)
    try:
        center = float(center_mhz)
        margin_hz = 0.4 * 240000 - float(bandwidth_khz) * 500 - 2000
        if not (.001 <= center <= 1700 and math.isfinite(center) and
                abs(center - frequency) * 1e6 <= max(0, margin_hz)):
            return round(default, 6)
        return center
    except (TypeError, ValueError):
        return round(default, 6)


def configured_sstv_frequency(settings, catalog_number):
    """Return the user's frequency for one satellite, if it is usable."""
    key = str(catalog_number)
    for field in ('manual_sstv_frequencies', 'sstv_downlink_preferences'):
        try:
            frequency = float(settings.get(field, {}).get(key))
            if math.isfinite(frequency) and .001 <= frequency <= 1700:
                return round(frequency, 6)
        except (TypeError, ValueError, AttributeError):
            pass
    return None


def normalize_auto_monitor(settings):
    """Arm the selected mapped satellite when auto receive has no monitor list."""
    if settings.get('auto_receive') and not settings.get('monitored_satellites'):
        tracked = [str(item) for item in settings.get('tracked_satellites', ['25544'])]
        selected = str(settings.get('selected_satellite', '25544'))
        if tracked:
            settings['monitored_satellites'] = [selected if selected in tracked else tracked[0]]
    return settings


def sdrplay_if_reduction(gain_control):
    """Map the 0–50 slider to SDRplay's inverse 59–20 dB IF reduction."""
    return round(59 - 39 * max(0, min(50, gain_control)) / 50)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temp.replace(path)


def read_ini(path: Path, default):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open('r', encoding='utf-8') as stream:
            parser.read_file(stream)
        return {key: json.loads(value) for section in parser.sections()
                for key, value in parser.items(section)}
    except (OSError, ValueError, configparser.Error):
        return default


def write_ini(path: Path, values):
    sections = {
        'Receiver': ('frequency_mhz', 'gain_db', 'bandwidth_khz', 'device_id',
                     'ppm_correction', 'demod_mode', 'antenna_name',
                     'noise_filter_enabled', 'squelch_dbfs', 'receiver_center_mhz'),
        'Decoder': ('decoder_mode',),
        'Audio output': ('audio_output_name', 'monitor_enabled', 'monitor_volume'),
        'Tracker': ('home_lat', 'home_lon', 'auto_receive', 'doppler_enabled'),
        'Satellites': ('selected_satellite', 'tracked_satellites', 'monitored_satellites',
                       'pass_satellites',
                       'manual_sstv_frequencies', 'sstv_downlink_preferences'),
        'Layout': ('window_geometry', 'window_maximized', 'pane_positions',
                   'map_zoom', 'map_pan_x', 'map_pan_y', 'selected_tab',
                   'spectrum_floor_dbfs', 'thumbnail_cleared_at_ns'),
    }
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    used = set()
    for section, keys in sections.items():
        parser[section] = {key: json.dumps(values[key]) for key in keys if key in values}
        used.update(keys)
    other = {key: json.dumps(value) for key, value in values.items() if key not in used}
    if other:
        parser['Other'] = other
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        parser.write(stream)
    temp.replace(path)


def write_settings(values):
    write_json(SETTINGS_FILE, values)
    write_ini(INI_FILE, values)


def read_settings():
    json_values = read_json(SETTINGS_FILE, None)
    ini_values = read_ini(INI_FILE, None)
    if ini_values is None:
        return json_values or {}
    if json_values is None:
        return ini_values
    return (ini_values if INI_FILE.stat().st_mtime > SETTINGS_FILE.stat().st_mtime
            else json_values)


def valid_tle(lines):
    if len(lines) != 2 or not lines[0].startswith('1 25544') or not lines[1].startswith('2 25544'):
        return False
    for line in lines:
        if len(line) < 69:
            return False
        checksum = sum(int(x) for x in line[:68] if x.isdigit()) + line[:68].count('-')
        if checksum % 10 != int(line[68]):
            return False
    return True


def tle_epoch(lines):
    year = int(lines[0][18:20])
    year += 2000 if year < 57 else 1900
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=float(lines[0][20:32]) - 1)


def valid_orbit(elements):
    return (isinstance(elements, dict) and all(key in elements for key in
            ('NORAD_CAT_ID', 'EPOCH', 'MEAN_MOTION', 'ECCENTRICITY')) or
            isinstance(elements, list) and valid_generic_tle(elements))


def orbit_epoch(elements):
    return (datetime.fromisoformat(elements['EPOCH'].replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
            if isinstance(elements, dict) else tle_epoch(elements))


def parse_iss_tle(raw, format_name):
    if format_name == 'json':
        data = json.loads(raw)
        name = data.get('header') or data.get('name') or 'ISS (ZARYA)'
        lines = [data['line1'].strip(), data['line2'].strip()]
    else:
        rows = [row.strip() for row in raw.splitlines() if row.strip()]
        index = next(i for i, row in enumerate(rows[:-1])
                     if row.startswith('1 25544') and rows[i + 1].startswith('2 25544'))
        name = rows[index - 1] if index else 'ISS (ZARYA)'
        lines = rows[index:index + 2]
    if not valid_tle(lines):
        raise ValueError('TLE checksum or ISS catalog number is invalid')
    return name, lines


def doppler_shift_hz(lines, home_lat, home_lon, frequency_mhz, when=None, current_look=None):
    when = when or datetime.now(timezone.utc)
    current_look = current_look or look_angles(position(lines, when), home_lat, home_lon)
    if not footprint_contains(position(lines, when), home_lat, home_lon):
        return 0.0
    before = look_angles(position(lines, when - timedelta(seconds=1)), home_lat, home_lon)['range_km']
    after = look_angles(position(lines, when + timedelta(seconds=1)), home_lat, home_lon)['range_km']
    return -(after - before) / 2 / 299792.458 * frequency_mhz * 1e6


class Kwargs(C.Structure):
    _fields_ = [('size', C.c_size_t), ('keys', C.POINTER(C.c_char_p)), ('vals', C.POINTER(C.c_char_p))]


class Soapy:
    def __init__(self):
        self.lib = None
        self.dll_dir = None
        self.error = 'SoapySDR runtime not found'
        candidates = [ROOT / 'runtime' / 'bin' / 'SoapySDR.dll', ROOT / 'runtime' / 'bin' / 'libSoapySDR.dll']
        desktop = Path.home() / 'Desktop'
        candidates += [desktop / 'IC-SDR' / 'DATA' / 'runtime' / 'windows-x64' / 'bin' / 'SoapySDR.dll',
                       desktop / "Fubar's AIS Radar" / 'third_party' / 'rx-runtime' / 'bin' / 'libSoapySDR.dll']
        custom = os.environ.get('SOAPY_SDR_DLL')
        if custom:
            candidates.insert(0, Path(custom))
        for path in candidates:
            if not path.is_file():
                continue
            try:
                self.dll_dir = os.add_dll_directory(str(path.parent))
                os.environ['PATH'] = str(path.parent) + os.pathsep + os.environ.get('PATH', '')
                modules = path.parent.parent / 'modules0.8'
                if not modules.is_dir():
                    modules = path.parent.parent / 'lib' / 'SoapySDR' / 'modules0.8'
                if modules.is_dir():
                    os.environ['SOAPY_SDR_PLUGIN_PATH'] = str(modules)
                self.lib = C.CDLL(str(path))
                self.error = ''
                break
            except OSError as exc:
                self.error = str(exc)
        if self.lib:
            self._bind()

    def _fn(self, name, result, *args):
        fn = getattr(self.lib, name)
        fn.restype = result
        fn.argtypes = args
        return fn

    def _bind(self):
        p = C.c_void_p
        self.enumerate = self._fn('SoapySDRDevice_enumerateStrArgs', C.POINTER(Kwargs), C.c_char_p, C.POINTER(C.c_size_t))
        self.clear_list = self._fn('SoapySDRKwargsList_clear', None, C.POINTER(Kwargs), C.c_size_t)
        self.make = self._fn('SoapySDRDevice_makeStrArgs', p, C.c_char_p)
        self.unmake = self._fn('SoapySDRDevice_unmake', C.c_int, p)
        self.last_error = self._fn('SoapySDRDevice_lastError', C.c_char_p)
        self.set_rate = self._fn('SoapySDRDevice_setSampleRate', C.c_int, p, C.c_int, C.c_size_t, C.c_double)
        self.get_rate = self._fn('SoapySDRDevice_getSampleRate', C.c_double, p, C.c_int, C.c_size_t)
        self.set_freq = self._fn('SoapySDRDevice_setFrequency', C.c_int, p, C.c_int, C.c_size_t, C.c_double, C.POINTER(Kwargs))
        self.get_freq = self._fn('SoapySDRDevice_getFrequency', C.c_double, p, C.c_int, C.c_size_t)
        self.set_gain = self._fn('SoapySDRDevice_setGain', C.c_int, p, C.c_int, C.c_size_t, C.c_double)
        self.get_gain = self._fn('SoapySDRDevice_getGain', C.c_double, p, C.c_int, C.c_size_t)
        self.set_gain_element = self._fn('SoapySDRDevice_setGainElement', C.c_int,
                                         p, C.c_int, C.c_size_t, C.c_char_p, C.c_double)
        self.get_gain_element = self._fn('SoapySDRDevice_getGainElement', C.c_double,
                                         p, C.c_int, C.c_size_t, C.c_char_p)
        self.set_gain_mode = self._fn('SoapySDRDevice_setGainMode', C.c_int, p, C.c_int, C.c_size_t, C.c_bool)
        self.set_correction = (self._fn('SoapySDRDevice_setFrequencyCorrection', C.c_int,
                                        p, C.c_int, C.c_size_t, C.c_double)
                               if hasattr(self.lib, 'SoapySDRDevice_setFrequencyCorrection') else None)
        self.list_antennas = self._fn('SoapySDRDevice_listAntennas', C.POINTER(C.c_char_p),
                                      p, C.c_int, C.c_size_t, C.POINTER(C.c_size_t))
        self.set_antenna = self._fn('SoapySDRDevice_setAntenna', C.c_int,
                                    p, C.c_int, C.c_size_t, C.c_char_p)
        self.clear_strings = self._fn('SoapySDRStrings_clear', None,
                                      C.POINTER(C.POINTER(C.c_char_p)), C.c_size_t)
        self.setup = self._fn('SoapySDRDevice_setupStream', p, p, C.c_int, C.c_char_p, C.POINTER(C.c_size_t), C.c_size_t, C.POINTER(Kwargs))
        self.activate = self._fn('SoapySDRDevice_activateStream', C.c_int, p, p, C.c_int, C.c_longlong, C.c_size_t)
        self.read = self._fn('SoapySDRDevice_readStream', C.c_int, p, p, C.POINTER(p), C.c_size_t, C.POINTER(C.c_int), C.POINTER(C.c_longlong), C.c_long)
        self.deactivate = self._fn('SoapySDRDevice_deactivateStream', C.c_int, p, p, C.c_int, C.c_longlong)
        self.close = self._fn('SoapySDRDevice_closeStream', C.c_int, p, p)

    def devices(self):
        if not self.lib:
            return []
        count = C.c_size_t()
        ptr = self.enumerate(b'', C.byref(count))
        found = []
        try:
            for i in range(count.value):
                row = ptr[i]
                values = {row.keys[j].decode(errors='replace'): row.vals[j].decode(errors='replace') for j in range(row.size)}
                args = ','.join(f'{k}={v}' for k, v in values.items() if k not in ('label',))
                found.append({'id': args, 'label': values.get('label') or values.get('driver') or args,
                              'driver': values.get('driver', ''), 'hardware': values.get('hardware', '')})
        finally:
            if ptr:
                self.clear_list(ptr, count.value)
        return found

    def antennas(self, device_id):
        if not self.lib:
            return []
        dev = self.make(device_id.encode())
        if not dev:
            return []
        try:
            count = C.c_size_t()
            names = self.list_antennas(dev, 1, 0, C.byref(count))
            try:
                return [names[index].decode(errors='replace') for index in range(count.value)] if names else []
            finally:
                if names:
                    self.clear_strings(C.byref(names), count.value)
        finally:
            self.unmake(dev)


class Receiver:
    def __init__(self, soapy):
        self.soapy = soapy
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
        self.target_gain_db = 25.0
        self.target_ppm_correction = 0
        self.applied_ppm_correction = None
        self.commanded_gain_db = None
        self.applied_gain_db = None
        self.gain_detail = ''
        self.demod_mode = 'FM'
        self.antenna_name = ''
        self.noise_filter_enabled = True
        self.target_squelch_dbfs = -120.0
        self.signal_level_dbfs = -120.0
        self.squelch_open = True
        self.monitor_sink = None

    def start(self, device_id, frequency_mhz, gain_db, bandwidth_khz=12.5,
              ppm_correction=0, demod_mode='FM', antenna_name='', noise_filter_enabled=True,
              squelch_dbfs=-120, center_frequency_mhz=None):
        self.stop()
        if not self.soapy.lib:
            raise RuntimeError(self.soapy.error)
        if not device_id:
            raise RuntimeError('Select a connected SDR first')
        self.stop_event.clear()
        self.error = ''
        self.target_frequency_mhz = frequency_mhz
        self.target_center_mhz = (float(center_frequency_mhz) if center_frequency_mhz is not None else
                                  frequency_mhz + (StreamingHFDemodulator.offset_hz / 1e6
                                                   if demod_mode in HF_MODES else 0))
        self.commanded_frequency_mhz = None
        self.tuned_frequency_mhz = None
        self.target_bandwidth_khz = bandwidth_khz
        self.target_gain_db = float(gain_db)
        self.target_ppm_correction = int(ppm_correction)
        self.applied_ppm_correction = None
        self.commanded_gain_db = None
        self.applied_gain_db = None
        self.gain_detail = ''
        self.demod_mode = demod_mode
        self.antenna_name = antenna_name
        self.noise_filter_enabled = bool(noise_filter_enabled)
        self.target_squelch_dbfs = float(squelch_dbfs)
        self.signal_level_dbfs = -120.0
        self.squelch_open = True
        self.thread = threading.Thread(target=self._run,
                                       args=(device_id, frequency_mhz, gain_db, antenna_name), daemon=True)
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

    def _run(self, device_id, frequency_mhz, gain_db, antenna_name):
        s = self.soapy
        dev = s.make(device_id.encode())
        stream = None
        if not dev:
            self.error = (s.last_error() or b'Device open failed').decode(errors='replace')
            return
        try:
            ppm_correction = self.target_ppm_correction
            if ppm_correction:
                if not s.set_correction or s.set_correction(dev, 1, 0, ppm_correction) != 0:
                    raise RuntimeError('Selected SDR does not support PPM correction')
            self.applied_ppm_correction = ppm_correction
            last_ppm_attempt = ppm_correction
            chosen = None
            rates = ((250000, 240000, 256000, 1024000, 2000000, 2048000, 2400000)
                     if 'sdrplay' in device_id.lower() else
                     (240000, 250000, 256000, 1024000, 2000000, 2048000, 2400000))
            for rate in rates:
                if s.set_rate(dev, 1, 0, rate) == 0:
                    actual = s.get_rate(dev, 1, 0)
                    if actual > 0 and abs(actual - rate) / rate < .02:
                        chosen = actual
                        break
            if not chosen:
                raise RuntimeError('SDR does not accept a supported sample rate')
            self.rate = chosen
            if antenna_name and s.set_antenna(dev, 1, 0, antenna_name.encode()) != 0:
                raise RuntimeError('Antenna input unavailable: ' + antenna_name)
            center_mhz = self.target_center_mhz
            if s.set_freq(dev, 1, 0, center_mhz * 1e6, None) != 0:
                raise RuntimeError('Tuning failed: ' + (s.last_error() or b'').decode(errors='replace'))
            self.commanded_frequency_mhz = center_mhz
            self.tuned_frequency_mhz = s.get_freq(dev, 1, 0) / 1e6
            if s.set_gain_mode(dev, 1, 0, False) != 0:
                raise RuntimeError('SDR rejected manual gain mode')
            is_sdrplay = 'sdrplay' in device_id.lower()
            def apply_gain(requested):
                if is_sdrplay:
                    # SDRplay IFGR is gain reduction: lower values mean more gain.
                    ifgr = sdrplay_if_reduction(requested)
                    if s.set_gain_element(dev, 1, 0, b'IFGR', ifgr) != 0:
                        return False
                    actual = s.get_gain_element(dev, 1, 0, b'IFGR')
                    self.applied_gain_db = None
                    self.gain_detail = f'SDRplay IF reduction: {actual:.0f} dB (lower = more gain)'
                else:
                    if s.set_gain(dev, 1, 0, requested) != 0:
                        return False
                    self.applied_gain_db = s.get_gain(dev, 1, 0)
                    self.gain_detail = ''
                self.commanded_gain_db = requested
                return True
            if not apply_gain(gain_db):
                raise RuntimeError('SDR rejected the RF gain setting')
            stream = s.setup(dev, 1, b'CF32', None, 0, None)
            if not stream:
                raise RuntimeError('IQ stream failed: ' + (s.last_error() or b'').decode(errors='replace'))
            if s.activate(dev, stream, 0, 0, 0) != 0:
                raise RuntimeError('IQ stream activation failed')
            if antenna_name and s.set_antenna(dev, 1, 0, antenna_name.encode()) != 0:
                raise RuntimeError('Antenna input could not be selected after stream activation')
            buf = (C.c_float * (16384 * 2))()
            buffs = (C.c_void_p * 1)(C.cast(buf, C.c_void_p))
            flags = C.c_int()
            stamp = C.c_longlong()
            stride = max(1, round(chosen / 240000))
            self.iq_rate = chosen / stride
            demod = make_demodulator(self.demod_mode, self.iq_rate, self.target_bandwidth_khz)
            shifter = StreamingFrequencyShifter(self.iq_rate)
            self.audio_rate = demod.audio_rate
            audio_filter = StreamingAudioNoiseFilter(self.audio_rate)
            squelch = StreamingSquelch(self.iq_rate, self.audio_rate)
            filter_enabled = self.noise_filter_enabled
            iq_tail = np.empty(0, dtype=np.complex64)
            with self.lock:
                self.running = True
            while not self.stop_event.is_set():
                ppm_target = self.target_ppm_correction
                if ppm_target != last_ppm_attempt:
                    last_ppm_attempt = ppm_target
                    if s.set_correction and s.set_correction(dev, 1, 0, ppm_target) == 0:
                        self.applied_ppm_correction = ppm_target
                        self.tuned_frequency_mhz = s.get_freq(dev, 1, 0) / 1e6
                        if self.error.startswith('Selected SDR rejected PPM correction'):
                            self.error = ''
                    else:
                        self.error = f'Selected SDR rejected PPM correction {ppm_target}'
                gain_target = self.target_gain_db
                if abs(gain_target - self.commanded_gain_db) >= .05:
                    if not apply_gain(gain_target):
                        self.error = 'SDR rejected RF gain ' + str(gain_target) + ' dB'
                        self.commanded_gain_db = gain_target
                target = self.target_center_mhz
                if abs(target - self.commanded_frequency_mhz) >= 0.00001:
                    if s.set_freq(dev, 1, 0, target * 1e6, None) == 0:
                        self.commanded_frequency_mhz = target
                        self.tuned_frequency_mhz = s.get_freq(dev, 1, 0) / 1e6
                        demod.reset()
                        shifter.reset()
                        audio_filter.reset()
                        squelch.reset()
                        iq_tail = np.empty(0, dtype=np.complex64)
                got = s.read(dev, stream, buffs, 16384, C.byref(flags), C.byref(stamp), 250000)
                if got < 0:
                    if got == -1:
                        continue
                    raise RuntimeError('SDR read error: ' + str(got))
                if not got:
                    continue
                values = np.frombuffer(buf, dtype=np.float32, count=got * 2)
                iq = (values[0::2] + 1j * values[1::2]).astype(np.complex64)
                if stride > 1:
                    joined = np.concatenate((iq_tail, iq))
                    groups = len(joined) // stride
                    iq = joined[:groups * stride].reshape(groups, stride).mean(axis=1)
                    iq_tail = joined[groups * stride:].copy()
                if not len(iq):
                    continue
                if demod.bandwidth_khz != self.target_bandwidth_khz:
                    demod = make_demodulator(self.demod_mode, self.iq_rate, self.target_bandwidth_khz)
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
                out = (audio * 32767).astype('<i2').tobytes()
                display = np.stack((iq[:8192].real, iq[:8192].imag), axis=1)
                iq_out = np.clip((display + 1) * 127.5, 0, 255).astype(np.uint8).tobytes()
                with self.lock:
                    if out:
                        self.audio.append(out)
                    self.iq = iq_out
                    self.iq_frames.append(self.iq)
        except Exception as exc:
            self.error = str(exc)
        finally:
            with self.lock:
                self.running = False
            if stream:
                try:
                    s.deactivate(dev, stream, 0, 0)
                    s.close(dev, stream)
                except Exception:
                    pass
            s.unmake(dev)

    def pull(self):
        with self.lock:
            chunks = list(self.audio)
            self.audio.clear()
            iq = self.iq
            return {'running': self.running, 'error': self.error, 'sample_rate': self.audio_rate,
                    'tuned_frequency_mhz': self.tuned_frequency_mhz,
                    'iq_rate': self.iq_rate, 'audio': [base64.b64encode(x).decode() for x in chunks],
                    'iq': base64.b64encode(iq).decode() if iq else ''}

    def pull_raw(self):
        with self.lock:
            chunks = list(self.audio)
            self.audio.clear()
            frames = list(self.iq_frames)
            self.iq_frames.clear()
            return {'running': self.running, 'error': self.error, 'sample_rate': self.audio_rate,
                    'tuned_frequency_mhz': self.tuned_frequency_mhz, 'iq_rate': self.iq_rate,
                    'audio': chunks, 'iq': self.iq, 'iq_frames': frames,
                    'applied_gain_db': self.applied_gain_db, 'gain_detail': self.gain_detail,
                    'applied_ppm_correction': self.applied_ppm_correction,
                    'signal_level_dbfs': self.signal_level_dbfs, 'squelch_open': self.squelch_open}


class HybridReceiver:
    def __init__(self, soapy, rtl):
        self.soapy_receiver = Receiver(soapy)
        self.rtl_receiver = RTLReceiver(rtl)
        self.active = self.rtl_receiver

    @property
    def running(self):
        return self.active.running

    @property
    def error(self):
        return self.active.error

    @error.setter
    def error(self, value):
        self.active.error = value

    @property
    def target_frequency_mhz(self):
        return self.active.target_frequency_mhz

    @target_frequency_mhz.setter
    def target_frequency_mhz(self, value):
        self.active.target_frequency_mhz = value

    @property
    def target_center_mhz(self):
        return self.active.target_center_mhz

    @target_center_mhz.setter
    def target_center_mhz(self, value):
        self.active.target_center_mhz = float(value)

    @property
    def target_bandwidth_khz(self):
        return self.active.target_bandwidth_khz

    @target_bandwidth_khz.setter
    def target_bandwidth_khz(self, value):
        self.active.target_bandwidth_khz = value

    @property
    def target_gain_db(self):
        return self.active.target_gain_db

    @target_gain_db.setter
    def target_gain_db(self, value):
        self.active.target_gain_db = value

    @property
    def target_ppm_correction(self):
        return self.active.target_ppm_correction

    @target_ppm_correction.setter
    def target_ppm_correction(self, value):
        self.active.target_ppm_correction = int(value)

    @property
    def noise_filter_enabled(self):
        return self.active.noise_filter_enabled

    @noise_filter_enabled.setter
    def noise_filter_enabled(self, value):
        self.active.noise_filter_enabled = bool(value)

    @property
    def target_squelch_dbfs(self):
        return self.active.target_squelch_dbfs

    @target_squelch_dbfs.setter
    def target_squelch_dbfs(self, value):
        self.active.target_squelch_dbfs = float(value)

    @property
    def monitor_sink(self):
        return self.active.monitor_sink

    @monitor_sink.setter
    def monitor_sink(self, value):
        self.soapy_receiver.monitor_sink = value
        self.rtl_receiver.monitor_sink = value

    def start(self, device_id, frequency_mhz, gain_db, bandwidth_khz=12.5,
              ppm_correction=0, demod_mode='FM', antenna_name='', noise_filter_enabled=True,
              squelch_dbfs=-120, center_frequency_mhz=None):
        self.stop()
        self.active = self.rtl_receiver if device_id.startswith('rtl:') else self.soapy_receiver
        center_frequency_mhz = safe_receiver_center(frequency_mhz, center_frequency_mhz,
                                                    bandwidth_khz, demod_mode)
        self.active.start(device_id, frequency_mhz, gain_db, bandwidth_khz,
                          ppm_correction, demod_mode, antenna_name, noise_filter_enabled,
                          squelch_dbfs, center_frequency_mhz)

    def stop(self):
        self.active.stop()

    def pull_raw(self):
        return self.active.pull_raw()


class App:
    def __init__(self):
        saved = read_settings()
        self.settings = normalize_auto_monitor({**DEFAULT_SETTINGS, **saved})
        self.settings.pop('audio_input_name', None)
        self.settings.pop('audio_input_active', None)
        held = self.settings.get('receiver_center_mhz')
        if held is not None:
            safe = safe_receiver_center(self.settings['frequency_mhz'], held,
                                        self.settings['bandwidth_khz'], self.settings['demod_mode'])
            if safe != held:
                self.settings['receiver_center_mhz'] = None
        self.tle = read_json(TLE_FILE, read_json(DEFAULT_TLE_FILE,
                             {'name': 'ISS (ZARYA)', 'lines': [], 'updated': None}))
        self.catalog = SatelliteCatalog(CATALOG_FILE)
        self.soapy = Soapy()
        self.rtl = RTLDriver()
        self.receiver = HybridReceiver(self.soapy, self.rtl)
        self.lock = threading.Lock()
        self.tle_update_lock = threading.Lock()
        self.tle_error = ''
        self.tle_check_status = 'Checking online TLE…'
        self.tle_checked_at = None
        self.auto_started = False
        self.auto_active_satellite = None
        self.auto_active_frequency_mhz = None
        self.last_auto_attempt = 0.0
        self.tracker_stopped = False
        threading.Thread(target=self.update_tle, daemon=True).start()
        threading.Thread(target=self.refresh_catalog, daemon=True).start()
        threading.Thread(target=self.tracker_loop, daemon=True).start()

    def refresh_catalog(self):
        return self.catalog.refresh()

    def selected_tle(self, settings=None, catalog_number=None):
        selected = str(catalog_number or (settings or self.settings).get('selected_satellite', '25544'))
        if selected == '25544':
            return self.tle.copy()
        record = self.catalog.get(selected)
        return ({'name': record['OBJECT_NAME'], 'lines': record.get('lines', record),
                 'updated': self.catalog.checked_at}
                if record else {'name': f'Satellite {selected}', 'lines': [], 'updated': None})

    def update_selected_tle(self):
        if str(self.settings.get('selected_satellite', '25544')) == '25544':
            return self.update_tle()
        return self.refresh_catalog()

    def devices(self):
        direct = self.rtl.devices()
        try:
            other = [d for d in self.soapy.devices() if d['driver'] != 'rtlsdr']
            for device in other:
                try:
                    device['antennas'] = self.soapy.antennas(device['id'])
                except Exception:
                    device['antennas'] = []
                if not device['antennas'] and 'rspdx' in (device['label'] + device.get('hardware', '')).lower():
                    device['antennas'] = ['Antenna A', 'Antenna B', 'Antenna C']
        except Exception:
            other = []
        return direct + other

    def update_tle(self):
        with self.tle_update_lock:
            return self._update_tle()

    def _update_tle(self):
        with self.lock:
            cached = self.tle.copy()
        cached_lines = cached.get('lines', [])
        latest_epoch = tle_epoch(cached_lines) if valid_tle(cached_lines) else None
        errors = []
        valid_online = False
        checked_source = None
        for source, url, format_name in TLE_SOURCES:
            try:
                req = Request(url, headers={'User-Agent': 'FubarsSSTVDecoder/1.0'})
                with urlopen(req, timeout=10) as response:
                    raw = response.read(100000).decode('utf-8', errors='replace')
                name, lines = parse_iss_tle(raw, format_name)
                valid_online = True
                if checked_source is None:
                    checked_source = source
                epoch = tle_epoch(lines)
                if latest_epoch is not None and epoch <= latest_epoch:
                    continue
                new = {'name': name, 'lines': lines,
                       'updated': datetime.now(timezone.utc).isoformat(), 'source': source}
                write_json(TLE_FILE, new)
                with self.lock:
                    self.tle = new
                    self.tle_error = ''
                    self.tle_checked_at = datetime.now(timezone.utc).isoformat()
                    self.tle_check_status = f'Updated from {source}'
                return f'ISS TLE updated from {source}; epoch {epoch:%Y-%m-%d %H:%M} UTC'
            except Exception as exc:
                errors.append(f'{source}: {exc}')
        if latest_epoch is not None:
            with self.lock:
                self.tle_error = ''
                self.tle_checked_at = datetime.now(timezone.utc).isoformat()
                self.tle_check_status = (f'Already latest · checked {checked_source}' if valid_online
                                         else 'Online check failed · using saved TLE')
            return (f'ISS TLE already latest · epoch {latest_epoch:%Y-%m-%d %H:%M} UTC'
                    if valid_online else 'Online TLE check failed · using saved TLE: ' + '; '.join(errors))
        message = '; '.join(errors) or 'No valid ISS TLE available'
        with self.lock:
            self.tle_error = message
            self.tle_checked_at = datetime.now(timezone.utc).isoformat()
            self.tle_check_status = 'TLE update failed'
        return 'ISS TLE update failed: ' + message

    def orbit(self):
        with self.lock:
            settings = self.settings.copy()
            tle = self.selected_tle(settings)
            error = self.tle_error
            check_status = self.tle_check_status
            checked_at = self.tle_checked_at
        if str(settings.get('selected_satellite', '25544')) != '25544':
            error = '' if valid_orbit(tle.get('lines', [])) else 'Selected satellite is not in the cached catalog'
            check_status = self.catalog.status
            checked_at = self.catalog.checked_at.isoformat() if self.catalog.checked_at else None
        result = {'tle': tle, 'tle_error': error, 'tle_check_status': check_status,
                  'tle_checked_at': checked_at, 'iss': None, 'look': None,
                  'track': [], 'doppler_hz': None, 'tracked': []}
        if valid_orbit(tle.get('lines', [])):
            try:
                now = datetime.now(timezone.utc)
                sat = position(tle['lines'], now)
                result['iss'] = sat
                if settings['home_lat'] is not None and settings['home_lon'] is not None:
                    result['look'] = look_angles(sat, settings['home_lat'], settings['home_lon'])
                    result['doppler_hz'] = doppler_shift_hz(tle['lines'], settings['home_lat'],
                                                            settings['home_lon'], settings['frequency_mhz'],
                                                            now, result['look'])
                result['track'] = [position(tle['lines'], now + timedelta(minutes=m)) for m in range(-45, 46, 3)]
            except Exception as exc:
                result['tle_error'] = str(exc)
        for catalog_number in settings.get('tracked_satellites', ['25544']):
            item = self.selected_tle(settings, catalog_number)
            if valid_orbit(item.get('lines', [])):
                try:
                    result['tracked'].append({'id': str(catalog_number), 'name': item['name'],
                                              'position': (result['iss'] if str(catalog_number) ==
                                                           str(settings.get('selected_satellite', '25544')) and result['iss']
                                                           else position(item['lines']))})
                except Exception:
                    pass
        return result

    def tracker_loop(self):
        while not getattr(self, 'tracker_stopped', False):
            try:
                with self.lock:
                    cfg = self.settings.copy()
                monitored = [str(item) for item in cfg.get('monitored_satellites', [])
                             if str(item) in [str(value) for value in cfg.get('tracked_satellites', [])]]
                visible = []
                armed = (cfg['demod_mode'] == 'FM' and cfg['auto_receive'] and monitored and
                         cfg['home_lat'] is not None and cfg['home_lon'] is not None)
                if armed:
                    for catalog_number in monitored:
                        if configured_sstv_frequency(cfg, catalog_number) is None:
                            continue
                        item = self.selected_tle(cfg, catalog_number)
                        if not valid_orbit(item.get('lines', [])):
                            continue
                        try:
                            if footprint_contains(position(item['lines']), cfg['home_lat'], cfg['home_lon']):
                                visible.append(catalog_number)
                        except Exception:
                            continue
                target = visible[0] if visible else None
                if self.auto_started and (self.auto_active_satellite not in visible or
                        (not self.receiver.running and time.time() - self.last_auto_attempt > 10)):
                    self.receiver.stop()
                    self.auto_started = False
                    self.auto_active_satellite = None
                    self.auto_active_frequency_mhz = None
                if target and not self.receiver.running and cfg['device_id']:
                    frequency = configured_sstv_frequency(cfg, target)
                    if frequency is not None and (target != getattr(self, 'last_auto_attempt_satellite', None)
                                                  or time.time() - self.last_auto_attempt > 30):
                        self.last_auto_attempt = time.time()
                        self.last_auto_attempt_satellite = target
                        self.receiver.start(cfg['device_id'], frequency, cfg['gain_db'],
                                            cfg['bandwidth_khz'], cfg['ppm_correction'],
                                            'FM', cfg['antenna_name'], cfg['noise_filter_enabled'],
                                            cfg['squelch_dbfs'], None)
                        self.auto_started = True
                        self.auto_active_satellite = target
                        self.auto_active_frequency_mhz = frequency
                        with self.lock:
                            self.settings['selected_satellite'] = target
                            self.settings['frequency_mhz'] = frequency
                            self.settings['receiver_center_mhz'] = None
                            write_settings(self.settings)
                        cfg = self.settings.copy()
                if self.receiver.running or self.auto_started:
                    satellite_id = str(cfg.get('selected_satellite', '25544'))
                    tle = self.selected_tle(cfg, satellite_id)
                    frequency = cfg['frequency_mhz']
                    shift = 0.0
                    if (cfg['demod_mode'] == 'FM' and cfg['doppler_enabled'] and cfg['home_lat'] is not None and
                            cfg['home_lon'] is not None and valid_orbit(tle.get('lines', []))):
                        now = datetime.now(timezone.utc)
                        current = look_angles(position(tle['lines'], now), cfg['home_lat'], cfg['home_lon'])
                        shift = doppler_shift_hz(tle['lines'], cfg['home_lat'], cfg['home_lon'],
                                                 frequency, now, current)
                    self.receiver.target_frequency_mhz = frequency + shift / 1e6
            except Exception as exc:
                self.receiver.error = str(exc)
            time.sleep(3)


