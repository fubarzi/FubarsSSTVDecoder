"""Desktop session persistence and TLE button smoke test."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from PIL import Image

import core
import desktop


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(.03)


def main():
    gallery_dir = tempfile.TemporaryDirectory()
    original_received = desktop.RECEIVED_DIR
    desktop.RECEIVED_DIR = Path(gallery_dir.name)
    base_time = time.time_ns()
    for index in range(22):
        path = desktop.RECEIVED_DIR / f'image_{index:02}.png'
        Image.new('RGB', (40, 32), (index * 10, 40, 80)).save(path)
        stamp = base_time - (22 - index) * 1_000_000_000
        os.utime(path, ns=(stamp, stamp))
    temporary = core.ROOT / 'settings.session-test.json'
    temporary_ini = temporary.with_suffix('.ini')
    assert temporary.resolve().parent == core.ROOT.resolve()
    assert not temporary.exists()
    assert not temporary_ini.exists()
    temporary.write_text(json.dumps({'audio_input_name': 'CABLE Output',
                                     'audio_input_active': True}), encoding='utf-8')
    original_core, original_ini = core.SETTINGS_FILE, core.INI_FILE
    core.SETTINGS_FILE, core.INI_FILE = temporary, temporary_ini
    try:
        audio = np.sin(2 * np.pi * 1900 * np.arange(7500) / 25000).astype(np.float32)
        for output_rate in (44100, 48000):
            monitor_buffer = desktop.MonitorAudioBuffer(output_rate)
            monitor_buffer.push(audio[:1250], 25000)
            assert not np.any(monitor_buffer.read(512))
            monitor_buffer.push(audio[1250:], 25000)
            expected = np.sin(2 * np.pi * 1900 * np.arange(len(monitor_buffer.data)) / output_rate)
            assert np.sqrt(np.mean((monitor_buffer.data - expected) ** 2)) < .04
            assert np.sqrt(np.mean(monitor_buffer.read(1024) ** 2)) > .2
            assert len(monitor_buffer.data) > output_rate * .2
        root = tk.Tk()
        app = desktop.Desktop(root)
        pump(root, .5)
        assert app.tabs.tab(4, 'text') == 'IMAGES'
        assert len(app.thumbnail_paths) == 20
        assert app.thumbnail_paths[0].name == 'image_21.png'
        assert app.thumbnail_paths[-1].name == 'image_02.png'
        assert app.thumbnail_photos[0].width() <= 76
        assert app.thumbnail_photos[0].height() <= 58
        with patch.object(desktop.os, 'startfile') as open_folder:
            app.tabs.select(4)
            pump(root, .1)
            assert app.thumbnail_columns >= 3
            assert app.thumbnail_cards[3].grid_info()['row'] == 0
            open_folder.assert_not_called()
            app.open_image_folder()
            open_folder.assert_called_once_with(str(desktop.RECEIVED_DIR))
        assert (app.clear_thumbnails_button.winfo_rootx() >=
                app.open_image_folder_button.winfo_rootx() +
                app.open_image_folder_button.winfo_width())
        app._clear_image_thumbnails()
        assert app.thumbnail_paths == []
        assert len(list(desktop.RECEIVED_DIR.glob('*.png'))) == 22
        with patch.object(app, 'mmsstv', None):
            app.save_image(image=Image.new('RGB', (40, 32), 'red'))
        assert len(app.thumbnail_paths) == 1
        assert app.thumbnail_paths[0] == app.last_save
        assert app.last_save.is_file()
        assert app.last_save.name.startswith('ISS_')
        app._open_saved_image(app.last_save)
        viewers = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
        assert len(viewers) == 1
        viewers[0].destroy()
        app.auto_receive.set(True)
        app._auto_receive_changed()
        assert app.app.settings['monitored_satellites'] == ['25544']
        app.auto_receive.set(False)
        app.save_settings()
        app.arm_auto_receive()
        assert app.auto_receive.get() and app.doppler.get()
        assert app.app.settings['auto_receive'] is True
        assert app.auto_button.cget('text') == 'AUTO' or app.auto_button.cget('text') == 'AUTO ON'
        app.stop_receiver()
        assert not app.auto_receive.get() and app.app.settings['auto_receive'] is False
        with app.app.lock:
            app.app.settings['monitored_satellites'] = []
            core.write_settings(app.app.settings)
        app._show_tracked_satellites()
        app.tabs.select(0)
        root.geometry('1050x700+100+120')
        pump(root, .2)
        assert (app.hf_lsb_button.winfo_rootx() + app.hf_lsb_button.winfo_width()
                <= app.tabs.winfo_rootx() + app.tabs.winfo_width()), 'HF buttons are clipped'
        assert app.volume_slider.winfo_viewable()
        assert (app.language_combo.winfo_rootx() + app.language_combo.winfo_width()
                <= root.winfo_rootx() + root.winfo_width())
        assert app.map_canvas.winfo_height() >= 140
        assert (app.pass_labels[-1].winfo_rooty() + app.pass_labels[-1].winfo_height()
                <= app.bottom_pane.winfo_rooty() + app.bottom_pane.winfo_height())
        assert app.receiver_canvas.yview()[1] < 1
        app.receiver_canvas.yview_moveto(1)
        pump(root, .1)
        assert app.volume_slider.winfo_rooty() < app.tabs.winfo_rooty() + app.tabs.winfo_height()
        app.receiver_canvas.yview_moveto(0)
        assert float(app.frequency.get()) == 437.55
        root.geometry('1200x760+100+120')
        app.frequency.delete(0, 'end')
        app.frequency.insert(0, '437.550')
        app.bandwidth.set('20')
        app.gain.set(30)
        app.ppm.delete(0, 'end')
        app.ppm.insert(0, '7')
        app.volume_slider.set(35)
        pump(root, .1)
        assert abs(app.monitor_gain - .35) < .001
        assert app.output_combo.current() >= 0
        assert app.outputs[app.output_combo.current()][0] != app.mmsstv_output_index
        raw_audio = np.ones(100, dtype=np.float32)
        with patch.object(app.mmsstv_buffer, 'push') as cable_feed, \
                patch.object(app.monitor_buffer, 'push') as speaker_feed, \
                patch.object(app, 'mmsstv_output_stream', object()):
            app._receiver_audio_sink(raw_audio, 24000)
            cable_feed.assert_called_once_with(raw_audio, 24000)
            speaker_feed.assert_not_called()
        class FakeOutputStream:
            def __init__(self, callback):
                self.callback = callback
            def start(self):
                pass
            def stop(self):
                pass
            def close(self):
                pass
        with patch.object(desktop.sd, 'OutputStream', side_effect=lambda **kwargs: FakeOutputStream(kwargs['callback'])):
            app._volume_user_changed(None)
        assert app.monitor.get() and app.audio_output_stream is not None
        monitor_audio = np.ones(round(app.monitor_sample_rate * .3), dtype=np.float32)
        app._receiver_audio_sink(monitor_audio, app.monitor_sample_rate)
        output = np.zeros((512, 1), dtype=np.float32)
        app.audio_output_stream.callback(output, 512, None, None)
        assert abs(float(output[120:300].mean()) - .35) < .02
        app.volume_slider.set(10)
        pump(root, .05)
        app.audio_output_stream.callback(output, 512, None, None)
        assert abs(float(output[120:300].mean()) - .10) < .02
        app.monitor.set(False)
        app.toggle_monitor()
        app.volume_slider.set(35)
        app.latitude.insert(0, '-33.8688')
        app.longitude.insert(0, '151.2093')
        app.doppler.set(False)
        app.tune_now()
        assert app.app.receiver.target_frequency_mhz == 437.55
        assert app.app.receiver.target_ppm_correction == 7
        app.ppm.delete(0, 'end')
        app.ppm.insert(0, '-11')
        app.ppm.focus_set()
        root.update()
        app.ppm.event_generate('<Return>')
        root.update()
        assert app.app.receiver.target_ppm_correction == -11
        deadline = time.time() + 3
        while len(app.home_passes) < 4 and time.time() < deadline:
            pump(root, .05)
        assert len(app.home_passes) == 4
        assert app.pass_labels[0].cget('fg') == '#54bfff'
        assert all(label.cget('fg') == desktop.GREEN for label in app.pass_labels[1:])
        assert all(label.cget('text') for label in app.pass_labels)
        app.spectrum_center_mhz = 437.551234
        app._draw_spectrum()
        assert app.spectrum_info.cget('text').startswith('437.551234 MHz center')
        rng = np.random.default_rng(7)
        weak = np.clip(127.5 + rng.normal(0, 2, 8192), 0, 255).astype(np.uint8).tobytes()
        strong = np.clip(127.5 + rng.normal(0, 20, 8192), 0, 255).astype(np.uint8).tobytes()
        app._draw_spectrum([weak] * 8)
        weak_level = float(np.median(app.spectrum_bins))
        app._draw_spectrum([strong] * 8)
        assert np.median(app.spectrum_bins) > weak_level + .08
        pump(root, .2)
        app.content_pane.sash_place(0, 0, 270)
        app.tabs.select(1)
        app.map_zoom = 1.5
        app.map_pan_x = 25
        app.map_pan_y = -12
        app._draw_map()
        pump(root, .1)
        saved_sash = app.content_pane.sash_coord(0)[1]
        app.close()
        cfg = json.loads(temporary.read_text(encoding='utf-8'))
        assert core.read_ini(temporary_ini, {}) == cfg
        assert cfg['home_lat'] == -33.8688 and cfg['home_lon'] == 151.2093
        assert cfg['frequency_mhz'] == 437.55 and cfg['bandwidth_khz'] == 20
        assert cfg['gain_db'] == 30 and cfg['doppler_enabled'] is False
        assert cfg['ppm_correction'] == -11
        assert 'audio_input_active' not in cfg and 'audio_input_name' not in cfg
        assert cfg['monitor_volume'] == 35
        assert cfg['decoder_mode'] == 'Auto'
        assert cfg['map_zoom'] == 1.5 and cfg['selected_tab'] == 1
        assert cfg['window_geometry'].startswith('1200x760+')
        temporary.unlink()
        assert core.read_settings() == cfg
        core.write_json(temporary, cfg)

        root = tk.Tk()
        restored = desktop.Desktop(root)
        pump(root, .8)
        assert restored.thumbnail_paths == [app.last_save]
        assert len(list(desktop.RECEIVED_DIR.glob('*.png'))) == 23
        assert restored.latitude.get() == '-33.8688'
        assert restored.longitude.get() == '151.2093'
        assert float(restored.frequency.get()) == 437.55
        assert restored.mode_combo.get() == 'Auto'
        assert restored.ppm.get() == '-11'
        assert restored.volume.get() == 35
        assert restored.map_zoom == 1.5
        assert restored.tabs.index(restored.tabs.select()) == 1
        assert restored.clock.cget('text').startswith('PC LOCAL')
        iss_downlinks = [{'frequency_mhz': 145.8, 'description': 'Mode V Imaging'},
                         {'frequency_mhz': 437.55, 'description': 'Mode U SSTV'}]
        restored._show_sstv_frequencies(437.55, {'25544': iss_downlinks}, 'Test transmitter data')
        assert restored.frequency.get() == '437.550000'
        assert restored.sstv_frequency_combo.current() == 1
        assert abs(restored.content_pane.sash_coord(0)[1] - saved_sash) <= 6
        assert ttk.Style().lookup('Dark.TCombobox', 'foreground', ('readonly',)) == desktop.GREEN
        restored._map_drag_start(SimpleNamespace(x=100, y=100))
        restored._map_drag(SimpleNamespace(x=140, y=110))
        assert restored.map_pan_x != 0
        restored._map_wheel(SimpleNamespace(x=180, y=150, delta=120))
        assert restored.map_zoom > 1
        assert abs(restored.map_scaled_size[0] / restored.map_scaled_size[1] - 2) < .01
        if restored.mmsstv is None:
            restored.mmsstv = SimpleNamespace(image=None, mode=None, status={},
                                              poll=lambda: None, close=lambda: None)
            restored.mmsstv_output_stream = SimpleNamespace(close=lambda: None)
        restored.mmsstv.image = Image.new('RGB', (64, 64))
        restored.view_image_full()
        viewers = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
        assert len(viewers) == 1
        viewers[0].destroy()
        updated = threading.Event()
        restored.app.update_tle = updated.set
        restored.update_tle()
        assert updated.wait(2), 'TLE update button did not invoke the updater'
        restored.scan_request += 1  # Ignore any startup SDR scan still in flight.
        restored.devices = [{'id': 'driver=sdrplay', 'driver': 'sdrplay', 'label': 'RSPdx',
                             'antennas': ['Antenna A', 'Antenna B', 'Antenna C']}]
        restored.device_combo['values'] = ['RSPdx']
        restored.device_combo.current(0)
        restored._update_antenna_choices()
        restored.scan_request += 1
        restored.device_results.put(('sdr', (restored.scan_request, 'driver=sdrplay',
                                              {'driver=sdrplay'},
                                              [restored.devices[0],
                                               {'id': 'rtl:2', 'driver': 'rtl',
                                                'label': 'New SDR (R828D)'}])))
        restored._poll()
        assert restored.device_combo.get() == 'New SDR (R828D)'
        restored.device_combo.current(0)
        restored._update_antenna_choices()
        restored.antenna_combo.set('Antenna C')
        assert 'HIGHER = STRONGER' in restored.gain_title.cget('text')
        restored.tabs.select(0)
        pump(root, .1)
        restored.gain.set(32.5)
        pump(root, .1)
        assert restored.app.receiver.target_gain_db == 32.5
        restored.squelch.set(-50)
        pump(root, .1)
        assert restored.app.receiver.target_squelch_dbfs == -50
        restored.demod_combo.set('WFM')
        restored._mode_changed()
        assert restored.bandwidth.get() == '200'
        assert str(restored.noise_filter_checkbox.cget('state')) == 'disabled'
        restored._draw_spectrum([strong] * 3)
        before_waterfall = restored.waterfall.copy()
        restored.floor_scale.set(-80)
        pump(root, .1)
        assert restored.spectrum_floor_dbfs.get() == -80
        assert np.any(restored.waterfall != before_waterfall)
        restored.noise_filter.set(False)
        restored._noise_changed()
        assert restored.app.receiver.noise_filter_enabled is False
        restored.hf_usb_button.invoke()
        assert restored.demod_combo.get() == 'USB'
        assert restored.frequency.get() == '14.230000'
        assert restored.bandwidth.get() == '3'
        restored._draw_spectrum()
        assert restored.spectrum_passband[0] == restored.spectrum_marker_x
        assert restored.spectrum_passband[1] > restored.spectrum_marker_x
        restored.hf_lsb_button.invoke()
        assert restored.demod_combo.get() == 'LSB'
        assert restored.frequency.get() == '7.171000'
        assert restored.bandwidth.get() == '3'
        restored._draw_spectrum()
        assert restored.spectrum_passband[0] < restored.spectrum_marker_x
        assert restored.spectrum_passband[1] == restored.spectrum_marker_x
        assert restored.spectrum.bind('<MouseWheel>')
        restored._spectrum_wheel(SimpleNamespace(delta=120, state=0))
        assert restored.frequency.get() == '7.171100'
        assert restored.app.receiver.target_frequency_mhz == 7.1711
        restored._spectrum_wheel(SimpleNamespace(delta=-120, state=0x0001))
        assert restored.frequency.get() == '7.171090'
        restored._draw_spectrum()
        last_digit = restored.frequency_hitboxes[-1]
        x = (last_digit[1] + last_digit[2]) / 2
        y = (last_digit[3] + last_digit[4]) / 2
        restored._spectrum_hover(SimpleNamespace(x=x, y=y))
        assert restored.frequency_hover_index == last_digit[0]
        restored._spectrum_wheel(SimpleNamespace(delta=120, state=0, x=x, y=y))
        assert restored.frequency.get() == '7.171091'
        megahertz_digit = restored.frequency_hitboxes[0]
        x = (megahertz_digit[1] + megahertz_digit[2]) / 2
        y = (megahertz_digit[3] + megahertz_digit[4]) / 2
        restored._spectrum_wheel(SimpleNamespace(delta=120, state=0, x=x, y=y))
        assert restored.frequency.get() == '8.171091'
        assert abs(restored.app.receiver.target_center_mhz - 8.183091) < 1e-6
        restored.set_frequency(7.17109)
        restored._draw_spectrum([strong] * 3)
        original_trace = restored.spectrum_bins.copy()
        original_center = restored.spectrum_display_center_mhz
        original_marker = restored.spectrum_marker_x
        restored._spectrum_press(SimpleNamespace(x=40, y=60))
        assert restored.spectrum_drag['kind'] == 'trace'
        restored._spectrum_drag_motion(SimpleNamespace(x=75, y=60))
        assert restored.frequency.get() == '7.171090'
        assert restored.spectrum_display_center_mhz < original_center
        assert restored.spectrum_marker_x > original_marker
        assert not np.array_equal(restored.spectrum_bins, original_trace)
        restored._spectrum_release(SimpleNamespace(x=75, y=60))
        assert restored.spectrum_drag is None and restored.spectrum_pan_pixels == 0
        assert restored.frequency.get() == '7.171090'
        assert restored.app.settings['receiver_center_mhz'] < original_center
        assert restored.app.receiver.target_center_mhz == restored.app.settings['receiver_center_mhz']
        restored.set_frequency(7.17109)
        restored._draw_spectrum()
        original_trace = restored.spectrum_bins.copy()
        original_marker = restored.spectrum_marker_x
        marker_x = round(original_marker)
        restored._spectrum_press(SimpleNamespace(x=marker_x, y=60))
        assert restored.spectrum_drag['kind'] == 'marker'
        restored._spectrum_drag_motion(SimpleNamespace(x=marker_x + 25, y=60))
        assert restored.frequency.get() == '7.171090'
        assert restored.spectrum_pan_pixels == 0
        assert restored.spectrum_marker_x > original_marker
        assert np.array_equal(restored.spectrum_bins, original_trace)
        restored._spectrum_release(SimpleNamespace(x=marker_x + 25, y=60))
        selected_after_marker = float(restored.frequency.get())
        center_after_marker = restored.spectrum_display_center_mhz
        assert selected_after_marker > 7.17109
        assert center_after_marker == original_center or abs(center_after_marker - original_center) < 1e-6
        assert restored.app.receiver.target_center_mhz == restored.app.settings['receiver_center_mhz']
        assert np.array_equal(restored.spectrum_bins, original_trace)
        assert restored.spectrum_marker_x > original_marker
        fake_data = {'running': True, 'error': '', 'tuned_frequency_mhz': 7.17109,
                     'applied_gain_db': 32.5, 'iq': b'', 'iq_frames': [],
                     'audio': [(np.zeros(100, dtype='<i2')).tobytes()], 'sample_rate': 24000}
        restored.source = 'SDR FM'
        audio = np.ones(100, dtype=np.float32)
        with patch.object(restored.mmsstv_buffer, 'push') as feed:
            restored._receiver_audio_sink(audio, 24000)
            feed.assert_called_once()
            assert feed.call_args.args[0] is audio
            assert feed.call_args.args[1] == 24000
        restored.mmsstv.mode = 'PD120'
        restored.mmsstv.status = {'state': 'ready', 'mode': 'PD120', 'receiving': True}
        with patch.object(restored.app.receiver, 'pull_raw', return_value={**fake_data, 'audio': []}), \
                patch.object(restored.mmsstv, 'poll', return_value=None):
            restored._poll()
        assert 'PD120' in restored.image_title.cget('text')
        assert 'PD120' in restored.detected_mode_status.cget('text')
        assert restored.mode_combo.get() == 'Auto'
        restored.close()
        hf_cfg = json.loads(temporary.read_text(encoding='utf-8'))
        assert hf_cfg['frequency_mhz'] == selected_after_marker and hf_cfg['demod_mode'] == 'LSB'
        assert hf_cfg['receiver_center_mhz'] == round(center_after_marker, 6)
        assert hf_cfg['antenna_name'] == 'Antenna C'
        assert hf_cfg['gain_db'] == 32.5 and hf_cfg['noise_filter_enabled'] is False
        assert hf_cfg['squelch_dbfs'] == -50 and hf_cfg['spectrum_floor_dbfs'] == -80
        assert hf_cfg['window_geometry'].startswith('1200x760+')
        root = tk.Tk()
        reopened = desktop.Desktop(root)
        pump(root, .8)
        assert root.geometry().startswith('1200x760+')
        assert float(reopened.frequency.get()) == selected_after_marker
        assert abs(reopened._receiver_center() - center_after_marker) < 1e-6
        reopened.satellite_search.set('AO-07')
        assert reopened.satellite_rows and reopened.satellite_rows[0]['NORAD_CAT_ID'] == 7530
        reopened.catalog_list.selection_set(0)
        reopened._add_satellites()
        assert reopened.app.settings['tracked_satellites'] == ['25544', '7530']
        assert reopened.app.settings['selected_satellite'] == '7530'
        assert 'AO-07' in reopened.selected_satellite_label.cget('text')
        assert len(reopened.app.orbit()['tracked']) == 2
        deadline = time.time() + 2
        while '7530' not in reopened.map_paths and time.time() < deadline:
            pump(root, .05)
        assert '7530' in reopened.map_paths
        reopened.tracked_list.selection_clear(0, 'end')
        reopened.tracked_list.selection_set(0, 1)
        reopened._select_pass_satellites()
        assert reopened.app.settings['pass_satellites'] == ['25544', '7530']
        deadline = time.time() + 4
        while len(reopened.home_passes) < 4 and time.time() < deadline:
            pump(root, .05)
        assert len(reopened.home_passes) == 4
        assert all(item['satellite_name'] in reopened.pass_labels[index].cget('text')
                   for index, item in enumerate(reopened.home_passes))
        assert [item['aos'] for item in reopened.home_passes] == sorted(
            item['aos'] for item in reopened.home_passes)
        reopened._monitor_selected()
        assert reopened.app.settings['monitored_satellites'] == ['25544', '7530']
        assert 'ISS' in reopened.monitored_satellites_label.cget('text')
        assert 'AO-07' in reopened.monitored_satellites_label.cget('text')
        pump(root, .05)
        assert (reopened.monitored_satellites_label.winfo_rooty() >=
                reopened.remove_from_map_button.winfo_rooty() +
                reopened.remove_from_map_button.winfo_height())
        reopened.tracked_list.selection_clear(0, 'end')
        reopened.tracked_list.selection_set(0)
        reopened._unmonitor_selected()
        assert reopened.app.settings['monitored_satellites'] == ['7530']
        reopened.tracked_list.selection_clear(0, 'end')
        reopened.tracked_list.selection_set(0, 1)
        before_sstv_tune = float(reopened.frequency.get())
        reopened._show_sstv_frequencies(before_sstv_tune,
            {'25544': [{'frequency_mhz': 437.55, 'description': 'ISS SSTV', 'source': 'SatNOGS DB'}],
             '7530': [{'frequency_mhz': 145.88, 'description': 'SSTV test', 'source': 'SatNOGS DB'}]},
            'Test transmitter data')
        assert reopened.frequency.get() == '145.880000'
        assert reopened.demod_combo.get() == 'FM'
        assert reopened.sstv_frequency_combo.current() == 1
        assert 'ISS' in reopened.sstv_frequency_combo['values'][0]
        assert 'AO-07' in reopened.sstv_frequency_combo['values'][1]
        reopened.sstv_satellite_search.set('AO-07')
        assert len(reopened.sstv_rows) == 1
        assert reopened.sstv_rows[0]['satellite_id'] == '7530'
        reopened.sstv_satellite_search.set('')
        reopened.tracked_list.selection_clear(0, 'end')
        reopened.tracked_list.selection_set(1)
        reopened.tracked_list.event_generate('<<ListboxSelect>>')
        pump(root, .05)
        assert len(reopened.sstv_rows) == 2
        assert {row['satellite_id'] for row in reopened.sstv_rows} == {'25544', '7530'}
        reopened._show_sstv_frequencies(before_sstv_tune, {'25544': [], '7530': []},
                                        'SatNOGS DB · updated/cached')
        assert reopened.sstv_rows[0]['frequency_mhz'] is None
        assert 'AO-07 · No SSTV downlink listed' in reopened.sstv_frequency_combo.get()
        reopened._show_sstv_frequencies(before_sstv_tune,
            {'25544': [{'frequency_mhz': 437.55, 'description': 'ISS SSTV'}],
             '7530': [{'frequency_mhz': 145.88, 'description': 'SSTV test'}]},
            'Test transmitter data')
        reopened.tracked_list.selection_set(0)
        reopened.tracked_list.event_generate('<<ListboxSelect>>')
        pump(root, .05)
        assert len(reopened.sstv_rows) == 2
        reopened.tracked_list.selection_clear(0, 'end')
        reopened.tracked_list.selection_set(1)
        reopened._monitor_selected()
        assert reopened.app.settings['monitored_satellites'] == ['7530']
        assert reopened.auto_receive.get() is True
        reopened.sstv_frequency_combo.current(0)
        reopened._sstv_downlink_selected(None)
        assert reopened.app.settings['selected_satellite'] == '25544'
        assert reopened.frequency.get() == '437.550000'
        assert reopened.app.settings['sstv_downlink_preferences']['25544'] == 437.55
        reopened.tracked_list.selection_clear(0, 'end')
        reopened.tracked_list.selection_set(1)
        reopened.tracked_list.event_generate('<<ListboxSelect>>')
        pump(root, .05)
        assert reopened.app.settings['selected_satellite'] == '7530'
        reopened.manual_sstv_entry.insert(0, '145.950')
        reopened._manual_sstv_tune()
        assert reopened.frequency.get() == '145.950000'
        assert reopened.app.settings['manual_sstv_frequencies']['7530'] == 145.95
        reopened._show_sstv_frequencies(145.95,
            {'7530': [{'frequency_mhz': 145.88, 'description': 'SSTV test'}]}, 'Test transmitter data')
        assert reopened.frequency.get() == '145.950000'
        assert any('Manual SSTV' in row for row in reopened.sstv_frequency_combo['values'])
        with patch.object(reopened.app, 'update_tle', return_value='ISS updated') as update_iss, \
                patch.object(reopened.app, 'refresh_catalog', return_value='Catalog updated') as update_catalog:
            reopened._refresh_home_passes()
            deadline = time.time() + 2
            while (not update_iss.called or not update_catalog.called) and time.time() < deadline:
                pump(root, .05)
            update_iss.assert_called_once()
            update_catalog.assert_called_once()
        reopened.save_image(image=Image.new('RGB', (40, 32), 'blue'))
        assert reopened.last_save.name.startswith('AO-07_')
        reopened.arm_auto_receive()
        assert reopened.app.settings['monitored_satellites'] == ['25544', '7530']
        reopened.tracked_list.selection_clear(0, 'end')
        reopened.tracked_list.selection_set(0)
        reopened._unmonitor_selected()
        reopened.close()
        assert json.loads(temporary.read_text(encoding='utf-8'))['selected_satellite'] == '7530'
        assert json.loads(temporary.read_text(encoding='utf-8'))['monitored_satellites'] == ['7530']
        assert json.loads(temporary.read_text(encoding='utf-8'))['pass_satellites'] == ['25544', '7530']
        root = tk.Tk()
        maximized = desktop.Desktop(root)
        pump(root, .5)
        normal_geometry = root.geometry()
        root.state('zoomed')
        pump(root, .2)
        maximized.close()
        maximized_cfg = json.loads(temporary.read_text(encoding='utf-8'))
        assert maximized_cfg['window_maximized'] is True
        assert maximized_cfg['window_geometry'] == normal_geometry
        root = tk.Tk()
        restored_maximized = desktop.Desktop(root)
        pump(root, .8)
        assert root.state() == 'zoomed'
        root.state('normal')
        pump(root, .1)
        assert root.geometry() == normal_geometry
        restored_maximized.close()
        with tempfile.TemporaryDirectory() as catalog_folder, \
                patch.object(core, 'CATALOG_FILE', Path(catalog_folder) / 'satellites.json'), \
                patch.object(core.App, 'refresh_catalog', return_value='Catalog test'):
            root = tk.Tk()
            manual_app = desktop.Desktop(root)
            manual_app.translation_loading = True
            manual_app.band_country_combo.set('Italy')
            manual_app._country_changed()
            assert manual_app.app.settings['bandplan_country'] == 'IT'
            assert manual_app.app.settings['language_code'] == 'it'
            assert manual_app.language_combo.get() == 'Italian'
            assert '7.165' in manual_app.hf_lsb_button.cget('text')
            manual_app.language_combo.set('German')
            manual_app._language_changed()
            assert manual_app.app.settings['language_code'] == 'de'
            manual_app._add_satellite_manually()
            dialog = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
            entries = [child for child in dialog.winfo_children() if isinstance(child, tk.Entry)]
            tle_box = next(child for child in dialog.winfo_children() if isinstance(child, tk.Text))
            entries[0].insert(0, 'LUCA')
            entries[1].insert(0, '437.450')
            tle_box.insert('1.0', '\n'.join((
                '1 67287U 25313AR  26317.19964205  .00010094  00000-0  38548-3 0  9994',
                '2 67287  97.3803  30.3533 0008986 180.2735 179.8501 15.26796163 48690')))
            button_row = next(child for child in dialog.winfo_children() if isinstance(child, tk.Frame))
            next(child for child in button_row.winfo_children() if isinstance(child, tk.Button)
                 and child.cget('text') == 'SAVE AND ADD TO MAP').invoke()
            assert manual_app.app.catalog.get(67287)['OBJECT_NAME'] == 'LUCA'
            assert manual_app.app.settings['manual_sstv_frequencies']['67287'] == 437.45
            assert '67287' in manual_app.app.settings['tracked_satellites']
            assert manual_app.frequency.get() == '437.450000'
            manual_app.close()
        print('Desktop satellite selection, pass list, WFM, squelch, spectrum, persistence, and TLE button passed')
    finally:
        desktop.RECEIVED_DIR = original_received
        gallery_dir.cleanup()
        core.SETTINGS_FILE, core.INI_FILE = original_core, original_ini
        if temporary.exists():
            temporary.unlink()
        if temporary_ini.exists():
            temporary_ini.unlink()


if __name__ == '__main__':
    with patch.object(desktop.SSTVFrequencies, 'lookup', return_value=([], 'Test transmitter data')), \
            patch.object(desktop.SSTVFrequencies, 'lookup_all', return_value=({}, 'Test transmitter data')), \
            patch.object(desktop.Desktop, '_start_mmsstv'):
        main()
