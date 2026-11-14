"""Meaningful native decoder and pass-path checks."""
from __future__ import annotations

import math
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np

import core
from core import TLE_FILE, doppler_shift_hz, read_json, sdrplay_if_reduction
from desktop import pass_paths, mapped_ground_paths, footprint_ring
from fm import (StreamingFMDemodulator, StreamingWFMDemodulator, StreamingHFDemodulator,
                StreamingAudioNoiseFilter, StreamingSquelch, StreamingFrequencyShifter,
                digital_shift_hz, make_demodulator)
from orbit import position, next_home_passes, next_mapped_home_passes, look_angles, footprint_contains
from sstv import MODES, SSTVDecoder, StreamingToneTracker
from rtl import RTLReceiver
from sgp4.api import Satrec
from sgp4.exporter import export_omm


def signal_for_mode(name):
    rate, phase = 24000, 0.0
    parts = []
    def tone(hz, ms):
        nonlocal phase
        count = round(ms * rate / 1000)
        angles = phase + np.arange(1, count + 1) * (2 * math.pi * hz / rate)
        parts.append((np.sin(angles) * .8).astype(np.float32))
        phase = float(angles[-1]) % (2 * math.pi)
    vis, width, height, sync, porch, scan, pair = MODES[name]
    for hz, ms in ((1900, 300), (1200, 10), (1900, 300), (1200, 30)):
        tone(hz, ms)
    ones = 0
    for bit in range(7):
        value = (vis >> bit) & 1
        ones += value
        tone(1100 if value else 1300, 30)
    tone(1100 if ones % 2 else 1300, 30)
    tone(1200, 30)
    if name == 'Robot36':
        for line in range(2):
            for hz, ms in ((1200, 9), (1500, 3), (1750 + line * 300, 88),
                           (2300 if line else 1500, 4.5), (1900, 1.5), (1900, 44)):
                tone(hz, ms)
    else:
        tone(1200, 20)
        tone(1500, porch)
        for level in (80, 128, 128, 180):
            tone(1500 + 800 * level / 255, scan)
    tone(1900, 30)
    return np.concatenate(parts)


def feed(decoder, signal):
    for index in range(0, len(signal), 4096):
        decoder.add_audio(signal[index:index+4096], 24000)


def test_live_rtl_ppm_correction():
    class Driver:
        def __init__(self):
            self.corrections = []
            self.reads = 0

        def open(self, handle, index):
            return 0

        def correction(self, handle, value):
            self.corrections.append(value)
            return 0

        def rate(self, handle, value):
            return 0

        def frequency(self, handle, value):
            return 0

        def get_frequency(self, handle):
            return 437550000

        def gains(self, handle, values):
            return 0

        def gain_mode(self, handle, value):
            return 0

        def gain(self, handle, value):
            return 0

        def get_gain(self, handle):
            return 300

        def reset(self, handle):
            return 0

        def read(self, handle, raw, length, got):
            self.reads += 1
            got._obj.value = 0
            if self.reads == 1:
                receiver.target_ppm_correction = -11
            else:
                receiver.stop_event.set()
            return 0

        def close(self, handle):
            return 0

    driver = Driver()
    receiver = RTLReceiver(driver)
    receiver.target_ppm_correction = 7
    receiver._run(0, 30)
    assert driver.corrections == [7, -11]
    assert receiver.pull_raw()['applied_ppm_correction'] == -11


def test_decoder():
    for name in MODES:
        decoder = SSTVDecoder()
        feed(decoder, signal_for_mode(name))
        assert decoder.mode == name, (name, decoder.mode)
        assert decoder.next_pair >= 1, name
        assert decoder.image.shape[:2] == (MODES[name][2], MODES[name][1])
    decoder = SSTVDecoder()
    feed(decoder, np.concatenate((signal_for_mode('PD120'), signal_for_mode('Robot36'))))
    assert decoder.mode == 'Robot36'


def test_vis_needs_full_leader():
    signal = signal_for_mode('Robot36')
    for index, (name, spec) in enumerate(MODES.items()):
        clean = signal_for_mode(name)
        noisy = clean + np.random.default_rng(index + 20).normal(0, .25, len(clean)).astype(np.float32)
        valid = SSTVDecoder()
        feed(valid, noisy)
        assert valid.last_vis == spec[0], (name, valid.last_vis)
    truncated_header = np.concatenate((np.zeros(24000, dtype=np.float32),
                                       signal[round(.57 * 24000):]))
    events = []
    decoder = SSTVDecoder(events.append)
    feed(decoder, truncated_header)
    assert decoder.mode is None
    assert not any(event['type'] == 'vis' for event in events)


def test_pass_paths():
    lines = read_json(TLE_FILE, {})['lines']
    current, upcoming = pass_paths(lines)
    assert len(current) == 241 and len(upcoming) == 241
    assert len({round(sat['lon']) for sat in current}) > 120
    assert current[-1]['time'] < upcoming[-1]['time']
    assert current[-1]['time'] == upcoming[0]['time']
    crossing = datetime.fromisoformat(upcoming[0]['time'].replace('Z', '+00:00'))
    before_current, before_next = pass_paths(lines, crossing - timedelta(seconds=1))
    after_current, after_next = pass_paths(lines, crossing + timedelta(seconds=1))
    def seconds_apart(a, b):
        return abs((datetime.fromisoformat(a.replace('Z', '+00:00')) -
                    datetime.fromisoformat(b.replace('Z', '+00:00'))).total_seconds())
    assert seconds_apart(before_current[0]['time'], current[0]['time']) < .01
    assert seconds_apart(before_next[0]['time'], after_current[0]['time']) < .01
    assert after_current[-1]['time'] == after_next[0]['time']


def test_mapped_paths_and_rings():
    lines = read_json(TLE_FILE, {})['lines']
    now = datetime.now(timezone.utc)
    paths = mapped_ground_paths([{'id': '25544', 'lines': lines}], now)
    past, future = paths['25544']
    assert len(past) == len(future) == 16
    assert past[-1] == future[0]
    assert datetime.fromisoformat(past[0]['time'].replace('Z', '+00:00')) < now
    assert datetime.fromisoformat(future[-1]['time'].replace('Z', '+00:00')) > now
    ring = footprint_ring(position(lines, now))
    assert len(ring) == 121
    assert all(-90 <= point['lat'] <= 90 and -180 <= point['lon'] <= 180 for point in ring)


def test_home_passes():
    lines = read_json(TLE_FILE, {})['lines']
    now = datetime.now(timezone.utc)
    passes = next_home_passes(lines, -33.8688, 151.2093, now)
    assert len(passes) == 4
    assert all(item['aos'] < item['peak'] < item['los'] for item in passes)
    assert all(item['aos'] > now for item in passes)
    assert all(a['los'] < b['aos'] for a, b in zip(passes, passes[1:]))
    assert all(look_angles(position(lines, item['peak']), -33.8688, 151.2093)['elevation_deg'] > 0
               for item in passes)
    shifted = next_home_passes(lines, -33.8688, 151.2093,
                               passes[0]['aos'] + timedelta(seconds=1))
    assert abs((shifted[0]['aos'] - passes[0]['aos']).total_seconds()) < .01
    assert shifted[0]['los'] > passes[0]['aos'] + timedelta(seconds=1)
    after = next_home_passes(lines, -33.8688, 151.2093,
                             passes[0]['los'] + timedelta(seconds=1))
    assert abs((after[0]['aos'] - passes[1]['aos']).total_seconds()) < .01


def test_mapped_home_passes_are_merged():
    now = datetime.now(timezone.utc)
    sources = [{'id': 'A', 'name': 'Alpha', 'lines': ['A']},
               {'id': 'B', 'name': 'Bravo', 'lines': ['B']}]
    def prediction(minutes):
        return {'aos': now + timedelta(minutes=minutes),
                'los': now + timedelta(minutes=minutes + 5),
                'peak': now + timedelta(minutes=minutes + 2),
                'max_elevation_deg': 30}
    def fake_passes(lines, *_args, **_kwargs):
        return ([prediction(10), prediction(40), prediction(70), prediction(100)] if
                lines == ['A'] else
                [prediction(20), prediction(30), prediction(50), prediction(80)])
    with patch('orbit.next_home_passes', side_effect=fake_passes):
        passes = next_mapped_home_passes(sources, 0, 0, now)
    assert [item['satellite_name'] for item in passes] == ['Alpha', 'Bravo', 'Bravo', 'Alpha']
    assert [round((item['aos'] - now).total_seconds() / 60) for item in passes] == [10, 20, 30, 40]


def test_wfm_and_squelch():
    rate = 240000
    times = np.arange(rate) / rate
    modulation = 60000 * np.sin(2 * np.pi * 1000 * times) + 5000 * np.sin(2 * np.pi * 19000 * times)
    iq = np.exp(1j * np.cumsum(modulation) * (2 * np.pi / rate)).astype(np.complex64)
    reference = StreamingWFMDemodulator(rate, 200).process(iq)
    decoder = make_demodulator('WFM', rate, 200)
    audio = np.concatenate([decoder.process(iq[i:i+4096]) for i in range(0, len(iq), 4096)])
    assert len(audio) == len(reference) == 48000
    assert np.max(np.abs(audio - reference)) < .01
    useful = audio[4800:]
    spectrum = np.abs(np.fft.rfft(useful * np.hanning(len(useful))))
    frequencies = np.fft.rfftfreq(len(useful), 1 / 48000)
    tone = spectrum[np.argmin(abs(frequencies - 1000))]
    pilot = spectrum[np.argmin(abs(frequencies - 19000))]
    assert tone > 20 * pilot
    assert np.sqrt(np.mean(useful ** 2)) > .1
    gate = StreamingSquelch(rate, 48000)
    quiet = np.zeros(24000, dtype=np.complex64)
    speaker = np.ones(4800, dtype=np.float32)
    for _ in range(3):
        gated = gate.process(quiet, speaker, -40)
    assert not gate.is_open and np.max(gated) == 0
    assert gate.process(iq[:24000], speaker, -40)[-1] == 1
    assert gate.is_open
    assert np.all(gate.process(quiet, speaker, -120) == 1)


def test_doppler_only_in_range():
    lines = read_json(TLE_FILE, {})['lines']
    now = datetime.now(timezone.utc)
    iss = position(lines, now)
    opposite_lon = (iss['lon'] + 360) % 360 - 180
    assert doppler_shift_hz(lines, -iss['lat'], opposite_lon, 437.55, now) == 0
    ahead = position(lines, now + timedelta(minutes=2))
    assert abs(doppler_shift_hz(lines, ahead['lat'], ahead['lon'], 437.55, now)) > 1000


def test_sharp_tone_transition():
    rate, length = 24000, 240
    first = 2 * math.pi * 1500 * np.arange(1, length + 1) / rate
    second = first[-1] + 2 * math.pi * 2300 * np.arange(1, length + 1) / rate
    audio = np.sin(np.r_[first, second]).astype(np.float32)
    audio += np.random.default_rng(0).normal(0, .01, len(audio)).astype(np.float32)
    decoder = SSTVDecoder()
    decoder.add_audio(audio, rate)
    left, right = decoder._channels(np.array([9.75, 10.25]))
    assert left < 60 and right > 210, (left, right)


def test_noisy_sstv_tone_detail():
    rng = np.random.default_rng(4)
    levels = rng.integers(0, 256, 120)
    tones = 1500 + 800 * levels / 255
    phase = np.cumsum(np.repeat(tones, 120)) * (2 * math.pi / 24000)
    audio = (.35 * np.sin(phase) + rng.normal(0, .12, len(phase))).astype(np.float32)
    decoder = SSTVDecoder()
    for index in range(0, len(audio), 1000):
        decoder.add_audio(audio[index:index + 1000], 24000)
    centers = np.arange(len(levels)) * 5 + 2.5
    clearer = decoder._channels(centers)
    crossings = np.asarray(decoder.cross)
    indexes = np.searchsorted(crossings, centers * 24)
    original = np.clip(np.rint((24000 / (crossings[indexes] - crossings[indexes - 1])
                                 - 1500) * 255 / 800), 0, 255)
    assert np.mean(np.abs(clearer - levels)) < .5 * np.mean(np.abs(original - levels))
    chunked = SSTVDecoder()
    for index in range(0, len(audio), 4096):
        chunked.add_audio(audio[index:index + 4096], 24000)
    assert np.array_equal(clearer, chunked._channels(centers))


def test_sharp_sstv_edges():
    frequencies = np.r_[np.full(2400, 1500.0), np.full(2400, 2300.0)]
    audio = np.sin(2 * np.pi * np.cumsum(frequencies) / 24000).astype(np.float32)
    tracker = StreamingToneTracker()
    smooth = tracker.process(audio)
    detail = tracker.detail_tones
    def edge_width(values):
        near = values[2380:2460]
        return np.flatnonzero(near > 2220)[0] - np.flatnonzero(near > 1580)[0]
    assert edge_width(detail) <= edge_width(smooth) - 4
    sync_frequencies = np.r_[np.full(489, 1200.0), np.full(240, 1500.0)]
    sync_audio = (.8 * np.sin(2 * np.pi * np.cumsum(sync_frequencies) / 24000)).astype(np.float32)
    decoder = SSTVDecoder()
    decoder.add_audio(sync_audio)
    assert abs(decoder._sync_end(0, 20) - 489 / 24) < .17


def test_footprint_auto_receive_boundary():
    sat = {'lat': 0.0, 'lon': 0.0, 'footprint_deg': 20.0}
    assert footprint_contains(sat, 0.0, 0.0)
    assert footprint_contains(sat, 0.0, 19.9)
    assert not footprint_contains(sat, 0.0, 20.1)


def test_new_catalog_number_orbit():
    lines = read_json(TLE_FILE, {})['lines']
    fields = export_omm(Satrec.twoline2rv(*lines), 'TEST SATELLITE')
    fields['NORAD_CAT_ID'] = 100001
    assert position(fields)['alt_km'] > 0
    current, upcoming = pass_paths(fields)
    assert current and upcoming


def test_auto_receive_follows_footprint():
    class Receiver:
        running = False
        starts = 0
        stops = 0
        target_frequency_mhz = 0
        def start(self, *args):
            self.starts += 1
            self.running = True
        def stop(self):
            self.stops += 1
            self.running = False
    app = core.App.__new__(core.App)
    app.lock = threading.Lock()
    app.settings = {**core.DEFAULT_SETTINGS, 'auto_receive': True, 'doppler_enabled': False,
                    'home_lat': -33.8, 'home_lon': 151.2, 'device_id': 'rtl:0',
                    'monitored_satellites': ['25544'],
                    'sstv_downlink_preferences': {'25544': 437.55}}
    app.tle = read_json(TLE_FILE, {})
    app.selected_tle = lambda settings, catalog_number=None: app.tle
    app.receiver = Receiver()
    app.auto_started = False
    app.auto_active_satellite = None
    app.auto_active_frequency_mhz = None
    app.last_auto_attempt = 0
    for inside in (True, False):
        with patch.object(core, 'footprint_contains', return_value=inside), \
                patch.object(core, 'write_settings'), \
                patch.object(core.time, 'sleep', side_effect=StopIteration):
            try:
                app.tracker_loop()
            except StopIteration:
                pass
    assert app.receiver.starts == 1 and app.receiver.stops == 1
    assert not app.receiver.running and not app.auto_started


def test_auto_receive_arms_mapped_satellites_from_empty_list():
    settings = {**core.DEFAULT_SETTINGS, 'auto_receive': True,
                'tracked_satellites': ['25544', '7530'], 'selected_satellite': '25544',
                'monitored_satellites': []}
    assert core.normalize_auto_monitor(settings)['monitored_satellites'] == ['25544', '7530']
    settings['auto_receive'] = False
    settings['monitored_satellites'] = []
    assert core.normalize_auto_monitor(settings)['monitored_satellites'] == []


def test_auto_receive_leaves_manual_receiver_running():
    class Receiver:
        def __init__(self):
            self.running = True
            self.stops = 0
            self.starts = 0
            self.target_frequency_mhz = 437.55
            self.active = SimpleNamespace(demod_mode='FM')
        def start(self, *_args):
            self.starts += 1
            self.running = True
        def stop(self):
            self.stops += 1
            self.running = False
    app = core.App.__new__(core.App)
    app.lock = threading.Lock()
    app.settings = {**core.DEFAULT_SETTINGS, 'auto_receive': True, 'doppler_enabled': False,
                    'home_lat': -33.8, 'home_lon': 151.2, 'device_id': 'rtl:0',
                    'monitored_satellites': ['25544'],
                    'sstv_downlink_preferences': {'25544': 437.55}}
    app.tle = read_json(TLE_FILE, {})
    app.selected_tle = lambda settings, catalog_number=None: app.tle
    app.receiver = Receiver()
    app.auto_started = False
    app.auto_active_satellite = None
    app.auto_active_frequency_mhz = None
    app.last_auto_attempt = 0
    for inside in (True, False):
        with patch.object(core, 'footprint_contains', return_value=inside), \
                patch.object(core.time, 'sleep', side_effect=StopIteration):
            try:
                app.tracker_loop()
            except StopIteration:
                pass
    assert app.receiver.starts == 0
    assert app.receiver.stops == 0
    assert not app.auto_started and app.receiver.running
    with patch.object(core, 'footprint_contains', return_value=False), \
            patch.object(core.time, 'sleep', side_effect=StopIteration):
        try:
            app.tracker_loop()
        except StopIteration:
            pass
    assert app.receiver.stops == 0 and app.receiver.running


def test_manual_receiver_doppler_only_inside_footprint():
    class Receiver:
        running = True
        target_frequency_mhz = 437.55
        stops = 0
        def stop(self):
            self.stops += 1
    app = core.App.__new__(core.App)
    app.lock = threading.Lock()
    app.settings = {**core.DEFAULT_SETTINGS, 'auto_receive': True, 'doppler_enabled': True,
                    'home_lat': -33.8, 'home_lon': 151.2, 'device_id': 'rtl:0',
                    'monitored_satellites': ['25544'], 'frequency_mhz': 437.55}
    app.selected_tle = lambda settings, catalog_number=None: {'lines': ['orbit']}
    app.receiver = Receiver()
    app.auto_started = False
    app.auto_active_satellite = None
    app.last_auto_attempt = 0
    for inside, expected in ((True, 437.5525), (False, 437.55)):
        with patch.object(core, 'valid_orbit', return_value=True), \
                patch.object(core, 'position', return_value={'lat': 0, 'lon': 0}), \
                patch.object(core, 'footprint_contains', return_value=inside), \
                patch.object(core, 'look_angles', return_value={}), \
                patch.object(core, 'doppler_shift_hz', return_value=2500 if inside else 0), \
                patch.object(core.time, 'sleep', side_effect=StopIteration):
            try:
                app.tracker_loop()
            except StopIteration:
                pass
        assert abs(app.receiver.target_frequency_mhz - expected) < .000001
    assert app.receiver.stops == 0 and app.receiver.running


def test_auto_receive_switches_monitored_satellites():
    class Receiver:
        def __init__(self):
            self.running = False
            self.starts = []
            self.stops = 0
            self.target_frequency_mhz = 0
        def start(self, _device, frequency, *_args):
            self.starts.append(frequency)
            self.running = True
        def stop(self):
            self.stops += 1
            self.running = False
    app = core.App.__new__(core.App)
    app.lock = threading.Lock()
    app.settings = {**core.DEFAULT_SETTINGS, 'auto_receive': True, 'doppler_enabled': False,
                    'home_lat': -33.8, 'home_lon': 151.2, 'device_id': 'rtl:0',
                    'tracked_satellites': ['A', 'B'], 'monitored_satellites': ['A', 'B'],
                    'sstv_downlink_preferences': {'A': 145.8, 'B': 437.55}}
    app.selected_tle = lambda settings, catalog_number=None: {'name': catalog_number,
                                                                'lines': [catalog_number]}
    app.receiver = Receiver()
    app.auto_started = False
    app.auto_active_satellite = None
    app.auto_active_frequency_mhz = None
    app.last_auto_attempt = 0
    for visible in ({'A'}, {'B'}, set()):
        with patch.object(core, 'valid_orbit', return_value=True), \
                patch.object(core, 'position', side_effect=lambda lines, *args: {'id': lines[0]}), \
                patch.object(core, 'footprint_contains', side_effect=lambda sat, *_: sat['id'] in visible), \
                patch.object(core, 'write_settings'), \
                patch.object(core.time, 'sleep', side_effect=StopIteration):
            try:
                app.tracker_loop()
            except StopIteration:
                pass
    assert app.receiver.starts == [145.8, 437.55]
    assert app.receiver.stops == 2
    assert not app.auto_started


def test_streaming_fm_demodulator():
    rate = 240000
    times = np.arange(rate) / rate
    modulation = 900 + 2200 * np.sin(2 * math.pi * 1900 * times)
    phase = np.cumsum(modulation) * (2 * math.pi / rate)
    iq = (np.exp(1j * phase) + .04 + .02j).astype(np.complex64)
    reference = StreamingFMDemodulator().process(iq)
    streamed = StreamingFMDemodulator()
    parts = []
    start = 0
    for size in (16384, 501, 30001, 127, 8192) * 5:
        if start >= len(iq):
            break
        parts.append(streamed.process(iq[start:start + size]))
        start += size
    if start < len(iq):
        parts.append(streamed.process(iq[start:]))
    audio = np.concatenate(parts)
    assert len(audio) == len(reference) == 24000
    assert np.max(np.abs(audio - reference)) < .02
    useful = audio[3000:]
    spectrum = np.abs(np.fft.rfft(useful * np.hanning(len(useful))))
    peak = np.fft.rfftfreq(len(useful), 1 / 24000)[np.argmax(spectrum[1:]) + 1]
    assert abs(peak - 1900) < 2
    assert .15 < np.sqrt(np.mean(useful ** 2)) < .6


def test_marker_tunes_with_fixed_spectrum_center():
    rate = 240000
    times = np.arange(rate) / rate
    tone = 1900 * np.sin(2 * math.pi * 1900 * times)
    carrier = 25000 + tone
    iq = np.exp(1j * np.cumsum(carrier) * (2 * math.pi / rate)).astype(np.complex64)
    offset = digital_shift_hz(145.8, 145.825, 'FM')
    assert abs(offset + 25000) < .001
    assert abs(digital_shift_hz(14.242, 14.230, 'USB')) < .001
    shifter = StreamingFrequencyShifter(rate)
    demod = StreamingFMDemodulator(rate)
    audio = np.concatenate([demod.process(shifter.process(iq[i:i + 16384], offset))
                            for i in range(0, len(iq), 16384)])
    useful = audio[3000:]
    spectrum = np.abs(np.fft.rfft(useful * np.hanning(len(useful))))
    peak = np.fft.rfftfreq(len(useful), 1 / 24000)[np.argmax(spectrum[1:]) + 1]
    assert abs(peak - 1900) < 2
    assert np.sqrt(np.mean(useful ** 2)) > .1


def test_hf_sidebands_and_noise_filter():
    for mode, sign in (('USB', 1), ('LSB', -1)):
        for rate in (240000, 250000):
            timebase = np.arange(rate) / rate
            iq = np.exp(2j * math.pi * (-12000 + sign * 1900) * timebase).astype(np.complex64)
            demod = StreamingHFDemodulator(rate, 3, mode)
            audio = np.concatenate([demod.process(iq[i:i + 16384])
                                    for i in range(0, len(iq), 16384)])
            useful = audio[round(demod.audio_rate * .2):]
            spectrum = np.abs(np.fft.rfft(useful * np.hanning(len(useful))))
            peak = np.fft.rfftfreq(len(useful), 1 / demod.audio_rate)[np.argmax(spectrum[1:]) + 1]
            assert abs(peak - 1900) < 3, (mode, rate, peak)
    filt = StreamingAudioNoiseFilter(24000)
    def response(hz):
        return abs(sum(filt.kernel * np.exp(-2j * math.pi * hz * np.arange(-40, 41) / 24000)))
    assert .8 < response(1100) < 1.2
    assert .8 < response(2300) < 1.2
    assert response(5000) < .02


def test_sdrplay_gain_direction():
    reductions = [sdrplay_if_reduction(value) for value in range(0, 51, 5)]
    assert reductions[0] == 59 and reductions[-1] == 20
    assert all(left > right for left, right in zip(reductions, reductions[1:]))


def test_hf_sstv_from_iq():
    source = signal_for_mode('Robot36')
    spectrum = np.fft.fft(source)
    analytic_mask = np.zeros(len(source))
    analytic_mask[0] = 1
    analytic_mask[1:(len(source) + 1) // 2] = 2
    if len(source) % 2 == 0:
        analytic_mask[len(source) // 2] = 1
    analytic = np.fft.ifft(spectrum * analytic_mask)
    iq = np.repeat(analytic, 10)
    iq *= np.exp(-2j * math.pi * 12000 * np.arange(len(iq)) / 240000)
    demod = StreamingHFDemodulator()
    filt = StreamingAudioNoiseFilter(demod.audio_rate)
    decoder = SSTVDecoder()
    for index in range(0, len(iq), 16384):
        decoded = filt.process(demod.process(iq[index:index + 16384]))
        decoder.add_audio(decoded, demod.audio_rate)
    assert decoder.mode == 'Robot36' and decoder.next_pair >= 1


def signal_without_vis(name):
    rate, phase = 24000, 0.0
    parts = []
    def tone(hz, ms):
        nonlocal phase
        count = round(ms * rate / 1000)
        angles = phase + np.arange(1, count + 1) * (2 * math.pi * hz / rate)
        parts.append((np.sin(angles) * .8).astype(np.float32))
        phase = float(angles[-1]) % (2 * math.pi)
    tone(1900, 80)
    spec = MODES[name]
    if name == 'Robot36':
        for line in range(5):
            for hz, ms in ((1200, 9), (1500, 3), (1900, 88),
                           (1500 if line % 2 == 0 else 2300, 4.5),
                           (1900, 1.5), (1900, 44)):
                tone(hz, ms)
    else:
        for _ in range(4):
            tone(1200, 20)
            tone(1500, spec[4])
            for _ in range(4):
                tone(1900, spec[5])
    return np.concatenate(parts)


def test_wait_for_vis_and_manual_sync():
    for name in ('Robot36', 'PD50', 'PD120', 'PD290'):
        events = []
        decoder = SSTVDecoder(events.append)
        feed(decoder, signal_without_vis(name))
        assert decoder.mode is None and decoder.image is None, name
        assert not any(event['type'] == 'start' for event in events), name
        assert decoder.force_sync(name), name
        decoder.add_audio(np.zeros(4096, dtype=np.float32), 24000)
        assert decoder.mode == name and decoder.image is not None, name


def test_fm_iq_waits_for_vis():
    baseband = signal_without_vis('Robot36')
    iq = np.exp(1j * np.cumsum(500 + 2500 * np.repeat(baseband, 10))
                * (2 * math.pi / 240000)).astype(np.complex64)
    demod = StreamingFMDemodulator()
    decoder = SSTVDecoder()
    for index in range(0, len(iq), 16384):
        decoder.add_audio(demod.process(iq[index:index + 16384]), 24000)
    assert decoder.mode is None and decoder.image is None
    assert decoder.force_sync('Robot36')
    decoder.add_audio(np.zeros(4096, dtype=np.float32), 24000)
    assert decoder.mode == 'Robot36' and decoder.next_pair >= 1


def test_tle_update_status():
    cached = read_json(TLE_FILE, {})
    lines = cached['lines']
    app = core.App.__new__(core.App)
    app.lock = threading.Lock()
    app.tle_update_lock = threading.Lock()
    app.tle = cached.copy()
    app.tle_error = ''
    app.tle_check_status = 'Checking online TLE…'
    app.tle_checked_at = None
    temporary = Path(__file__).resolve().parent / 'tle.update-test.json'
    assert not temporary.exists()
    same = ('ISS\n' + '\n'.join(lines)).encode()
    newer = lines.copy()
    newer[0] = newer[0][:18] + f'{float(newer[0][18:32]) + 1:014.8f}' + newer[0][32:68]
    checksum = (sum(int(char) for char in newer[0] if char.isdigit()) + newer[0].count('-')) % 10
    newer[0] += str(checksum)
    assert core.valid_tle(newer)
    try:
        with patch.object(core, 'TLE_FILE', temporary), patch.object(core, 'TLE_SOURCES',
                (('Test feed', 'https://example.invalid/iss', 'text'),)):
            with patch.object(core, 'urlopen', return_value=BytesIO(same)):
                result = app.update_tle()
            assert 'already latest' in result and app.tle_checked_at
            assert not temporary.exists()
            with patch.object(core, 'urlopen', return_value=BytesIO(('ISS\n' + '\n'.join(newer)).encode())):
                result = app.update_tle()
            assert 'updated from Test feed' in result
            assert read_json(temporary, {})['lines'] == newer
            assert app.tle_check_status == 'Updated from Test feed'
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == '__main__':
    test_live_rtl_ppm_correction()
    test_vis_needs_full_leader()
    test_decoder()
    test_pass_paths()
    test_mapped_paths_and_rings()
    test_home_passes()
    test_mapped_home_passes_are_merged()
    test_doppler_only_in_range()
    test_sharp_tone_transition()
    test_noisy_sstv_tone_detail()
    test_sharp_sstv_edges()
    test_footprint_auto_receive_boundary()
    test_new_catalog_number_orbit()
    test_auto_receive_follows_footprint()
    test_auto_receive_arms_mapped_satellites_from_empty_list()
    test_auto_receive_leaves_manual_receiver_running()
    test_manual_receiver_doppler_only_inside_footprint()
    test_auto_receive_switches_monitored_satellites()
    test_streaming_fm_demodulator()
    test_marker_tunes_with_fixed_spectrum_center()
    test_wfm_and_squelch()
    test_hf_sidebands_and_noise_filter()
    test_sdrplay_gain_direction()
    test_hf_sstv_from_iq()
    test_wait_for_vis_and_manual_sync()
    test_fm_iq_waits_for_vis()
    test_tle_update_status()
    print('Native VIS gating, manual sync, FM/WFM/HF demodulation, home passes, and Doppler passed')
