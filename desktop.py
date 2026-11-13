"""Fubar's SSTV Decoder: native Windows desktop application."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

import numpy as np
from PIL import Image, ImageDraw, ImageTk
import sounddevice as sd

from core import App, ROOT, RECEIVED_DIR, write_settings, valid_tle, tle_epoch, valid_orbit, orbit_epoch, safe_receiver_center, configured_sstv_frequency
from fm import StreamingHFDemodulator, HF_MODES
from mmsstv_bridge import MmsstvBridge
from windows_audio import ensure_cable_decoder_only
from orbit import position, look_angles, next_mapped_home_passes, footprint_contains
from sstv_frequencies import SSTVFrequencies


BG, PANEL, PANEL2 = '#00120f', '#08221d', '#0c3028'
GREEN, CYAN, WHITE, MUTED = '#2bff99', '#72d9ed', '#e9fff6', '#83a89e'


class MonitorAudioBuffer:
    """Thread-safe monitor audio with a short jitter buffer and rate conversion."""

    def __init__(self, output_rate=48000):
        self.lock = threading.Lock()
        self.reset(output_rate)

    def reset(self, output_rate):
        with self.lock:
            self.output_rate = int(output_rate)
            self.data = np.empty(0, dtype=np.float32)
            self.prefill = round(self.output_rate * .22)
            self.maximum = round(self.output_rate * .85)
            self.input_rate = None
            self.next_position = 0.0
            self.previous = 0.0
            self.ready = False

    def push(self, samples, input_rate):
        values = np.asarray(samples, dtype=np.float32)
        if not len(values):
            return
        with self.lock:
            if self.input_rate is None or abs(self.input_rate - input_rate) > 1:
                self.input_rate = float(input_rate)
                self.next_position = 0.0
                self.previous = 0.0
                self.data = np.empty(0, dtype=np.float32)
                self.ready = False
            if abs(input_rate - self.output_rate) > 1:
                step = input_rate / self.output_rate
                positions = np.arange(self.next_position, len(values), step)
                output = np.interp(positions, np.arange(-1, len(values)),
                                   np.r_[self.previous, values]).astype(np.float32)
                self.next_position = (positions[-1] + step - len(values)
                                      if len(positions) else self.next_position - len(values))
            else:
                output = values.copy()
            self.previous = float(values[-1])
            self.data = np.concatenate((self.data, output))
            if len(self.data) > self.maximum:
                self.data = self.data[-self.maximum:].copy()
                self.ready = False

    def read(self, frames):
        output = np.zeros(frames, dtype=np.float32)
        with self.lock:
            starting = not self.ready
            if starting:
                if len(self.data) < self.prefill:
                    return output
                self.ready = True
            count = min(frames, len(self.data))
            output[:count] = self.data[:count]
            self.data = self.data[count:].copy()
            if starting and count:
                fade = min(96, count)
                output[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            if count < frames:
                fade = min(96, count)
                if fade:
                    output[count-fade:count] *= np.linspace(1, 0, fade, dtype=np.float32)
                self.ready = False
        return output


def pass_paths(lines, now=None):
    """Ground tracks for the current and next complete satellite revolutions."""
    if not valid_orbit(lines):
        return [], []
    now = now or datetime.now(timezone.utc)
    try:
        mean_motion = (float(lines['MEAN_MOTION']) if isinstance(lines, dict)
                       else float(lines[1][52:63]))
        period = timedelta(seconds=86400 / mean_motion)
        step = period / 120
        def refine(earlier, later):
            for _ in range(16):
                middle = earlier + (later - earlier) / 2
                if position(lines, middle)['lat'] >= 0:
                    later = middle
                else:
                    earlier = middle
            return earlier + (later - earlier) / 2

        later, later_lat = now, position(lines, now)['lat']
        start = None
        for _ in range(140):
            earlier = later - step
            earlier_lat = position(lines, earlier)['lat']
            if earlier_lat < 0 <= later_lat:
                start = refine(earlier, later)
                break
            later, later_lat = earlier, earlier_lat
        if start is None:
            return [], []

        def next_crossing(previous):
            earlier = previous + period * .6
            earlier_lat = position(lines, earlier)['lat']
            for _ in range(120):
                later = earlier + step
                later_lat = position(lines, later)['lat']
                if earlier_lat < 0 <= later_lat:
                    return refine(earlier, later)
                earlier, earlier_lat = later, later_lat
            raise ValueError('Next ascending crossing unavailable')

        following = next_crossing(start)
        after_following = next_crossing(following)
        def track(begin, end):
            return [position(lines, begin + (end - begin) * index / 240)
                    for index in range(241)]
        current = track(start, following)
        upcoming = track(following, after_following)
        return current, upcoming
    except Exception:
        return [], []


class Desktop:
    def __init__(self, root):
        self.root = root
        root.title("Fubar's SSTV Decoder")
        root.configure(bg=BG)
        root.geometry('1500x900')
        root.minsize(1050, 700)
        self.app = App()
        self.sstv_frequencies = SSTVFrequencies(ROOT / 'sstv_frequencies.json')
        self.sstv_lookup_request = 0
        self.sstv_rows = []
        self.sstv_all_rows = []
        self.sstv_lookup_status = 'Checking SatNOGS DB…'
        self.displayed_auto_target = None
        self.monitor_buffer = MonitorAudioBuffer()
        self.monitor_gain = .7
        self.device_results = queue.Queue()
        self.scan_request = 0
        self.catalog_revision = None
        self.audio_output_stream = None
        self.mmsstv = None
        self.mmsstv_output_stream = None
        self.mmsstv_buffer = MonitorAudioBuffer()
        self.mmsstv_output_index = None
        self.mmsstv_error = ''
        self.monitor_sample_rate = 24000
        self.source = 'SDR FM'
        self.devices = []
        self.outputs = []
        self.map_base = Image.open(ROOT / 'worldmap.jpg').convert('RGB')
        self.map_image = None
        self.map_zoom = 1.0
        self.map_pan_x = 0.0
        self.map_pan_y = 0.0
        self.map_scaled = None
        self.map_scaled_size = None
        self.decoder_image = None
        self.spectrum_image = None
        self.waterfall = None
        self.waterfall_db = None
        self.spectrum_ema = None
        self.spectrum_peak = None
        self.spectrum_bins = None
        self.heat_lut = self._make_heat_lut()
        self.spectrum_floor_dbfs = tk.DoubleVar(value=-105)
        self.pass_current, self.pass_next = [], []
        self.last_pass_calc = 0
        self.last_tle_key = None
        self.pass_calculating = False
        self.pass_request = 0
        self.home_passes = []
        self.home_pass_sources = []
        self.last_home_pass_calc = 0
        self.last_home_key = None
        self.home_pass_calculating = False
        self.home_pass_request = 0
        self.last_iq = b''
        self.last_rate = 240000
        self.spectrum_center_mhz = None
        self.spectrum_display_center_mhz = None
        self.spectrum_drag = None
        self.spectrum_pan_pixels = 0
        self.spectrum_preview_frequency_mhz = None
        self.spectrum_pending_center_mhz = None
        self.spectrum_pending_started = 0
        self.frequency_hover_index = None
        self.frequency_hitboxes = []
        self.last_error = ''
        self.last_save = None
        self.thumbnail_photos = []
        self.thumbnail_paths = []
        self.thumbnail_cards = []
        self.thumbnail_columns = 0
        self.tick_after_id = None
        self.restore_after_id = None
        self._build()
        self._load_settings()
        self._refresh_image_thumbnails()
        self.normal_window_geometry = self.app.settings.get('window_geometry') or self.root.geometry()
        self.root.bind('<Configure>', self._remember_normal_window_geometry, add='+')
        self.rescan()
        self.rescan_audio_outputs()
        self._start_mmsstv()
        self._request_sstv_frequency()
        self.restore_after_id = self.root.after(400, self._restore_session)
        self._tick()
        root.protocol('WM_DELETE_WINDOW', self.close)

    def _label(self, parent, text, color=MUTED, size=10, bold=False):
        return tk.Label(parent, text=text, fg=color, bg=parent.cget('bg'), font=('Segoe UI', size, 'bold' if bold else 'normal'))

    def _entry(self, parent, width=11):
        return tk.Entry(parent, width=width, bg='#061b17', fg=WHITE, insertbackground=WHITE,
                        relief='flat', highlightthickness=1, highlightbackground='#236652', font=('Segoe UI', 10))

    def _button(self, parent, text, command, width=None):
        return tk.Button(parent, text=text, command=command, width=width, bg='#053628', fg=WHITE,
                         activebackground='#12684c', activeforeground=WHITE, relief='flat',
                         highlightthickness=1, highlightbackground='#13805d', padx=10, pady=5,
                         font=('Segoe UI', 9, 'bold'))

    def _combo(self, parent, values=(), width=25):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('Dark.TCombobox', fieldbackground='#061b17', background='#073727',
                        foreground=GREEN, arrowcolor=GREEN, bordercolor='#236652',
                        selectforeground=GREEN, selectbackground='#061b17')
        style.map('Dark.TCombobox', foreground=[('readonly', GREEN)],
                  fieldbackground=[('readonly', '#061b17')],
                  background=[('readonly', '#073727')],
                  selectforeground=[('readonly', GREEN)],
                  selectbackground=[('readonly', '#061b17')])
        return ttk.Combobox(parent, values=values, width=width, state='readonly', style='Dark.TCombobox')

    def _build(self):
        self.root.option_add('*TCombobox*Listbox.foreground', GREEN)
        self.root.option_add('*TCombobox*Listbox.background', '#061b17')
        self.root.option_add('*TCombobox*Listbox.selectForeground', '#00120f')
        self.root.option_add('*TCombobox*Listbox.selectBackground', GREEN)
        header = tk.Frame(self.root, bg=BG, height=58)
        header.pack(fill='x', padx=12, pady=(8, 5))
        self._label(header, "FUBAR'S SSTV DECODER", GREEN, 18, True).pack(side='left', padx=(8, 22))
        self.header_status = self._label(header, 'ISS FM RECEIVER  •  STANDBY', CYAN, 10, True)
        self.header_status.pack(side='left')
        self.clock = self._label(header, '', GREEN, 11, True)
        self.clock.pack(side='right', padx=15)
        self.tle_status = self._label(header, 'Updating ISS TLE…', MUTED, 9)
        self.tle_status.pack(side='right', padx=15)
        for widget in (header, *header.winfo_children()):
            widget.bind('<ButtonPress-1>', self._drag_start)
            widget.bind('<B1-Motion>', self._drag_window)

        pane_style = dict(bg='#10533d', sashwidth=9, sashrelief='raised', showhandle=True,
                          handlesize=14, handlepad=10)
        outer = tk.PanedWindow(self.root, orient='horizontal', **pane_style)
        self.outer_pane = outer
        outer.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        content = tk.PanedWindow(outer, orient='vertical', **pane_style)
        self.content_pane = content
        outer.add(content, minsize=700)
        sidebar = tk.Frame(outer, bg=PANEL, width=355)
        outer.add(sidebar, minsize=320)

        spectrum_panel = tk.Frame(content, bg=PANEL, highlightbackground='#17644e', highlightthickness=1)
        content.add(spectrum_panel, minsize=190)
        bar = tk.Frame(spectrum_panel, bg=PANEL)
        bar.pack(fill='x', padx=10, pady=6)
        self._label(bar, 'RF SPECTRUM / WATERFALL', WHITE, 10, True).pack(side='left')
        self.spectrum_info = self._label(bar, '240 kHz span', CYAN, 9)
        self.spectrum_info.pack(side='right')
        spectrum_row = tk.Frame(spectrum_panel, bg=PANEL)
        spectrum_row.pack(fill='both', expand=True, padx=7, pady=(0, 7))
        self.spectrum = tk.Canvas(spectrum_row, bg='#031411', highlightthickness=0)
        self.spectrum.pack(side='left', fill='both', expand=True)
        floor_panel = tk.Frame(spectrum_row, bg=PANEL)
        floor_panel.pack(side='right', fill='y', padx=(5, 0))
        self._label(floor_panel, 'RGB', GREEN, 8, True).pack(anchor='center')
        floor_controls = tk.Frame(floor_panel, bg=PANEL)
        floor_controls.pack(fill='both', expand=True)
        self.floor_scale = tk.Scale(floor_controls, from_=-30, to=-120, resolution=1,
                                    orient='vertical', variable=self.spectrum_floor_dbfs,
                                    command=self._display_floor_changed, length=150,
                                    bg=PANEL, fg=GREEN, troughcolor=PANEL2,
                                    activebackground=GREEN, highlightthickness=0)
        self.floor_scale.pack(side='left', fill='y', expand=True)
        self.floor_legend = tk.Canvas(floor_controls, width=13, bg=PANEL,
                                      highlightthickness=0)
        self.floor_legend.pack(side='right', fill='y', padx=(0, 2))
        self.floor_legend.bind('<Configure>', self._draw_floor_legend)
        self._label(floor_panel, 'FLOOR', CYAN, 8, True).pack(anchor='center')
        self.frequency_display_font = tkfont.Font(family='Consolas', size=22, weight='bold')
        self.spectrum.bind('<ButtonPress-1>', self._spectrum_press)
        self.spectrum.bind('<B1-Motion>', self._spectrum_drag_motion)
        self.spectrum.bind('<ButtonRelease-1>', self._spectrum_release)
        self.spectrum.bind('<MouseWheel>', self._spectrum_wheel)
        self.spectrum.bind('<Motion>', self._spectrum_hover)
        self.spectrum.bind('<Leave>', self._spectrum_leave)

        bottom = tk.PanedWindow(content, orient='horizontal', **pane_style)
        self.bottom_pane = bottom
        content.add(bottom, minsize=300)
        decode_panel = tk.Frame(bottom, bg=PANEL, highlightbackground='#17644e', highlightthickness=1)
        bottom.add(decode_panel, minsize=330)
        self.image_title = self._label(decode_panel, 'RECEIVED SSTV IMAGE  ·  MODE: WAITING FOR VIS', WHITE, 10, True)
        self.image_title.pack(anchor='w', padx=10, pady=6)
        self.image_source_status = self._label(decode_panel, 'Decoder audio: SDR receiver', CYAN, 9)
        self.image_source_status.pack(anchor='w', padx=10, pady=(0, 4))
        self.image_canvas = tk.Canvas(decode_panel, bg='#071714', highlightthickness=0)
        self.image_canvas.pack(fill='both', expand=True, padx=7, pady=(0, 7))
        self.image_canvas.bind('<Double-Button-1>', lambda _event: self.view_image_full())
        map_panel = tk.Frame(bottom, bg=PANEL, highlightbackground='#17644e', highlightthickness=1)
        bottom.add(map_panel, minsize=330)
        self.map_title = self._label(map_panel, 'ISS WORLD TRACKER   •   SOLID: NEXT PASS   •   DASHED: CURRENT PASS', WHITE, 9, True)
        self.map_title.pack(anchor='w', padx=10, pady=6)
        pass_list = tk.Frame(map_panel, bg=PANEL)
        pass_list.pack(side='bottom', fill='x', padx=8, pady=(2, 6))
        pass_header = tk.Frame(pass_list, bg=PANEL)
        pass_header.pack(fill='x')
        self._label(pass_header, 'NEXT 4 HOME PASSES  ·  PC LOCAL TIME', CYAN, 8, True).pack(side='left')
        update_pass_button = self._button(pass_header, 'UPDATE PASSES', self._refresh_home_passes)
        update_pass_button.config(padx=4, pady=0, font=('Segoe UI', 8, 'bold'))
        update_pass_button.pack(side='right')
        self.pass_labels = []
        for index in range(4):
            label = tk.Label(pass_list, text='Set home GPS coordinates' if index == 0 else '',
                             fg='#54bfff' if index == 0 else GREEN, bg=PANEL,
                             font=('Consolas', 8), anchor='w')
            label.pack(fill='x')
            self.pass_labels.append(label)
        self.map_canvas = tk.Canvas(map_panel, bg='#061611', highlightthickness=0, cursor='fleur')
        self.map_canvas.pack(fill='both', expand=True, padx=7, pady=(0, 7))
        self.map_canvas.bind('<ButtonPress-1>', self._map_drag_start)
        self.map_canvas.bind('<B1-Motion>', self._map_drag)
        self.map_canvas.bind('<MouseWheel>', self._map_wheel)
        self.map_canvas.bind('<Double-Button-1>', self._map_reset)

        tabs = ttk.Notebook(sidebar)
        self.tabs = tabs
        tabs.pack(fill='both', expand=True, padx=7, pady=7)
        style = ttk.Style()
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', background=PANEL2, foreground=WHITE, padding=(12, 7))
        style.map('TNotebook.Tab', background=[('selected', '#146c4b')])
        receive = tk.Frame(tabs, bg=PANEL)
        track = tk.Frame(tabs, bg=PANEL)
        decode = tk.Frame(tabs, bg=PANEL)
        satellites = tk.Frame(tabs, bg=PANEL)
        images = tk.Frame(tabs, bg=PANEL)
        tabs.add(receive, text='RECEIVER')
        tabs.add(track, text='TRACKER')
        tabs.add(decode, text='DECODER')
        tabs.add(satellites, text='SATELLITES')
        tabs.add(images, text='IMAGES')
        self.images_tab = images
        tabs.bind('<<NotebookTabChanged>>', self._tab_changed)
        receiver_scroll = tk.Scrollbar(receive, orient='vertical')
        receiver_canvas = tk.Canvas(receive, bg=PANEL, highlightthickness=0,
                                    yscrollcommand=receiver_scroll.set)
        receiver_scroll.config(command=receiver_canvas.yview)
        receiver_scroll.pack(side='right', fill='y')
        receiver_canvas.pack(side='left', fill='both', expand=True)
        receiver_inner = tk.Frame(receiver_canvas, bg=PANEL)
        receiver_window = receiver_canvas.create_window(0, 0, window=receiver_inner, anchor='nw')
        receiver_inner.bind('<Configure>', lambda _event: receiver_canvas.config(
            scrollregion=receiver_canvas.bbox('all')))
        receiver_canvas.bind('<Configure>', lambda event: receiver_canvas.itemconfigure(
            receiver_window, width=event.width))
        self.receiver_canvas = receiver_canvas
        self._build_receiver(receiver_inner)
        def bind_receiver_wheel(widget):
            widget.bind('<MouseWheel>', lambda event: receiver_canvas.yview_scroll(
                -int(event.delta / 120), 'units'))
            for child in widget.winfo_children():
                bind_receiver_wheel(child)
        bind_receiver_wheel(receiver_inner)
        receiver_canvas.bind('<MouseWheel>', lambda event: receiver_canvas.yview_scroll(
            -int(event.delta / 120), 'units'))
        self._build_tracker(track)
        self._build_decoder(decode)
        satellite_scroll = tk.Scrollbar(satellites, orient='vertical')
        satellite_canvas = tk.Canvas(satellites, bg=PANEL, highlightthickness=0,
                                     yscrollcommand=satellite_scroll.set)
        satellite_scroll.config(command=satellite_canvas.yview)
        satellite_scroll.pack(side='right', fill='y')
        satellite_canvas.pack(side='left', fill='both', expand=True)
        satellite_inner = tk.Frame(satellite_canvas, bg=PANEL)
        satellite_window = satellite_canvas.create_window(0, 0, window=satellite_inner, anchor='nw')
        satellite_inner.bind('<Configure>', lambda _event: satellite_canvas.config(
            scrollregion=satellite_canvas.bbox('all')))
        satellite_canvas.bind('<Configure>', lambda event: satellite_canvas.itemconfigure(
            satellite_window, width=event.width))
        satellite_canvas.bind('<MouseWheel>', lambda event: satellite_canvas.yview_scroll(
            -int(event.delta / 120), 'units'))
        self.satellite_canvas = satellite_canvas
        self._build_satellites(satellite_inner)
        self._row(images, 'SAVED SSTV IMAGES')
        folder_label = self._label(images, str(RECEIVED_DIR), WHITE, 9)
        folder_label.config(wraplength=390, justify='left')
        folder_label.pack(anchor='w', padx=10, pady=6)
        image_buttons = tk.Frame(images, bg=PANEL)
        image_buttons.pack(fill='x', padx=10, pady=8)
        self.open_image_folder_button = self._button(image_buttons, 'OPEN IMAGE FOLDER', self.open_image_folder)
        self.open_image_folder_button.pack(side='left')
        self.clear_thumbnails_button = self._button(image_buttons, 'CLEAR THUMBNAILS', self._clear_image_thumbnails)
        self.clear_thumbnails_button.pack(side='left', padx=6)
        self.thumbnail_count_label = self._label(images, 'Recent images', CYAN, 9)
        self.thumbnail_count_label.pack(anchor='w', padx=10, pady=(0, 5))
        gallery = tk.Frame(images, bg=PANEL)
        gallery.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        gallery_scroll = tk.Scrollbar(gallery, orient='vertical')
        gallery_scroll.pack(side='right', fill='y')
        self.thumbnail_canvas = tk.Canvas(gallery, bg=PANEL, highlightthickness=0,
                                          yscrollcommand=gallery_scroll.set)
        self.thumbnail_canvas.pack(side='left', fill='both', expand=True)
        gallery_scroll.config(command=self.thumbnail_canvas.yview)
        self.thumbnail_inner = tk.Frame(self.thumbnail_canvas, bg=PANEL)
        thumbnail_window = self.thumbnail_canvas.create_window(0, 0, window=self.thumbnail_inner,
                                                               anchor='nw')
        self.thumbnail_inner.bind('<Configure>', lambda _event: self.thumbnail_canvas.config(
            scrollregion=self.thumbnail_canvas.bbox('all')))
        self.thumbnail_canvas.bind('<Configure>', lambda event: self._resize_thumbnail_gallery(
            event, thumbnail_window))
        self.thumbnail_canvas.bind('<MouseWheel>', lambda event: self.thumbnail_canvas.yview_scroll(
            -int(event.delta / 120), 'units'))
        self.status = self._label(sidebar, 'Ready', CYAN, 9)
        self.status.pack(fill='x', padx=12, pady=(0, 10))

    def _drag_start(self, event):
        self._drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _remember_normal_window_geometry(self, event):
        if event.widget is self.root and self.root.state() == 'normal':
            self.normal_window_geometry = self.root.geometry()

    def _tab_changed(self, _event):
        if self.tabs.select() == str(self.images_tab):
            self._refresh_image_thumbnails()

    def open_image_folder(self):
        try:
            RECEIVED_DIR.mkdir(exist_ok=True)
            os.startfile(str(RECEIVED_DIR))
        except OSError as exc:
            self._status(f'Image folder: {exc}')

    def _resize_thumbnail_gallery(self, event, window_id):
        self.thumbnail_canvas.itemconfigure(window_id, width=event.width)
        self._layout_thumbnail_cards(event.width)

    def _layout_thumbnail_cards(self, width=None):
        width = width or self.thumbnail_canvas.winfo_width()
        if width < 90:
            width = 380
        columns = max(1, min(5, width // 95))
        if columns == self.thumbnail_columns:
            return
        self.thumbnail_columns = columns
        for index, card in enumerate(self.thumbnail_cards):
            card.grid(row=index // columns, column=index % columns,
                      sticky='n', padx=3, pady=3)

    def _refresh_image_thumbnails(self):
        try:
            cutoff = int(self.app.settings.get('thumbnail_cleared_at_ns', 0))
            paths = sorted((path for path in RECEIVED_DIR.iterdir()
                            if path.is_file() and path.suffix.lower() in
                            ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp') and
                            path.stat().st_mtime_ns > cutoff),
                           key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)[:20]
        except (OSError, ValueError, TypeError) as exc:
            self._status(f'Images: {exc}')
            paths = []
        for child in self.thumbnail_inner.winfo_children():
            child.destroy()
        self.thumbnail_photos = []
        self.thumbnail_paths = []
        self.thumbnail_cards = []
        self.thumbnail_columns = 0
        for path in paths:
            try:
                with Image.open(path) as original:
                    thumbnail = original.convert('RGB')
                    thumbnail.thumbnail((76, 58), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(thumbnail, master=self.root)
            except (OSError, ValueError):
                continue
            self.thumbnail_photos.append(photo)
            self.thumbnail_paths.append(path)
            card = tk.Frame(self.thumbnail_inner, bg=PANEL2, highlightbackground='#236652',
                            highlightthickness=1, cursor='hand2')
            self.thumbnail_cards.append(card)
            picture = tk.Canvas(card, width=84, height=64, bg=PANEL2,
                                highlightthickness=0, cursor='hand2')
            picture.pack()
            picture.create_image(42, 32, image=photo)
            caption = tk.Label(card, text=datetime.fromtimestamp(path.stat().st_mtime).strftime('%d %b %H:%M'),
                               bg=PANEL2, fg=WHITE, font=('Segoe UI', 7),
                               wraplength=84, justify='center',
                               cursor='hand2')
            caption.pack(fill='x', padx=2, pady=(0, 2))
            for widget in (card, picture, caption):
                widget.bind('<Button-1>', lambda _event, image_path=path: self._open_saved_image(image_path))
                widget.bind('<MouseWheel>', lambda event: self.thumbnail_canvas.yview_scroll(
                    -int(event.delta / 120), 'units'))
        if not self.thumbnail_paths:
            tk.Label(self.thumbnail_inner, text='No recent saved SSTV images', bg=PANEL,
                     fg=MUTED, font=('Segoe UI', 10)).grid(row=0, column=0, padx=10, pady=20)
        self._layout_thumbnail_cards()
        self.thumbnail_count_label.config(text=f'LATEST {len(self.thumbnail_paths)} SAVED IMAGE(S) · MAX 20')
        self.thumbnail_canvas.yview_moveto(0)

    def _clear_image_thumbnails(self):
        with self.app.lock:
            self.app.settings['thumbnail_cleared_at_ns'] = time.time_ns()
            write_settings(self.app.settings)
        self._refresh_image_thumbnails()
        self._status('Thumbnails cleared; saved images remain in received_sstv')

    def _open_saved_image(self, path):
        try:
            with Image.open(path) as saved:
                image = saved.copy()
        except OSError as exc:
            self._status(f'Open image: {exc}')
            return
        self._show_image_window(image, path.name)

    def _drag_window(self, event):
        if hasattr(self, '_drag_offset'):
            self.root.geometry(f'+{event.x_root - self._drag_offset[0]}+{event.y_root - self._drag_offset[1]}')

    def _row(self, parent, title):
        self._label(parent, title, CYAN, 9, True).pack(anchor='w', padx=10, pady=(13, 3))

    def _build_receiver(self, parent):
        self._row(parent, 'SDR DEVICE')
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=10)
        self.device_combo = self._combo(row, width=27)
        self.device_combo.pack(side='left', fill='x', expand=True)
        self.device_combo.bind('<<ComboboxSelected>>', lambda _event: self._update_antenna_choices())
        self._button(row, 'SCAN', self.rescan).pack(side='left', padx=(5, 0))
        self._row(parent, 'RF MODE')
        self.demod_combo = self._combo(parent, ('FM', 'WFM', 'USB', 'LSB', 'AM'), width=12)
        self.demod_combo.set('FM')
        self.demod_combo.pack(anchor='w', padx=10)
        self.demod_combo.bind('<<ComboboxSelected>>', lambda _event: self._mode_changed())
        self._row(parent, 'ANTENNA INPUT')
        self.antenna_combo = self._combo(parent, ('Auto',), width=24)
        self.antenna_combo.set('Auto')
        self.antenna_combo.pack(anchor='w', padx=10)
        self._row(parent, 'FREQUENCY  ·  MHz')
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=10)
        self.frequency = self._entry(row, 12)
        self.frequency.pack(side='left')
        self.frequency.bind('<Return>', lambda _event: self.tune_now())
        self._button(row, '145.800', lambda: self.set_frequency(145.8, select_fm=True)).pack(side='left', padx=4)
        self._button(row, '437.550', lambda: self.set_frequency(437.55, select_fm=True)).pack(side='left')
        hf_row = tk.Frame(parent, bg=PANEL)
        hf_row.pack(anchor='w', padx=10, pady=(6, 0))
        self.hf_usb_button = self._button(hf_row, 'HF SSTV 14.230 USB',
                                          lambda: self.set_hf_preset(14.230, 'USB'))
        self.hf_usb_button.pack(side='left')
        self.hf_lsb_button = self._button(hf_row, 'HF SSTV 7.171 LSB',
                                          lambda: self.set_hf_preset(7.171, 'LSB'))
        self.hf_lsb_button.pack(side='left', padx=(5, 0))
        self._button(parent, 'TUNE TO SELECTED FREQUENCY', self.tune_now).pack(anchor='w', padx=10, pady=(7, 0))
        self._row(parent, 'CHANNEL BANDWIDTH  ·  kHz')
        self.bandwidth = self._combo(parent, ('2.5', '3', '4', '6.25', '12.5', '15', '20', '25', '50',
                                               '150', '180', '200'), width=12)
        self.bandwidth.pack(anchor='w', padx=10)
        self.gain_title = self._label(parent, 'RF GAIN  ·  dB', CYAN, 9, True)
        self.gain_title.pack(anchor='w', padx=10, pady=(13, 3))
        self.gain = tk.Scale(parent, from_=0, to=50, resolution=.5, orient='horizontal',
                             command=self._gain_changed, length=260, bg=PANEL, fg=GREEN,
                             troughcolor=PANEL2, activebackground=GREEN, highlightthickness=0)
        self.gain.pack(anchor='w', padx=10)
        self.gain_status = self._label(parent, 'Applied RF gain: —', MUTED, 9)
        self.gain_status.pack(anchor='w', padx=10)
        self._row(parent, 'SQUELCH  ·  dBFS  ·  -120 = OFF')
        self.squelch = tk.Scale(parent, from_=-120, to=0, resolution=1, orient='horizontal',
                                command=self._squelch_changed, length=260, bg=PANEL, fg=GREEN,
                                troughcolor=PANEL2, activebackground=GREEN, highlightthickness=0)
        self.squelch.pack(anchor='w', padx=10)
        self.squelch_status = self._label(parent, 'Signal: —  ·  squelch off', MUTED, 9)
        self.squelch_status.pack(anchor='w', padx=10)
        self._row(parent, 'FREQUENCY CORRECTION  ·  PPM')
        self.ppm = self._entry(parent)
        self.ppm.pack(anchor='w', padx=10)
        self.ppm.bind('<Return>', lambda _event: self.save_settings())
        self.ppm_status = self._label(parent, 'Applied PPM: —', MUTED, 9)
        self.ppm_status.pack(anchor='w', padx=10, pady=(3, 0))
        self.noise_filter = tk.BooleanVar(value=True)
        self.noise_filter_checkbox = tk.Checkbutton(parent, text='SSTV noise filter', variable=self.noise_filter,
                                                    command=self._noise_changed, bg=PANEL, fg=WHITE,
                                                    selectcolor=PANEL2, activebackground=PANEL,
                                                    activeforeground=WHITE)
        self.noise_filter_checkbox.pack(anchor='w', padx=10, pady=(10, 0))
        self._row(parent, 'SPEAKER MONITOR OUTPUT')
        self.output_combo = self._combo(parent, width=35)
        self.output_combo.pack(anchor='w', padx=10)
        self.output_combo.bind('<<ComboboxSelected>>',
                               lambda _event: self.toggle_monitor() if self.monitor.get() else None)
        self._button(parent, 'RESCAN OUTPUTS', self.rescan_audio_outputs).pack(anchor='w', padx=10, pady=(5, 0))
        self.monitor = tk.BooleanVar(value=False)
        tk.Checkbutton(parent, text='Monitor received audio', variable=self.monitor, command=self.toggle_monitor,
                       bg=PANEL, fg=WHITE, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=WHITE).pack(anchor='w', padx=10, pady=(6, 2))
        self._row(parent, 'SPEAKER MONITOR VOLUME')
        self.volume = tk.DoubleVar(value=70)
        self.volume_slider = tk.Scale(parent, from_=0, to=100, orient='horizontal', variable=self.volume,
                                      command=self._set_monitor_volume, resolution=1, length=260,
                                      bg=PANEL, fg=GREEN, troughcolor=PANEL2, activebackground=GREEN,
                                      highlightthickness=0)
        self.volume_slider.pack(anchor='w', padx=10)
        self.volume_slider.bind('<ButtonRelease-1>', self._volume_user_changed, add='+')
        self.volume_slider.bind('<KeyRelease>', self._volume_user_changed, add='+')
        self.monitor_status = self._label(parent, 'Speaker monitor off', MUTED, 9)
        self.monitor_status.pack(anchor='w', padx=10, pady=(3, 0))
        self.cable_route_status = self._label(parent, 'VB-CABLE route: checking…', CYAN, 9)
        self.cable_route_status.pack(anchor='w', padx=10, pady=(3, 0))
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=10, pady=18)
        self._button(row, 'CONNECT', self.connect, 9).pack(side='left')
        self._button(row, 'STOP', self.stop_receiver, 7).pack(side='left', padx=5)
        self.auto_button = self._button(row, 'AUTO', self.arm_auto_receive, 7)
        self.auto_button.pack(side='left')
        self.rx_status = self._label(parent, 'Receiver stopped', MUTED, 10)
        self.rx_status.pack(anchor='w', padx=10)
        self.tuned = self._label(parent, 'Driver center: —', GREEN, 10)
        self.tuned.pack(anchor='w', padx=10, pady=5)
        self.tune_detail = self._label(parent, 'Selected: —', CYAN, 9)
        self.tune_detail.pack(anchor='w', padx=10)

    def _build_tracker(self, parent):
        self._row(parent, 'HOME LOCATION  ·  GPS COORDINATES')
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=10)
        self._label(row, 'LAT', MUTED, 9).pack(side='left')
        self.latitude = self._entry(row, 10)
        self.latitude.pack(side='left', padx=5)
        self._label(row, 'LON', MUTED, 9).pack(side='left')
        self.longitude = self._entry(row, 10)
        self.longitude.pack(side='left', padx=5)
        self._button(parent, 'SAVE HOME LOCATION', self.save_settings).pack(anchor='w', padx=10, pady=8)
        self.auto_receive = tk.BooleanVar(value=False)
        self.doppler = tk.BooleanVar(value=True)
        self.auto_receive_checkbox = tk.Checkbutton(parent, text='Auto start receiver when footprint reaches Home',
            variable=self.auto_receive, command=self._auto_receive_changed, bg=PANEL, fg=WHITE,
            selectcolor=PANEL2, activebackground=PANEL, activeforeground=WHITE)
        self.auto_receive_checkbox.pack(anchor='w', padx=10, pady=6)
        self.auto_receive_status = self._label(parent, 'Auto pass start off · connect anytime', CYAN, 9)
        self.auto_receive_status.config(wraplength=310, justify='left')
        self.auto_receive_status.pack(anchor='w', padx=10)
        tk.Checkbutton(parent, text='Automatic Doppler correction', variable=self.doppler,
            command=self.save_settings, bg=PANEL, fg=WHITE, selectcolor=PANEL2,
            activebackground=PANEL, activeforeground=WHITE).pack(anchor='w', padx=10, pady=6)
        self._row(parent, 'SELECTED SATELLITE PASS DATA')
        self.position_text = self._label(parent, 'Waiting for TLE', WHITE, 10)
        self.position_text.pack(anchor='w', padx=10)
        self.look_text = self._label(parent, 'Azimuth —   Elevation —', GREEN, 10)
        self.look_text.pack(anchor='w', padx=10, pady=5)
        self.doppler_text = self._label(parent, 'Doppler —', CYAN, 10)
        self.doppler_text.pack(anchor='w', padx=10)
        self.tle_epoch_text = self._label(parent, 'TLE epoch: —', GREEN, 9)
        self.tle_epoch_text.pack(anchor='w', padx=10, pady=(12, 2))
        self.tle_check_text = self._label(parent, 'Checking online TLE…', CYAN, 9)
        self.tle_check_text.config(wraplength=310, justify='left')
        self.tle_check_text.pack(anchor='w', padx=10)
        self.tle_button = self._button(parent, 'UPDATE SELECTED ORBIT DATA', self.update_tle)
        self.tle_button.pack(anchor='w', padx=10, pady=15)

    def _build_satellites(self, parent):
        self._row(parent, 'AMATEUR SATELLITES  ·  SEARCH BY NAME OR NORAD ID')
        self.satellite_search = tk.StringVar()
        search = self._entry(parent, 28)
        search.config(textvariable=self.satellite_search)
        search.pack(fill='x', padx=10)
        self.satellite_search.trace_add('write', lambda *_: self._filter_satellites())
        self.catalog_status_label = self._label(parent, 'Loading satellite catalog…', CYAN, 9)
        self.catalog_status_label.config(wraplength=310, justify='left')
        self.catalog_status_label.pack(anchor='w', padx=10, pady=5)
        catalog_frame = tk.Frame(parent, bg=PANEL)
        catalog_frame.pack(fill='both', expand=True, padx=10)
        catalog_scroll = tk.Scrollbar(catalog_frame, orient='vertical')
        catalog_scroll.pack(side='right', fill='y')
        self.catalog_list = tk.Listbox(catalog_frame, selectmode='extended', height=8,
                                      bg=PANEL2, fg=WHITE, selectbackground='#146c4b',
                                      yscrollcommand=catalog_scroll.set, exportselection=False,
                                      font=('Segoe UI', 9))
        self.catalog_list.pack(side='left', fill='both', expand=True)
        catalog_scroll.config(command=self.catalog_list.yview)
        self._label(parent, 'Use Ctrl or Shift to select several satellites.', MUTED, 9).pack(anchor='w', padx=10, pady=4)
        self._button(parent, 'ADD SELECTED TO MAP', self._add_satellites).pack(anchor='w', padx=10)
        self._row(parent, 'SATELLITES ON MAP')
        self.tracked_list = tk.Listbox(parent, selectmode='extended', height=4, bg=PANEL2, fg=WHITE,
                                      selectbackground='#146c4b', exportselection=False,
                                      font=('Segoe UI', 9))
        self.tracked_list.pack(fill='x', padx=10)
        self.tracked_list.bind('<Double-Button-1>', self._activate_satellite)
        self.tracked_list.bind('<<ListboxSelect>>', self._mapped_selection_changed)
        self._label(parent, 'Use Ctrl or Shift to select several for Home passes.', MUTED, 9).pack(anchor='w', padx=10, pady=4)
        self._button(parent, 'SHOW PASSES FOR SELECTED', self._select_pass_satellites).pack(anchor='w', padx=10)
        self._button(parent, 'SET ACTIVE FOR MAP / DOPPLER', self._activate_satellite).pack(anchor='w', padx=10)
        monitor_buttons = tk.Frame(parent, bg=PANEL)
        monitor_buttons.pack(anchor='w', padx=10)
        self._button(monitor_buttons, 'MONITOR SELECTED', self._monitor_selected).pack(side='left')
        self._button(monitor_buttons, 'STOP MONITORING', self._unmonitor_selected).pack(side='left', padx=5)
        self.remove_from_map_button = self._button(parent, 'REMOVE FROM MAP', self._remove_satellite)
        self.remove_from_map_button.pack(anchor='w', padx=10)
        self.monitored_satellites_label = self._label(parent, 'SATELLITES SELECTED TO MONITOR\nNone', CYAN, 9)
        self.monitored_satellites_label.config(wraplength=310, justify='left')
        self.monitored_satellites_label.pack(anchor='w', padx=10, pady=4)
        self.selected_satellite_label = self._label(parent, 'Selected: ISS', GREEN, 10, True)
        self.selected_satellite_label.config(wraplength=310, justify='left')
        self.selected_satellite_label.pack(anchor='w', padx=10, pady=10)
        self._row(parent, 'SSTV DOWNLINK  ·  SELECTED MAP SATELLITES  ·  SATNOGS DB')
        self.sstv_frequency_combo = self._combo(parent, width=31)
        self.sstv_frequency_combo.pack(fill='x', padx=10)
        self.sstv_frequency_combo.bind('<<ComboboxSelected>>', self._sstv_downlink_selected)
        self.sstv_frequency_status = self._label(parent, 'Checking SSTV downlinks…', CYAN, 9)
        self.sstv_frequency_status.config(wraplength=310, justify='left')
        self.sstv_frequency_status.pack(anchor='w', padx=10, pady=5)
        self._row(parent, 'MANUAL SSTV FREQUENCY  ·  MHz')
        manual_row = tk.Frame(parent, bg=PANEL)
        manual_row.pack(fill='x', padx=10)
        self.manual_sstv_entry = self._entry(manual_row, 12)
        self.manual_sstv_entry.pack(side='left')
        self.manual_sstv_entry.bind('<Return>', lambda _event: self._manual_sstv_tune())
        self._button(manual_row, 'TUNE', self._manual_sstv_tune).pack(side='left', padx=5)
        self._button(manual_row, 'CLEAR', self._clear_manual_sstv).pack(side='left')
        self._button(parent, 'REFRESH SSTV DOWNLINKS', lambda: self._request_sstv_frequency(force=True)).pack(anchor='w', padx=10)
        self._button(parent, 'UPDATE SATELLITE CATALOG', self._refresh_satellite_catalog).pack(anchor='w', padx=10, pady=(0, 10))
        self.satellite_rows = []
        self._filter_satellites()
        self._show_tracked_satellites()

    def _filter_satellites(self):
        query = self.satellite_search.get().strip().casefold()
        entries = self.app.catalog.entries()
        if not any(str(row['NORAD_CAT_ID']) == '25544' for row in entries):
            entries.insert(0, {'NORAD_CAT_ID': 25544, 'OBJECT_NAME': 'ISS (ZARYA)'})
        self.satellite_rows = [row for row in entries if query in row['OBJECT_NAME'].casefold()
                               or query in str(row['NORAD_CAT_ID'])]
        self.catalog_list.delete(0, 'end')
        for row in self.satellite_rows:
            self.catalog_list.insert('end', f"{row['OBJECT_NAME']}  ·  {row['NORAD_CAT_ID']}")
        self.catalog_status_label.config(text=self.app.catalog.status +
                                         f' · {len(self.satellite_rows):,} shown')

    def _show_tracked_satellites(self, select_ids=None):
        tracked = [str(x) for x in self.app.settings.get('tracked_satellites', ['25544'])]
        monitored = [str(x) for x in self.app.settings.get('monitored_satellites', [])]
        active = str(self.app.settings.get('selected_satellite', '25544'))
        pass_choice = self.app.settings.get('pass_satellites')
        pass_ids = (tracked if pass_choice is None else [str(item) for item in pass_choice])
        if select_ids is not None:
            pass_ids = [str(item) for item in select_ids]
        self.tracked_list.delete(0, 'end')
        for index, catalog_number in enumerate(tracked):
            name = self.app.selected_tle(catalog_number=catalog_number)['name']
            marker = ('▶ ' if catalog_number == active else '') + ('● ' if catalog_number in monitored else '')
            self.tracked_list.insert('end', f'{marker}{name}  ·  {catalog_number}')
            if catalog_number in pass_ids:
                self.tracked_list.selection_set(index)
        name = self.app.selected_tle()['name']
        self.selected_satellite_label.config(text=f'Selected: {name} · NORAD {active}')
        names = [self.app.selected_tle(catalog_number=number)['name'] for number in monitored if number in tracked]
        self.monitored_satellites_label.config(text='SATELLITES SELECTED TO MONITOR' +
            ('\n' + '\n'.join(f'• {name}' for name in names) if names else '\nNone'))

    def _set_tracked(self, tracked, active, refresh_downlinks=True, preserve_selection=False):
        tracked = list(dict.fromkeys(str(x) for x in tracked))
        old_tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
        selected_ids = [old_tracked[index] for index in self.tracked_list.curselection()
                        if index < len(old_tracked)] if preserve_selection else None
        previous_tracked = [str(x) for x in self.app.settings.get('tracked_satellites', ['25544'])]
        previous_active = str(self.app.settings.get('selected_satellite', '25544'))
        with self.app.lock:
            self.app.settings['tracked_satellites'] = tracked
            self.app.settings['selected_satellite'] = str(active)
            self.app.settings['monitored_satellites'] = [str(item) for item in
                self.app.settings.get('monitored_satellites', []) if str(item) in tracked]
            if not self.app.settings['monitored_satellites']:
                self.app.settings['auto_receive'] = False
            pass_choice = self.app.settings.get('pass_satellites')
            if pass_choice is not None:
                selected = [str(item) for item in pass_choice if str(item) in tracked]
                self.app.settings['pass_satellites'] = selected or None
            write_settings(self.app.settings)
        self.auto_receive.set(bool(self.app.settings['auto_receive']))
        self._show_tracked_satellites(select_ids=selected_ids)
        self.last_tle_key = None
        self._update_orbit()
        self._draw_map()
        if refresh_downlinks and (str(active) != previous_active or tracked != previous_tracked):
            if str(active) != previous_active:
                self.demod_combo.set('FM')
                self._update_filter_control()
            self._request_sstv_frequency()

    def _add_satellites(self):
        chosen = [str(self.satellite_rows[index]['NORAD_CAT_ID'])
                  for index in self.catalog_list.curselection()]
        if not chosen:
            return
        tracked = self.app.settings.get('tracked_satellites', ['25544'])
        self._set_tracked([*tracked, *chosen], chosen[0])

    def _remove_satellite(self):
        selection = self.tracked_list.curselection()
        if not selection:
            return
        tracked = [str(x) for x in self.app.settings.get('tracked_satellites', ['25544'])]
        tracked = [item for index, item in enumerate(tracked) if index not in selection]
        if not tracked:
            tracked = ['25544']
        active = str(self.app.settings.get('selected_satellite', '25544'))
        self._set_tracked(tracked, active if active in tracked else tracked[0])

    def _activate_satellite(self, event=None):
        selection = self.tracked_list.curselection()
        tracked = self.app.settings.get('tracked_satellites', ['25544'])
        if event is not None and hasattr(event, 'y'):
            selection = (self.tracked_list.nearest(event.y),)
        if len(selection) == 1 and selection[0] < len(tracked):
            active = str(tracked[selection[0]])
            if active != str(self.app.settings.get('selected_satellite', '25544')):
                self._set_tracked(tracked, active)
        else:
            self._status('Select one satellite to make active on the map')

    def _select_pass_satellites(self):
        tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
        selected = [tracked[index] for index in self.tracked_list.curselection() if index < len(tracked)]
        if not selected:
            self._status('Select one or more satellites on the map for Home passes')
            return
        with self.app.lock:
            self.app.settings['pass_satellites'] = selected
            write_settings(self.app.settings)
        self.last_home_key = None
        self._update_orbit()
        self._status(f'Showing the next four Home passes for {len(selected)} selected satellite(s)')

    def _mapped_selection_changed(self, _event=None):
        if hasattr(self, 'sstv_frequency_combo'):
            tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
            selection = self.tracked_list.curselection()
            if len(selection) == 1 and selection[0] < len(tracked):
                selected_id = tracked[selection[0]]
                if selected_id != str(self.app.settings.get('selected_satellite', '25544')):
                    self._set_tracked(tracked, selected_id, refresh_downlinks=False,
                                      preserve_selection=True)
                    manual = self.app.settings.get('manual_sstv_frequencies', {}).get(selected_id)
                    self.manual_sstv_entry.delete(0, 'end')
                    if manual is not None:
                        self.manual_sstv_entry.insert(0, str(manual))
            self._display_sstv_rows(None, self.sstv_lookup_status, auto_tune=False)

    def _change_monitored(self, add):
        selection = self.tracked_list.curselection()
        tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
        chosen = [tracked[index] for index in selection if index < len(tracked)]
        if not chosen:
            self._status('Select satellites on the map first')
            return
        monitored = [str(item) for item in self.app.settings.get('monitored_satellites', [])]
        if add:
            monitored = list(dict.fromkeys([*monitored, *chosen]))
        else:
            monitored = [item for item in monitored if item not in chosen]
        with self.app.lock:
            self.app.settings['monitored_satellites'] = monitored
            self.app.settings['auto_receive'] = bool(monitored)
            write_settings(self.app.settings)
        self.auto_receive.set(bool(monitored))
        self._show_tracked_satellites(select_ids=chosen)
        missing = [number for number in chosen if configured_sstv_frequency(self.app.settings, number) is None]
        if add and missing:
            self._status(f'{len(monitored)} satellite(s) selected to monitor; choose SSTV transponders for {len(missing)}')
        else:
            self._status(f'{len(monitored)} satellite(s) selected to monitor')

    def _monitor_selected(self):
        self._change_monitored(True)

    def _unmonitor_selected(self):
        self._change_monitored(False)

    def _auto_receive_changed(self):
        added = None
        if self.auto_receive.get() and not self.app.settings.get('monitored_satellites'):
            tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
            selected = str(self.app.settings.get('selected_satellite', '25544'))
            if tracked:
                added = selected if selected in tracked else tracked[0]
                with self.app.lock:
                    self.app.settings['monitored_satellites'] = [added]
                self._show_tracked_satellites(select_ids=[added])
        if self.save_settings() and added:
            name = self.app.selected_tle(catalog_number=added)['name']
            self._status(f'AUTO armed for {name}; radio starts and stops with its footprint')

    def _refresh_satellite_catalog(self):
        self.catalog_status_label.config(text='Updating satellite catalog…')
        def worker():
            result = self.app.refresh_catalog()
            self.device_results.put(('catalog', result))
        threading.Thread(target=worker, daemon=True).start()

    def _request_sstv_frequency(self, force=False):
        self.sstv_lookup_request += 1
        request = self.sstv_lookup_request
        catalog_number = str(self.app.settings.get('selected_satellite', '25544'))
        tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
        manual = self.app.settings.get('manual_sstv_frequencies', {}).get(catalog_number)
        try:
            manual = float(manual) if manual is not None else None
        except (TypeError, ValueError):
            manual = None
        self.manual_sstv_entry.delete(0, 'end')
        if manual is not None:
            self.manual_sstv_entry.insert(0, f'{manual:.6f}')
        try:
            dial_before = float(self.frequency.get())
        except ValueError:
            dial_before = None
        if manual is not None and .001 <= manual <= 1700 and not self.app.auto_started and (
                dial_before is None or abs(dial_before - manual) > .000001 or
                self.demod_combo.get() != 'FM'):
            self._tune_sstv_frequency(manual, manual=True)
        cached = {number: self.sstv_frequencies.cached(number) for number in tracked}
        self._show_sstv_frequencies(dial_before, cached, 'Checking SatNOGS DB…')
        def worker():
            choices = {}
            failures = []
            for number in tracked:
                downlinks, status = self.sstv_frequencies.lookup(number, force=force)
                choices[number] = downlinks
                if 'unavailable' in status:
                    failures.append(number)
            message = ('SatNOGS DB unavailable for ' + ', '.join(failures) + ' · cached data' if failures
                       else 'SatNOGS DB · updated/cached')
            self.device_results.put(('sstv_freq', (request, tracked, dial_before, choices, message)))
        threading.Thread(target=worker, daemon=True).start()

    def _show_sstv_frequencies(self, dial_before, choices_by_sat, status):
        tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
        active = str(self.app.settings.get('selected_satellite', '25544'))
        manual_frequencies = self.app.settings.get('manual_sstv_frequencies', {})
        rows = []
        for number in tracked:
            name = self.app.selected_tle(catalog_number=number)['name']
            for item in choices_by_sat.get(number, []):
                rows.append({**item, 'satellite_id': number, 'satellite_name': name})
            try:
                manual = float(manual_frequencies.get(number))
                if .001 <= manual <= 1700 and not any(
                        item['satellite_id'] == number and
                        abs(item['frequency_mhz'] - manual) < .000001 for item in rows):
                    rows.append({'satellite_id': number, 'satellite_name': name,
                                 'frequency_mhz': manual, 'description': 'Manual SSTV frequency',
                                 'source': 'Manual'})
            except (TypeError, ValueError):
                pass
        self.sstv_all_rows = rows
        self._display_sstv_rows(dial_before, status)

    def _display_sstv_rows(self, dial_before, status, auto_tune=True):
        self.sstv_lookup_status = status
        tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
        chosen = {tracked[index] for index in self.tracked_list.curselection() if index < len(tracked)}
        visible_ids = [number for number in tracked if not chosen or number in chosen]
        rows = []
        for number in visible_ids:
            listed = [item for item in self.sstv_all_rows if item['satellite_id'] == number]
            if listed:
                rows.extend(listed)
            else:
                description = ('Checking SatNOGS DB…' if status.startswith('Checking') else
                               'No SSTV downlink cached; use manual frequency' if 'unavailable' in status else
                               'No SSTV downlink listed; use manual frequency')
                rows.append({'satellite_id': number,
                             'satellite_name': self.app.selected_tle(catalog_number=number)['name'],
                             'frequency_mhz': None, 'description': description,
                             'source': 'Unavailable'})
        self.sstv_rows = rows
        self.sstv_frequency_combo['values'] = [
            (f"{item['satellite_name']} · {item['frequency_mhz']:.6f} MHz · {item['description']}"
             if item['frequency_mhz'] is not None else
             f"{item['satellite_name']} · {item['description']}")
            for item in rows]
        if not rows:
            self.sstv_frequency_combo.set('')
            self.sstv_frequency_status.config(text=status + ' · no SSTV downlinks listed for selected satellites')
            return
        active = str(self.app.settings.get('selected_satellite', '25544'))
        manual_frequencies = self.app.settings.get('manual_sstv_frequencies', {})
        preferred = configured_sstv_frequency(self.app.settings, active)
        try:
            current = float(self.frequency.get())
        except ValueError:
            current = None
        def matching(value):
            return next((index for index, item in enumerate(rows)
                         if item['satellite_id'] == active and item['frequency_mhz'] is not None and
                         value is not None and
                         abs(item['frequency_mhz'] - float(value)) < .000001), None)
        selected = matching(preferred)
        if selected is None:
            selected = matching(current)
        if selected is None:
            selected = next((index for index, item in enumerate(rows)
                             if item['satellite_id'] == active), None)
        if selected is None:
            selected = 0
        if selected is not None:
            self.sstv_frequency_combo.current(selected)
        else:
            self.sstv_frequency_combo.set('')
        dial_unchanged = (dial_before is not None and current is not None and
                          abs(current - dial_before) < .000001)
        available = sum(item['frequency_mhz'] is not None for item in rows)
        self.sstv_frequency_status.config(text=status +
            f" · {available} SSTV downlink(s) across {len(visible_ids)} selected satellite(s)" +
            ('; choose a downlink to tune' if not dial_unchanged else ''))
        if (auto_tune and rows[selected]['frequency_mhz'] is not None and
                rows[selected]['satellite_id'] == active and dial_unchanged and
                self.demod_combo.get() == 'FM' and
                manual_frequencies.get(active) is None and not self.app.auto_started):
            self._tune_sstv_frequency(rows[selected]['frequency_mhz'])

    def _tune_sstv_frequency(self, frequency_mhz, manual=False):
        catalog_number = str(self.app.settings.get('selected_satellite', '25544'))
        self.set_frequency(frequency_mhz, select_fm=True)
        with self.app.lock:
            if manual:
                overrides = dict(self.app.settings.get('manual_sstv_frequencies', {}))
                overrides[catalog_number] = frequency_mhz
                self.app.settings['manual_sstv_frequencies'] = overrides
            else:
                overrides = dict(self.app.settings.get('manual_sstv_frequencies', {}))
                overrides.pop(catalog_number, None)
                self.app.settings['manual_sstv_frequencies'] = overrides
                preferences = dict(self.app.settings.get('sstv_downlink_preferences', {}))
                preferences[catalog_number] = frequency_mhz
                self.app.settings['sstv_downlink_preferences'] = preferences
            write_settings(self.app.settings)
        self._status(f'{self.app.selected_tle()["name"]} SSTV · {frequency_mhz:.6f} MHz FM' +
                     (' · manual' if manual else ''))

    def _manual_sstv_tune(self):
        try:
            frequency_mhz = float(self.manual_sstv_entry.get())
            if not .001 <= frequency_mhz <= 1700:
                raise ValueError
        except ValueError:
            self._status('Enter a manual SSTV frequency from 0.001 to 1700 MHz')
            return
        frequency_mhz = round(frequency_mhz, 6)
        self.manual_sstv_entry.delete(0, 'end')
        self.manual_sstv_entry.insert(0, f'{frequency_mhz:.6f}')
        self.sstv_frequency_combo.set('')
        self._tune_sstv_frequency(frequency_mhz, manual=True)
        self._request_sstv_frequency()

    def _clear_manual_sstv(self):
        catalog_number = str(self.app.settings.get('selected_satellite', '25544'))
        with self.app.lock:
            overrides = dict(self.app.settings.get('manual_sstv_frequencies', {}))
            overrides.pop(catalog_number, None)
            self.app.settings['manual_sstv_frequencies'] = overrides
            write_settings(self.app.settings)
        self.manual_sstv_entry.delete(0, 'end')
        self._request_sstv_frequency()

    def _sstv_downlink_selected(self, _event):
        selected = self.sstv_frequency_combo.current()
        if 0 <= selected < len(self.sstv_rows):
            row = self.sstv_rows[selected]
            self.app.auto_started = False
            self.app.auto_active_satellite = None
            self.app.auto_active_frequency_mhz = None
            tracked = self.app.settings.get('tracked_satellites', ['25544'])
            if row['satellite_id'] != str(self.app.settings.get('selected_satellite', '25544')):
                self._set_tracked(tracked, row['satellite_id'], refresh_downlinks=False)
            target_index = [str(item) for item in tracked].index(row['satellite_id'])
            if target_index not in self.tracked_list.curselection():
                self.tracked_list.selection_clear(0, 'end')
                self.tracked_list.selection_set(target_index)
            if row['frequency_mhz'] is None:
                self._mapped_selection_changed()
                self._status(f"{row['satellite_name']}: no SSTV downlink listed in SatNOGS DB; use manual MHz")
                return
            self.manual_sstv_entry.delete(0, 'end')
            self._tune_sstv_frequency(row['frequency_mhz'], manual=row.get('source') == 'Manual')
            self._mapped_selection_changed()

    def _build_decoder(self, parent):
        self._row(parent, 'MMSSTV ENGINE  ·  AUTO DETECTS VIS')
        self.mode_combo = self._combo(parent, ['Auto'], width=18)
        self.mode_combo.set('Auto')
        self.mode_combo.pack(anchor='w', padx=10)
        self.mode_combo.configure(state='disabled')
        self.detected_mode_status = self._label(parent, 'Detected mode: waiting for VIS', CYAN, 9)
        self.detected_mode_status.pack(anchor='w', padx=10, pady=(6, 0))
        self.mode_status = self._label(parent, 'Waiting for VIS header', GREEN, 10)
        self.mode_status.pack(anchor='w', padx=10, pady=7)
        self._label(parent, 'Decoder audio: SDR receiver → VB-CABLE → MMSSTV', CYAN, 9).pack(anchor='w', padx=10, pady=6)
        self.mmsstv_input_status = self._label(parent, 'MMSSTV input: checking CABLE Output…', CYAN, 9)
        self.mmsstv_input_status.pack(anchor='w', padx=10, pady=6)
        self._label(parent, 'PC speaker output is used only when monitoring is enabled on the Receiver tab.', CYAN, 9).pack(anchor='w', padx=10, pady=6)
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=10, pady=10)
        self._button(row, 'RETRY MMSSTV', self._start_mmsstv).pack(side='left')
        self._button(row, 'VB-CABLE SETUP', self.open_vb_cable).pack(side='left', padx=6)
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor='w', padx=10)
        self._button(row, 'SAVE PNG', self.save_image).pack(side='left')
        self._button(row, 'VIEW IMAGE 1:1', self.view_image_full).pack(side='left', padx=6)
        self._label(parent, 'Images save to received_sstv in the program folder.', MUTED, 9).pack(anchor='w', padx=10, pady=10)

    def _load_settings(self):
        cfg = self.app.settings
        if cfg.get('window_geometry'):
            self.root.geometry(cfg['window_geometry'])
        self.frequency.insert(0, str(cfg['frequency_mhz']))
        self.bandwidth.set(str(cfg['bandwidth_khz']))
        self.gain.set(float(cfg['gain_db']))
        self.squelch.set(float(cfg.get('squelch_dbfs', -120)))
        self.ppm.insert(0, str(cfg['ppm_correction']))
        self.demod_combo.set(cfg.get('demod_mode', 'FM'))
        self.noise_filter.set(bool(cfg.get('noise_filter_enabled', True)))
        self.spectrum_floor_dbfs.set(max(-120, min(-30, float(cfg.get('spectrum_floor_dbfs', -105)))))
        self._update_filter_control()
        for widget, key in ((self.latitude, 'home_lat'), (self.longitude, 'home_lon')):
            if cfg[key] is not None:
                widget.insert(0, str(cfg[key]))
        self.auto_receive.set(cfg['auto_receive'])
        self.doppler.set(cfg['doppler_enabled'])
        self.map_zoom = max(1.0, min(4.0, float(cfg.get('map_zoom', 1.0))))
        self.map_pan_x = float(cfg.get('map_pan_x', 0.0))
        self.map_pan_y = float(cfg.get('map_pan_y', 0.0))
        mode = cfg.get('decoder_mode', 'Auto')
        self.mode_combo.set('Auto')
        self.monitor.set(bool(cfg.get('monitor_enabled')))
        self.volume.set(max(0, min(100, float(cfg.get('monitor_volume', 70)))))
        self.monitor_gain = self.volume.get() / 100

    def _set_monitor_volume(self, value):
        self.monitor_gain = float(value) / 100
        if hasattr(self, 'monitor_status') and self.monitor.get():
            self.monitor_status.config(text=f'Speaker monitor: {round(float(value))}%')

    def _volume_user_changed(self, _event):
        if self.volume.get() > 0 and not self.monitor.get():
            self.monitor.set(True)
            self.toggle_monitor()

    def _gain_changed(self, value):
        gain = float(value)
        self.app.receiver.target_gain_db = gain
        with self.app.lock:
            self.app.settings['gain_db'] = gain

    def _squelch_changed(self, value):
        threshold = float(value)
        self.app.receiver.target_squelch_dbfs = threshold
        with self.app.lock:
            self.app.settings['squelch_dbfs'] = threshold

    def _noise_changed(self):
        enabled = self.noise_filter.get()
        self.app.receiver.noise_filter_enabled = enabled
        with self.app.lock:
            self.app.settings['noise_filter_enabled'] = enabled

    def _update_antenna_choices(self):
        index = self.device_combo.current()
        device = self.devices[index] if 0 <= index < len(self.devices) else {}
        names = ['Auto'] + list(device.get('antennas', []))
        self.antenna_combo['values'] = names
        wanted = self.app.settings.get('antenna_name') or 'Auto'
        self.antenna_combo.set(wanted if wanted in names else 'Auto')
        self.gain_title.config(text=('SDRplay GAIN  ·  HIGHER = STRONGER'
                                     if device.get('driver') == 'sdrplay' else 'RF GAIN  ·  dB'))

    def _mode_changed(self):
        mode = self.demod_combo.get()
        if mode == 'WFM':
            self.bandwidth.set('200')
        elif mode in HF_MODES and float(self.bandwidth.get()) > 6.25:
            self.bandwidth.set('3')
        elif mode == 'FM' and not 6.25 <= float(self.bandwidth.get()) <= 50:
            self.bandwidth.set('12.5')
        self._update_filter_control()
        if self.app.receiver.running:
            self.connect(recenter=True)
        else:
            self.save_settings(recenter=True)
        self._draw_spectrum()

    def _update_filter_control(self):
        self.noise_filter_checkbox.config(state='disabled' if self.demod_combo.get() == 'WFM' else 'normal')

    def _restore_session(self):
        positions = self.app.settings.get('pane_positions') or {}
        if self.app.settings.get('window_maximized'):
            self.root.state('zoomed')
            self.root.update_idletasks()
        selected_tab = self.app.settings.get('selected_tab', 0)
        if selected_tab in range(5):
            self.tabs.select(selected_tab)
        for pane, key, axis in ((self.outer_pane, 'outer_x', 'x'),
                                (self.content_pane, 'content_y', 'y'),
                                (self.bottom_pane, 'bottom_x', 'x')):
            value = positions.get(key)
            if isinstance(value, (int, float)) and value > 0:
                try:
                    x, y = pane.sash_coord(0)
                    pane.sash_place(0, int(value) if axis == 'x' else x,
                                    int(value) if axis == 'y' else y)
                except tk.TclError:
                    pass
        if self.monitor.get():
            self.toggle_monitor()

    def _save_session(self):
        positions = {}
        for pane, key, axis in ((self.outer_pane, 'outer_x', 'x'),
                                (self.content_pane, 'content_y', 'y'),
                                (self.bottom_pane, 'bottom_x', 'x')):
            try:
                x, y = pane.sash_coord(0)
                positions[key] = x if axis == 'x' else y
            except tk.TclError:
                pass
        values = {'decoder_mode': self.mode_combo.get(),
                  'audio_output_name': self.output_combo.get(),
                  'monitor_enabled': self.monitor.get(),
                  'monitor_volume': self.volume.get(),
                  'window_geometry': (self.normal_window_geometry if self.root.state() == 'zoomed'
                                      else self.root.geometry()),
                  'window_maximized': self.root.state() == 'zoomed',
                  'pane_positions': positions,
                  'map_zoom': self.map_zoom,
                  'map_pan_x': self.map_pan_x,
                  'map_pan_y': self.map_pan_y,
                  'spectrum_floor_dbfs': self.spectrum_floor_dbfs.get(),
                  'selected_tab': self.tabs.index(self.tabs.select())}
        with self.app.lock:
            self.app.settings.update(values)
            write_settings(self.app.settings)

    def save_settings(self, recenter=False):
        try:
            freq = float(self.frequency.get())
            lat = float(self.latitude.get()) if self.latitude.get().strip() else None
            lon = float(self.longitude.get()) if self.longitude.get().strip() else None
            gain = float(self.gain.get())
            squelch = float(self.squelch.get())
            ppm = int(self.ppm.get())
            bw = float(self.bandwidth.get())
            mode = self.demod_combo.get()
            if (not .001 <= freq <= 1700 or not 1 <= bw <= 200 or mode not in ('FM', 'WFM', *HF_MODES) or
                    not -200 <= ppm <= 200 or not -120 <= squelch <= 0 or
                    (lat is not None and not -90 <= lat <= 90) or
                    (lon is not None and not -180 <= lon <= 180)):
                raise ValueError('Frequency, bandwidth, PPM, or coordinates are out of range')
            choice = self.device_combo.current()
            device_id = self.devices[choice]['id'] if 0 <= choice < len(self.devices) else self.app.settings['device_id']
            held_center = None if recenter else self.app.settings.get('receiver_center_mhz')
            center = safe_receiver_center(freq, held_center, bw, mode)
            if held_center is not None and center != held_center:
                held_center = None
            if not .001 <= center <= 1700:
                raise ValueError('Spectrum center is out of range')
            values = dict(frequency_mhz=freq, home_lat=lat, home_lon=lon, gain_db=gain,
                          squelch_dbfs=squelch,
                          ppm_correction=ppm,
                          bandwidth_khz=bw, device_id=device_id, auto_receive=self.auto_receive.get(),
                          doppler_enabled=self.doppler.get(), demod_mode=mode,
                          antenna_name=('' if self.antenna_combo.get() == 'Auto' else self.antenna_combo.get()),
                          noise_filter_enabled=self.noise_filter.get(),
                          receiver_center_mhz=held_center)
            with self.app.lock:
                self.app.settings.update(values)
                write_settings(self.app.settings)
            self.app.receiver.target_bandwidth_khz = bw
            self.app.receiver.target_gain_db = gain
            self.app.receiver.target_ppm_correction = ppm
            self.app.receiver.target_squelch_dbfs = squelch
            self.app.receiver.noise_filter_enabled = self.noise_filter.get()
            self.app.receiver.target_frequency_mhz = freq + self._doppler_shift_hz() / 1e6
            self.app.receiver.target_center_mhz = center
            self.last_pass_calc = 0
            self._status('Settings saved')
            return True
        except Exception as exc:
            self._status(f'Settings: {exc}')
            return False

    def set_frequency(self, value, select_fm=False, keep_spectrum=False, center_mhz=None):
        self.app.auto_started = False
        self.app.auto_active_satellite = None
        self.app.auto_active_frequency_mhz = None
        previous_mode = self.demod_combo.get()
        if select_fm:
            self.demod_combo.set('FM')
            if not 6.25 <= float(self.bandwidth.get()) <= 50:
                self.bandwidth.set('12.5')
            self._update_filter_control()
        self.frequency.delete(0, 'end')
        self.frequency.insert(0, f'{value:.6f}')
        if keep_spectrum and center_mhz is not None and not select_fm:
            with self.app.lock:
                self.app.settings['receiver_center_mhz'] = round(center_mhz, 6)
        if self.app.receiver.running and previous_mode != self.demod_combo.get():
            self.connect(recenter=True)
        else:
            self.tune_now(recenter=not keep_spectrum)

    def set_hf_preset(self, frequency_mhz=14.230, mode='USB'):
        was_running = self.app.receiver.running
        self.demod_combo.set(mode)
        self._update_filter_control()
        self.bandwidth.set('3')
        self.frequency.delete(0, 'end')
        self.frequency.insert(0, f'{frequency_mhz:.6f}')
        if was_running:
            self.connect(recenter=True)
        else:
            self.tune_now()

    def _doppler_shift_hz(self):
        cfg = self.app.settings
        if (cfg.get('demod_mode') != 'FM' or not cfg['doppler_enabled'] or
                cfg['home_lat'] is None or cfg['home_lon'] is None):
            return 0.0
        return float(self.app.orbit().get('doppler_hz') or 0.0)

    def tune_now(self, recenter=True):
        if self.save_settings(recenter=recenter):
            self._draw_spectrum()
            if self.app.receiver.running:
                self._status(f'Tuning to {self.app.receiver.target_frequency_mhz:.6f} MHz')
            else:
                self._status('Frequency selected; press CONNECT to receive')

    def rescan(self):
        self.scan_request += 1
        request = self.scan_request
        choice = self.device_combo.current()
        selected_id = self.devices[choice]['id'] if 0 <= choice < len(self.devices) else self.app.settings['device_id']
        previous_ids = {device['id'] for device in self.devices}
        self._status('Scanning SDR devices…')
        def worker():
            try:
                self.device_results.put(('sdr', (request, selected_id, previous_ids,
                                                 self.app.devices())))
            except Exception as exc:
                self.device_results.put(('sdr_error', (request, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def rescan_audio_outputs(self):
        try:
            devices = sd.query_devices()
            self.outputs = [(i, d['name']) for i, d in enumerate(devices)
                            if d['max_output_channels'] > 0 and not
                            ('CABLE' in d['name'].upper() and 'VB-AUDIO' in d['name'].upper())]
            self.output_combo['values'] = [name for _, name in self.outputs]
            if self.outputs:
                wanted = self.app.settings.get('audio_output_name', '')
                preferred = sd.default.device[1]
                self.output_combo.current(next((i for i, (device, name) in enumerate(self.outputs)
                                                if name == wanted),
                                               next((i for i, (device, _) in enumerate(self.outputs)
                                                     if device == preferred), 0)))
        except Exception as exc:
            self._status(f'Audio devices: {exc}')

    def _receiver_audio_sink(self, samples, rate):
        if self.mmsstv_output_stream is not None and self.source != 'SDR WFM':
            self.mmsstv_buffer.push(samples, rate)
        if self.audio_output_stream is not None:
            self.monitor_buffer.push(samples, rate)

    def _start_mmsstv(self):
        if self.mmsstv_output_stream is not None:
            self.mmsstv_output_stream.close()
            self.mmsstv_output_stream = None
        if self.mmsstv is not None:
            self.mmsstv.close()
            self.mmsstv = None
        self.mmsstv_error = ''
        try:
            devices = sd.query_devices()
            cable = next((i for i, device in enumerate(devices)
                          if device['max_output_channels'] > 0 and
                          'CABLE Input' in device['name'] and 'VB-Audio' in device['name'] and
                          sd.query_hostapis(device['hostapi'])['name'] == 'MME'), None)
            if cable is None:
                raise RuntimeError('Install the bundled VB-CABLE driver, then restart Windows')
            try:
                previous_listen = ensure_cable_decoder_only()
                message = ('VB-CABLE feeds MMSSTV only · Windows speaker loopback disabled'
                           if previous_listen is True else
                           'VB-CABLE feeds MMSSTV only · speaker monitor is separate')
                self.cable_route_status.config(text=message, fg=CYAN)
            except Exception:
                self.cable_route_status.config(
                    text='Turn off Windows “Listen to this device” for CABLE Output', fg='#ffbd70')
            self.mmsstv_output_index = cable
            rate = round(devices[cable]['default_samplerate'])
            self.mmsstv_buffer.reset(rate)
            def callback(outdata, frames, time_info, status):
                outdata[:, 0] = self.mmsstv_buffer.read(frames)
            self.mmsstv_output_stream = sd.OutputStream(device=cable, channels=1,
                                                        samplerate=rate, latency='high', callback=callback)
            self.mmsstv_output_stream.start()
            self.app.receiver.monitor_sink = self._receiver_audio_sink
            self.mmsstv = MmsstvBridge(ROOT)
            self.mmsstv.start()
            self.mmsstv_input_status.config(text='MMSSTV input: selecting CABLE Output in Fubars', fg=CYAN)
            self._status('MMSSTV engine starting · SDR audio routed through VB-CABLE')
        except Exception as exc:
            if self.mmsstv_output_stream is not None:
                self.mmsstv_output_stream.close()
                self.mmsstv_output_stream = None
            self.mmsstv_error = str(exc)
            self.mmsstv_input_status.config(text='MMSSTV input: ' + self.mmsstv_error, fg='#ffbd70')
            self._status('MMSSTV: ' + self.mmsstv_error)

    def open_vb_cable(self):
        package = ROOT / 'VB-CABLE' / 'VBCABLE_Driver_Pack46.zip'
        if not package.is_file():
            self._status('VB-CABLE installer is included in the portable ZIP')
            return
        os.startfile(package.parent)
        self._status('Extract VB-CABLE ZIP, run Setup x64 as administrator, then reboot')

    def connect(self, recenter=False):
        if not self.save_settings(recenter=recenter):
            return
        self.app.auto_started = False
        self.app.auto_active_satellite = None
        self.app.auto_active_frequency_mhz = None
        cfg = self.app.settings.copy()
        try:
            self.app.receiver.start(cfg['device_id'], cfg['frequency_mhz'], cfg['gain_db'],
                                    cfg['bandwidth_khz'], cfg['ppm_correction'],
                                    cfg['demod_mode'], cfg['antenna_name'], cfg['noise_filter_enabled'],
                                    cfg['squelch_dbfs'], cfg.get('receiver_center_mhz'))
            self.select_sdr_audio()
            self._status('Opening ' + self.device_combo.get())
        except Exception as exc:
            self._status(str(exc))

    def arm_auto_receive(self):
        previous_mode = self.demod_combo.get()
        selected = str(self.app.settings.get('selected_satellite', '25544'))
        monitored = [str(item) for item in self.app.settings.get('monitored_satellites', [])]
        if selected not in monitored:
            monitored.append(selected)
        with self.app.lock:
            self.app.settings['monitored_satellites'] = monitored
        self.auto_receive.set(True)
        self.doppler.set(True)
        self.demod_combo.set('FM')
        self._update_filter_control()
        if not 6.25 <= float(self.bandwidth.get()) <= 50:
            self.bandwidth.set('12.5')
        if not self.save_settings(recenter=True):
            return
        if self.app.receiver.running and previous_mode != 'FM':
            self.connect(recenter=True)
        self.app.last_auto_attempt = 0
        self._show_tracked_satellites(select_ids=[selected])
        self._status('AUTO armed · radio starts at Home footprint and stops when it leaves')

    def stop_receiver(self):
        self.auto_receive.set(False)
        with self.app.lock:
            self.app.settings['auto_receive'] = False
            write_settings(self.app.settings)
        self.app.auto_started = False
        self.app.auto_active_satellite = None
        self.app.auto_active_frequency_mhz = None
        self.app.receiver.stop()
        self.save_settings()
        self._status('Receiver stopped · AUTO off')

    def update_tle(self):
        self._status('Updating selected satellite orbit data…')
        with self.app.lock:
            self.app.tle_check_status = 'Checking online TLE…'
        def worker():
            result = self.app.update_selected_tle()
            if result:
                self.device_results.put(('tle', result))
        threading.Thread(target=worker, daemon=True).start()

    def _refresh_home_passes(self):
        self.last_home_pass_calc = 0
        self.home_passes = []
        self._show_home_passes()
        self._update_orbit()
        tracked = [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]
        choice = self.app.settings.get('pass_satellites')
        selected = tracked if choice is None else [str(item) for item in choice]
        self._status('Updating orbit data for selected Home pass satellites…')
        def worker():
            results = []
            if '25544' in selected:
                results.append(self.app.update_tle())
            if any(item != '25544' for item in selected):
                results.append(self.app.refresh_catalog())
            self.device_results.put(('tle', ' · '.join(str(item) for item in results if item)))
        threading.Thread(target=worker, daemon=True).start()

    def select_sdr_audio(self):
        self.source = 'SDR ' + self.demod_combo.get()
        self._status('Sending SDR receiver audio to MMSSTV')

    def toggle_monitor(self):
        if self.audio_output_stream:
            self.audio_output_stream.stop()
            self.audio_output_stream.close()
            self.audio_output_stream = None
        if not self.monitor.get():
            self.monitor_status.config(text='Speaker monitor off', fg=MUTED)
            self._status('Speaker monitor off · MMSSTV still receives SDR audio')
            return
        index = self.output_combo.current()
        if index < 0:
            self.monitor.set(False)
            self._status('Select an audio output')
            return
        if self.outputs[index][0] == self.mmsstv_output_index:
            self.monitor.set(False)
            self._status('VB-CABLE is already feeding the MMSSTV decoder')
            return
        try:
            self.monitor_sample_rate = round(sd.query_devices(self.outputs[index][0])['default_samplerate'])
            self.monitor_buffer.reset(self.monitor_sample_rate)
            def callback(outdata, frames, time_info, status):
                outdata[:, 0] = self.monitor_buffer.read(frames) * self.monitor_gain
            self.audio_output_stream = sd.OutputStream(device=self.outputs[index][0], channels=1,
                                                        samplerate=self.monitor_sample_rate,
                                                        blocksize=0, latency='high', callback=callback)
            self.audio_output_stream.start()
            self.app.receiver.monitor_sink = self._receiver_audio_sink
            self.monitor_status.config(text=f'Speaker monitor: {round(self.volume.get())}%  ·  {self.outputs[index][1]}', fg=GREEN)
            self._status('Speaker monitor on: ' + self.outputs[index][1])
        except Exception as exc:
            self.monitor.set(False)
            self.monitor_status.config(text='Speaker monitor unavailable', fg='#ffbd70')
            self._status(f'Audio output failed: {exc}')

    def save_image(self, automatic=False, image=None):
        image = image if image is not None else (self.mmsstv.image if self.mmsstv else None)
        if image is None:
            if not automatic:
                self._status('No decoded image to save')
            return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')[:-3]
        mode_name = (self.mmsstv.mode if self.mmsstv and self.mmsstv.mode else 'SSTV')
        mode = ''.join(c for c in mode_name if c.isalnum() or c in ' -_').strip() or 'SSTV'
        path = RECEIVED_DIR / f'ISS_{stamp}_{mode}.png'
        image.save(path)
        self.last_save = path
        self._refresh_image_thumbnails()
        self._status('Saved ' + path.name)

    def view_image_full(self):
        image = self.mmsstv.image if self.mmsstv else None
        if image is None:
            self._status('No decoded image to view')
            return
        self._show_image_window(image, f'ISS SSTV image · {(self.mmsstv.mode or "SSTV")} · 1:1 pixels')

    def _show_image_window(self, image, title):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(bg=PANEL)
        window.geometry(f'{min(image.width + 32, self.root.winfo_screenwidth() - 80)}x'
                        f'{min(image.height + 45, self.root.winfo_screenheight() - 100)}')
        frame = tk.Frame(window, bg=PANEL)
        frame.pack(fill='both', expand=True)
        horizontal = tk.Scrollbar(frame, orient='horizontal')
        vertical = tk.Scrollbar(frame, orient='vertical')
        canvas = tk.Canvas(frame, bg='#071714', highlightthickness=0,
                           xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        horizontal.config(command=canvas.xview)
        vertical.config(command=canvas.yview)
        horizontal.pack(side='bottom', fill='x')
        vertical.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        photo = ImageTk.PhotoImage(image, master=window)
        canvas.create_image(0, 0, image=photo, anchor='nw')
        canvas.config(scrollregion=(0, 0, image.width, image.height))
        window.image_ref = photo

    def _status(self, message):
        self.status.config(text=message)

    def _tick(self):
        try:
            self._poll()
            revision = (id(self.app.catalog.records), self.app.catalog.status)
            if revision != self.catalog_revision:
                self.catalog_revision = revision
                self._filter_satellites()
                self._show_tracked_satellites()
        except Exception as exc:
            self._status(f'UI: {exc}')
        self.tick_after_id = self.root.after(150, self._tick)

    def _poll(self):
        auto_label = 'AUTO ON' if self.auto_receive.get() else 'AUTO'
        if self.auto_button.cget('text') != auto_label:
            self.auto_button.config(text=auto_label,
                                    bg='#146c4b' if self.auto_receive.get() else '#053628')
        while not self.device_results.empty():
            kind, value = self.device_results.get_nowait()
            if kind == 'sdr':
                request, selected_id, previous_ids, devices = value
                if request != self.scan_request:
                    continue
                self.devices = devices
                self.device_combo['values'] = [d['label'] for d in devices]
                new_device = next((i for i, d in enumerate(devices)
                                   if d['id'] not in previous_ids), None)
                selected = (new_device if new_device is not None else
                            next((i for i, d in enumerate(devices) if d['id'] == selected_id), 0))
                if devices:
                    self.device_combo.current(selected)
                    self._update_antenna_choices()
                    self._status(('New SDR found: ' + devices[selected]['label']) if new_device is not None
                                 else f'{len(devices)} SDR device(s) found')
                else:
                    self.device_combo.set('')
                    self._status('No SDR found. Check USB driver and reconnect device.')
            elif kind == 'sdr_error':
                request, message = value
                if request == self.scan_request:
                    self._status('SDR scan failed: ' + message)
            elif kind == 'passes':
                request, key, current, upcoming = value
                if request == self.pass_request and key == self.last_tle_key:
                    self.pass_calculating = False
                    self.pass_current, self.pass_next = current, upcoming
            elif kind == 'home_passes':
                request, key, passes = value
                if request == self.home_pass_request and key == self.last_home_key:
                    self.home_pass_calculating = False
                    self.home_passes = passes
                    self._show_home_passes()
            elif kind == 'tle':
                self._status(value)
            elif kind == 'catalog':
                self.catalog_revision = None
                self._filter_satellites()
                self._show_tracked_satellites()
                self._status(value)
            elif kind == 'sstv_freq':
                request, tracked, dial_before, choices, status = value
                if (request == self.sstv_lookup_request and tracked ==
                        [str(item) for item in self.app.settings.get('tracked_satellites', ['25544'])]):
                    self._show_sstv_frequencies(dial_before, choices, status)
            else:
                self._status(value)
        auto_target = (self.app.auto_active_satellite if self.app.auto_started else None)
        if auto_target != self.displayed_auto_target:
            self.displayed_auto_target = auto_target
            if auto_target is not None:
                frequency = self.app.auto_active_frequency_mhz
                self.frequency.delete(0, 'end')
                self.frequency.insert(0, f'{frequency:.6f}')
                self.demod_combo.set('FM')
                self._show_tracked_satellites()
                for index, row in enumerate(self.sstv_rows):
                    if (row['satellite_id'] == auto_target and
                            abs(row['frequency_mhz'] - frequency) < .000001):
                        self.sstv_frequency_combo.current(index)
                        break
                self._status(f'Auto receiving {self.app.selected_tle(catalog_number=auto_target)["name"]} · {frequency:.6f} MHz')
        data = self.app.receiver.pull_raw()
        if data['running']:
            self.rx_status.config(text='ON AIR', fg=GREEN)
            self.header_status.config(text=f'{self.demod_combo.get()} RECEIVER  •  ON AIR')
        else:
            self.rx_status.config(text='Receiver stopped', fg=MUTED)
            self.header_status.config(text=f'{self.demod_combo.get()} RECEIVER  •  STANDBY')
        if data['error'] and data['error'] != self.last_error:
            self.last_error = data['error']
            self._status(data['error'])
        if data['running'] and data['tuned_frequency_mhz'] is not None:
            actual_center = data['tuned_frequency_mhz']
            changed = (self.spectrum_center_mhz is not None and
                       abs(actual_center - self.spectrum_center_mhz) > .00001)
            self.spectrum_center_mhz = actual_center
            if self.spectrum_pending_center_mhz is not None:
                if (abs(actual_center - self.spectrum_pending_center_mhz) < .00001 or
                        time.time() - self.spectrum_pending_started > 5):
                    self.spectrum_pending_center_mhz = None
                    self.spectrum_pan_pixels = 0
                    changed = True
            if changed:
                self.waterfall_db = None
                self.spectrum_ema = None
                self.spectrum_peak = None
                self.spectrum_image = None
            self.tuned.config(text=f"Driver center: {data['tuned_frequency_mhz']:.6f} MHz")
        elif not data['running']:
            self.spectrum_center_mhz = None
            self.spectrum_pending_center_mhz = None
            if self.spectrum_drag is None:
                self.spectrum_pan_pixels = 0
            self.tuned.config(text='Driver center: —')
        applied_gain = data.get('applied_gain_db')
        applied_ppm = data.get('applied_ppm_correction')
        self.ppm_status.config(text=(f'Applied PPM: {applied_ppm:+d}'
                                     if data['running'] and applied_ppm is not None else
                                     'Applied PPM: —'))
        gain_detail = data.get('gain_detail')
        self.gain_status.config(text=(gain_detail if data['running'] and gain_detail else
                                      f'Applied RF gain: {applied_gain:.1f} dB'
                                      if applied_gain is not None and data['running'] else
                                      'Applied RF gain: —'))
        level = data.get('signal_level_dbfs')
        if data['running'] and level is not None:
            state = ('OFF' if self.squelch.get() <= -120 else
                     'OPEN' if data.get('squelch_open', True) else 'CLOSED')
            self.squelch_status.config(text=f'Signal: {level:.0f} dBFS  ·  squelch {state}',
                                       fg=GREEN if state != 'CLOSED' else MUTED)
        else:
            self.squelch_status.config(text='Signal: —  ·  squelch off', fg=MUTED)
        try:
            selected = float(self.frequency.get())
            shift = self._doppler_shift_hz()
            self.tune_detail.config(text=f'{self.demod_combo.get()} dial: {selected:.6f} MHz  ·  Doppler: {shift:+.0f} Hz  ·  PPM: {self.ppm.get()}')
        except ValueError:
            pass
        if data['iq']:
            self.last_iq, self.last_rate = data['iq'], data['iq_rate']
        frames = data.get('iq_frames', [])
        if self.mmsstv is not None:
            completed = self.mmsstv.poll()
            if completed is not None:
                self.save_image(automatic=True, image=completed)
            state = self.mmsstv.status.get('state')
            mode = self.mmsstv.mode
            if state == 'error':
                self.mmsstv_error = self.mmsstv.status.get('message', 'Engine stopped')
                self.mmsstv_input_status.config(text='MMSSTV input: ' + self.mmsstv_error, fg='#ffbd70')
            elif state == 'ready':
                input_name = self.mmsstv.status.get('input_device', 'CABLE Output')
                self.mmsstv_input_status.config(
                    text='MMSSTV input set inside Fubars: ' + input_name, fg=CYAN)
            if mode:
                self._show_detected_mode(mode)
            else:
                self._show_detected_mode()
            if self.mmsstv_error:
                self.mode_status.config(text='MMSSTV: ' + self.mmsstv_error)
            elif state == 'starting':
                self.mode_status.config(text='Starting MMSSTV engine…')
            elif not data['running']:
                self.mode_status.config(text='Receiver stopped · connect an SDR to decode')
            elif self.source == 'SDR WFM':
                self.mode_status.config(text='WFM broadcast audio · SSTV decoder paused')
            elif self.mmsstv.status.get('receiving'):
                self.mode_status.config(text=f'Receiving {mode or "SSTV"} with MMSSTV')
            else:
                self.mode_status.config(text='MMSSTV listening for VIS')
        else:
            self.mode_status.config(text='MMSSTV: ' + (self.mmsstv_error or 'Engine unavailable'))
        self.image_source_status.config(text='MMSSTV audio: SDR receiver via VB-CABLE')
        self.clock.config(text=datetime.now().astimezone().strftime('PC LOCAL  %H:%M:%S  ·  %Y-%m-%d'))
        self._update_orbit()
        if frames or self.spectrum_image is None:
            self._draw_spectrum(frames)
        if int(time.time() * 2) != getattr(self, '_last_draw', None):
            self._last_draw = int(time.time() * 2)
            self._draw_map()
            self._draw_decoder()

    def _show_detected_mode(self, mode=None, code=None):
        if mode is None:
            self.image_title.config(text='RECEIVED SSTV IMAGE  ·  MODE: WAITING FOR VIS')
            self.detected_mode_status.config(text='Detected mode: waiting for VIS')
        else:
            self.image_title.config(text=f'RECEIVED SSTV IMAGE  ·  MODE: {mode.upper()}')
            detail = f'Detected mode: {mode} (VIS {code})' if code is not None else f'Mode: {mode} (sync)'
            self.detected_mode_status.config(text=detail)

    def _update_orbit(self):
        info = self.app.orbit()
        tle = info['tle']
        lines = tle.get('lines', [])
        name = tle.get('name', 'Satellite')
        self.map_title.config(text=f'{name.upper()} WORLD TRACKER   •   SOLID: NEXT PASS   •   DASHED: CURRENT PASS')
        self.selected_satellite_label.config(text=f"Selected: {name} · NORAD {self.app.settings.get('selected_satellite', '25544')}")
        if valid_orbit(lines):
            epoch = orbit_epoch(lines)
            self.tle_status.config(text=f'TLE {epoch:%d %b %H:%M}Z')
            self.tle_epoch_text.config(text=f'Orbit epoch: {epoch:%Y-%m-%d %H:%M} UTC')
        else:
            self.tle_status.config(text='Orbit data unavailable')
            self.tle_epoch_text.config(text='Orbit epoch: unavailable')
        checked = info['tle_checked_at']
        checked_local = (datetime.fromisoformat(checked).astimezone().strftime('%d %b %H:%M')
                         if checked else None)
        status = info['tle_check_status']
        self.tle_check_text.config(text=(f'{status} · {checked_local} PC time' if checked_local else status))
        sat = info['iss']
        if sat:
            self.position_text.config(text=f"{name}  {sat['lat']:+.2f}°  {sat['lon']:+.2f}°  ·  {sat['alt_km']:.0f} km")
        else:
            self.position_text.config(text=info['tle_error'] or 'Waiting for orbit data')
        cfg = self.app.settings
        if cfg['home_lat'] is None or cfg['home_lon'] is None:
            auto_status = 'Set and save Home coordinates for pass start and Doppler'
        elif not cfg['auto_receive']:
            auto_status = 'Auto pass start off · connect receiver anytime'
        elif not cfg.get('monitored_satellites'):
            auto_status = 'Select satellites to auto start on the Satellites tab'
        elif self.app.receiver.running:
            auto_status = ('AUTO receiver on · stops when its footprint leaves Home'
                           if self.app.auto_started else
                           'Manual receiver on · Doppler stops when its footprint leaves Home')
        else:
            monitored = {str(number) for number in cfg['monitored_satellites']}
            visible = [item for item in info['tracked'] if item['id'] in monitored and
                       footprint_contains(item['position'], cfg['home_lat'], cfg['home_lon'])]
            configured = [item for item in visible if configured_sstv_frequency(cfg, item['id']) is not None]
            if visible and not configured:
                auto_status = 'Home inside monitored footprint · choose its SSTV transponder'
            elif configured and not cfg['device_id']:
                auto_status = 'Home inside monitored footprint · select an SDR receiver'
            elif configured and self.app.receiver.error and time.time() - self.app.last_auto_attempt < 35:
                auto_status = 'Auto receiver start failed: ' + self.app.receiver.error
            elif configured:
                auto_status = 'Home inside monitored footprint · starting receiver'
            else:
                auto_status = 'Receiver off · waiting for a monitored footprint to reach Home'
        self.auto_receive_status.config(text=auto_status)
        look = info['look']
        if look:
            self.look_text.config(text=f"Az {look['azimuth_deg']:.1f}°   El {look['elevation_deg']:+.1f}°   Range {look['range_km']:.0f} km")
            if not cfg['doppler_enabled']:
                doppler_text = 'Doppler off'
            elif not self.app.receiver.running:
                doppler_text = 'Doppler idle · receiver stopped'
            elif sat and cfg['home_lat'] is not None and cfg['home_lon'] is not None and \
                    footprint_contains(sat, cfg['home_lat'], cfg['home_lon']):
                doppler_text = f"Doppler active {(info['doppler_hz'] or 0):+.0f} Hz"
            else:
                doppler_text = 'Doppler idle · outside footprint'
            self.doppler_text.config(text=doppler_text)
        now = datetime.now(timezone.utc)
        key = tuple(lines) if isinstance(lines, list) else tuple(sorted(lines.items()))
        if key != self.last_tle_key:
            self.pass_current, self.pass_next = [], []
            self.last_tle_key, self.last_pass_calc = key, 0
            self.pass_calculating = False
            self.pass_request += 1
        if self.pass_next and datetime.fromisoformat(self.pass_next[0]['time'].replace('Z', '+00:00')) <= now:
            self.pass_current, self.pass_next = self.pass_next, []
            self.last_pass_calc = 0
        if valid_orbit(lines) and not self.pass_calculating and (self.last_pass_calc == 0 or time.time() - self.last_pass_calc > 60):
            self.last_pass_calc = time.time()
            self.pass_calculating = True
            self.pass_request += 1
            request = self.pass_request
            def worker():
                current, upcoming = pass_paths(lines)
                self.device_results.put(('passes', (request, key, current, upcoming)))
            threading.Thread(target=worker, daemon=True).start()

        tracked = [str(item) for item in cfg.get('tracked_satellites', ['25544'])]
        chosen = cfg.get('pass_satellites')
        chosen = tracked if chosen is None else [str(item) for item in chosen if str(item) in tracked]
        pass_sources = []
        for catalog_number in dict.fromkeys(chosen):
            item = self.app.selected_tle(catalog_number=catalog_number)
            if valid_orbit(item.get('lines', [])):
                pass_sources.append({'id': catalog_number, 'name': item['name'], 'lines': item['lines']})
        self.home_pass_sources = pass_sources
        home_key = (tuple((item['id'], item['name'], repr(item['lines'])) for item in pass_sources),
                    tuple(chosen), cfg['home_lat'], cfg['home_lon'])
        if home_key != self.last_home_key:
            self.last_home_key, self.last_home_pass_calc = home_key, 0
            self.home_passes = []
            self.home_pass_calculating = False
            self.home_pass_request += 1
            self._show_home_passes()
        while self.home_passes and self.home_passes[0]['los'] <= now:
            self.home_passes.pop(0)
            self.last_home_pass_calc = 0
            self._show_home_passes()
        if (pass_sources and cfg['home_lat'] is not None and cfg['home_lon'] is not None and
                not self.home_pass_calculating and
                (self.last_home_pass_calc == 0 or time.time() - self.last_home_pass_calc > 300)):
            self.last_home_pass_calc = time.time()
            self.home_pass_calculating = True
            self.home_pass_request += 1
            request = self.home_pass_request
            latitude, longitude = cfg['home_lat'], cfg['home_lon']
            def home_worker():
                passes = next_mapped_home_passes(pass_sources, latitude, longitude, count=4)
                self.device_results.put(('home_passes', (request, home_key, passes)))
            threading.Thread(target=home_worker, daemon=True).start()
        self._orbit_info = info

    def _show_home_passes(self):
        if self.app.settings['home_lat'] is None or self.app.settings['home_lon'] is None:
            rows = ['Set home GPS coordinates to list passes']
        elif not self.home_pass_sources:
            rows = ['Select mapped satellites with orbit data to list passes']
        elif not self.home_passes:
            rows = ['Calculating upcoming passes…' if self.home_pass_calculating or
                    self.last_home_pass_calc == 0 else 'No upcoming passes for selected satellites']
        else:
            rows = []
            for index, item in enumerate(self.home_passes[:4], 1):
                aos = item['aos'].astimezone()
                los = item['los'].astimezone()
                current = item['aos'] <= datetime.now(timezone.utc) < item['los']
                rows.append(f"{index}  {item['satellite_name']} · {'NOW · ' if current else ''}"
                            f"{aos:%a %d %b %H:%M}–{los:%H:%M}  max {item['max_elevation_deg']:.0f}°")
        for index, label in enumerate(self.pass_labels):
            label.config(text=rows[index] if index < len(rows) else '',
                         fg='#54bfff' if index == 0 else GREEN)

    @staticmethod
    def _make_heat_lut():
        stops = np.array([0, 45, 95, 145, 195, 230, 255])
        colors = np.array([[2, 8, 20], [5, 31, 70], [5, 91, 130],
                           [0, 178, 155], [65, 235, 113], [255, 227, 67], [255, 96, 45]])
        values = np.arange(256)
        return np.stack([np.interp(values, stops, colors[:, channel]) for channel in range(3)], axis=1).astype(np.uint8)

    def _draw_floor_legend(self, _event=None):
        height = self.floor_legend.winfo_height()
        self.floor_legend.delete('all')
        if height < 2:
            return
        for y in range(height):
            rgb = self.heat_lut[round(255 * (1 - y / (height - 1)))]
            color = f'#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}'
            self.floor_legend.create_line(0, y, 13, y, fill=color)

    def _display_floor_changed(self, value):
        with self.app.lock:
            self.app.settings['spectrum_floor_dbfs'] = float(value)
        if self.spectrum_image is not None:
            self._draw_spectrum()

    def _receiver_center(self):
        if self.spectrum_center_mhz is not None:
            return self.spectrum_center_mhz
        held = self.app.settings.get('receiver_center_mhz')
        if held is not None:
            return float(held)
        return float(self.frequency.get()) + (StreamingHFDemodulator.offset_hz / 1e6
                                              if self.demod_combo.get() in HF_MODES else 0)

    def _limit_spectrum_dial(self, frequency, center, doppler_mhz=None):
        if doppler_mhz is None:
            doppler_mhz = self._doppler_shift_hz() / 1e6
        half_span = max(0, self.last_rate * 0.4 - 2000) / 1e6
        bandwidth = float(self.bandwidth.get()) / 1000
        mode = self.demod_combo.get()
        if mode == 'USB':
            low, high = center - half_span, center + half_span - bandwidth
        elif mode == 'LSB':
            low, high = center - half_span + bandwidth, center + half_span
        else:
            low, high = center - half_span + bandwidth / 2, center + half_span - bandwidth / 2
        if low > high:
            return float(self.frequency.get())
        return max(.001, min(1700, round(max(low, min(high, frequency + doppler_mhz)) - doppler_mhz, 6)))

    def _set_spectrum_center(self, center, pan_pixels=0):
        if not .001 <= center <= 1700:
            self._draw_spectrum()
            return
        center = round(center, 6)
        with self.app.lock:
            self.app.settings['receiver_center_mhz'] = center
            write_settings(self.app.settings)
        self.app.receiver.target_center_mhz = center
        if self.app.receiver.running:
            self.spectrum_pending_center_mhz = center
            self.spectrum_pending_started = time.time()
            self.spectrum_pan_pixels = pan_pixels
        self._draw_spectrum()
        self._status(f'Spectrum center: {center:.6f} MHz')

    @staticmethod
    def _shift_spectrum_data(values, pixels):
        if values is None or not pixels:
            return values
        width = values.shape[-1]
        shifted = np.full_like(values, -120)
        if 0 < pixels < width:
            shifted[..., pixels:] = values[..., :width - pixels]
        elif -width < pixels < 0:
            shifted[..., :pixels] = values[..., -pixels:]
        return shifted

    def _draw_spectrum(self, frames=()):
        width, height = self.spectrum.winfo_width(), self.spectrum.winfo_height()
        if width < 50 or height < 50:
            return
        split = max(90, height // 2)
        waterfall_height = height - split
        if self.waterfall_db is None or self.waterfall_db.shape != (waterfall_height, width):
            self.waterfall_db = np.full((waterfall_height, width), -120, dtype=np.float32)
            self.spectrum_ema = None
            self.spectrum_peak = None
        for frame in frames[-8:]:
            values = np.frombuffer(frame, dtype=np.uint8)
            n = min(8192, len(values) // 2)
            if n < 512:
                continue
            iq = ((values[:n*2:2].astype(np.float32) - 127.5) +
                  1j * (values[1:n*2:2].astype(np.float32) - 127.5)) / 127.5
            iq -= iq.mean()
            window = np.hanning(n)
            power = np.abs(np.fft.fftshift(np.fft.fft(iq * window))) ** 2
            edges = np.linspace(0, n, width + 1, dtype=np.int32)
            binned = np.add.reduceat(power, edges[:-1]) / np.maximum(1, np.diff(edges))
            db = 10 * np.log10(binned / window.sum() ** 2 + 1e-12)
            self.spectrum_ema = db if self.spectrum_ema is None else self.spectrum_ema * .72 + db * .28
            self.spectrum_peak = db if self.spectrum_peak is None else np.maximum(db, self.spectrum_peak - .7)
            if waterfall_height >= 2:
                self.waterfall_db[2:] = self.waterfall_db[:-2]
                self.waterfall_db[:2] = db
        floor = self.spectrum_floor_dbfs.get()
        span = -20 - floor
        pan_pixels = round(self.spectrum_pan_pixels)
        trace_db = self._shift_spectrum_data(self.spectrum_ema, pan_pixels)
        peak_db = self._shift_spectrum_data(self.spectrum_peak, pan_pixels)
        waterfall_db = self._shift_spectrum_data(self.waterfall_db, pan_pixels)
        self.spectrum_bins = (np.clip((trace_db - floor) / span, 0, 1)
                              if self.spectrum_ema is not None else None)
        heat = np.clip((waterfall_db - floor) / span, 0, 1)
        self.waterfall = self.heat_lut[(heat * 255).astype(np.uint8)]
        canvas = Image.new('RGB', (width, height), '#031411')
        canvas.paste(Image.fromarray(self.waterfall), (0, split))
        draw = ImageDraw.Draw(canvas)
        top, bottom = 12, split - 21
        for level in np.linspace(floor, -20, 5):
            y = bottom - (level - floor) / span * (bottom - top)
            draw.line((0, y, width, y), fill='#113c38')
            draw.text((4, y + 1), f'{level:.0f} dBFS', fill='#6fa49a')
        try:
            receiver_center = self._receiver_center()
        except ValueError:
            receiver_center = 437.55
        center = receiver_center - pan_pixels * self.last_rate / max(1, width) / 1e6
        self.spectrum_display_center_mhz = center
        for i in range(9):
            x = round(i * (width - 1) / 8)
            draw.line((x, 0, x, bottom), fill='#16423a')
            label = f'{center + (i/8 - .5) * self.last_rate / 1e6:.3f}'
            lx = max(2, min(width - 46, x - 21))
            draw.text((lx, split - 16), label, fill='#a8d6c7')
        try:
            bandwidth = float(self.bandwidth.get())
        except ValueError:
            bandwidth = 12.5
        try:
            dial = (self.spectrum_preview_frequency_mhz if self.spectrum_preview_frequency_mhz is not None
                    else float(self.frequency.get())) + self._doppler_shift_hz() / 1e6
            center_x = width / 2 + (dial - center) * 1e6 * width / max(1, self.last_rate)
        except ValueError:
            center_x = width / 2
        mode = self.demod_combo.get()
        band_pixels = max(2, width * bandwidth * 1000 / max(1, self.last_rate))
        if mode == 'USB':
            band_left, band_right = center_x, center_x + band_pixels
        elif mode == 'LSB':
            band_left, band_right = center_x - band_pixels, center_x
        else:
            band_left, band_right = center_x - band_pixels / 2, center_x + band_pixels / 2
        self.spectrum_marker_x = center_x
        self.spectrum_passband = (band_left, band_right)
        overlay = Image.new('RGBA', canvas.size)
        shade = ImageDraw.Draw(overlay)
        shade.rectangle((band_left, top, band_right, bottom), fill=(22, 255, 130, 70))
        shade.line((band_left, top, band_left, bottom), fill=(44, 221, 130, 160))
        shade.line((band_right, top, band_right, bottom), fill=(44, 221, 130, 160))
        canvas = Image.alpha_composite(canvas.convert('RGBA'), overlay).convert('RGB')
        draw = ImageDraw.Draw(canvas)
        if self.spectrum_bins is not None:
            points = [(x, int(bottom - value * (bottom - top))) for x, value in enumerate(self.spectrum_bins)]
            peak = np.clip((peak_db - floor) / span, 0, 1)
            peak_points = [(x, int(bottom - value * (bottom - top))) for x, value in enumerate(peak)]
            draw.line(peak_points, fill='#a89343', width=1)
            draw.line(points, fill='#44ffb2', width=2)
        draw.line((center_x, 0, center_x, split), fill='#ffe36a', width=2)
        if mode in ('USB', 'LSB'):
            label_x = center_x + 5 if mode == 'USB' else center_x - 29
            draw.text((label_x, top + 2), mode, fill='#8cffc5')
        draw.line((0, split, width, split), fill='#16745e', width=2)
        self.spectrum_image = ImageTk.PhotoImage(canvas)
        self.spectrum.delete('all')
        self.spectrum.create_image(0, 0, image=self.spectrum_image, anchor='nw')
        self._draw_frequency_display()
        self.spectrum_info.config(text=f'{center:.6f} MHz center  ·  {self.last_rate/1000:.0f} kHz span  ·  drag trace to pan / line to tune')

    def _draw_frequency_display(self):
        self.spectrum.delete('frequency_overlay')
        self.frequency_hitboxes = []
        try:
            display = f'{(self.spectrum_preview_frequency_mhz if self.spectrum_preview_frequency_mhz is not None else float(self.frequency.get())):.6f}'
        except ValueError:
            display = '---.------'
        self.frequency_display_text = display
        font = self.frequency_display_font
        right = self.spectrum.winfo_width() - 16
        left = right - sum(font.measure(char) for char in display)
        top = 16
        bottom = top + font.metrics('linespace')
        self.spectrum.create_rectangle(left - 10, 7, right + 9, bottom + 20,
                                       fill='#001b15', outline='#187454',
                                       tags='frequency_overlay')
        point = display.index('.')
        x = left
        for index, char in enumerate(display):
            char_width = font.measure(char)
            if char.isdigit():
                step_mhz = 10 ** (point - index - 1 if index < point else point - index)
            else:
                step_mhz = None
            if step_mhz is not None:
                self.frequency_hitboxes.append((index, x, x + char_width, top, bottom, step_mhz))
            if index == self.frequency_hover_index:
                self.spectrum.create_rectangle(x - 1, top - 2, x + char_width + 1, bottom + 2,
                                               fill='#165f43', outline='', tags='frequency_overlay')
            self.spectrum.create_text(x, top, text=char, font=font, anchor='nw',
                                      fill='#ffe86a' if index == self.frequency_hover_index else GREEN,
                                      tags='frequency_overlay')
            x += char_width
        self.spectrum.create_text(right, bottom + 3, text='MHz  ·  HOVER DIGIT + WHEEL',
                                  font=('Segoe UI', 8), fill=CYAN, anchor='ne',
                                  tags='frequency_overlay')

    def _frequency_hitbox(self, event):
        x, y = getattr(event, 'x', None), getattr(event, 'y', None)
        if x is None or y is None:
            return None
        return next((box for box in self.frequency_hitboxes
                     if box[1] <= x < box[2] and box[3] <= y <= box[4]), None)

    def _spectrum_hover(self, event):
        if self.spectrum_drag is not None:
            return
        hit = self._frequency_hitbox(event)
        index = hit[0] if hit else None
        if index != self.frequency_hover_index:
            self.frequency_hover_index = index
            self._draw_frequency_display()
        if hit:
            cursor = 'hand2'
        elif (event.y < max(90, self.spectrum.winfo_height() // 2) and
              abs(event.x - self.spectrum_marker_x) <= 10):
            cursor = 'sb_h_double_arrow'
        else:
            cursor = 'fleur'
        self.spectrum.config(cursor=cursor)

    def _spectrum_leave(self, _event):
        if self.spectrum_drag is not None:
            return
        if self.frequency_hover_index is not None:
            self.frequency_hover_index = None
            self._draw_frequency_display()
        self.spectrum.config(cursor='crosshair')

    def _spectrum_press(self, event):
        if self._frequency_hitbox(event):
            return 'break'
        try:
            frequency = float(self.frequency.get())
        except ValueError:
            return 'break'
        marker = (event.y < max(90, self.spectrum.winfo_height() // 2) and
                  abs(event.x - self.spectrum_marker_x) <= 10)
        self.spectrum_drag = {'kind': 'marker' if marker else 'trace',
                              'x': event.x, 'frequency': frequency,
                              'center': self._receiver_center(),
                              'doppler_mhz': self._doppler_shift_hz() / 1e6,
                              'moved': False}
        self.spectrum.config(cursor='sb_h_double_arrow' if marker else 'fleur')
        return 'break'

    def _spectrum_drag_motion(self, event):
        drag = self.spectrum_drag
        if drag is None:
            return
        pixels = event.x - drag['x']
        if abs(pixels) < 3 and not drag['moved']:
            return 'break'
        drag['moved'] = True
        width = max(1, self.spectrum.winfo_width())
        pixels = max(-width, min(width, pixels))
        delta_mhz = pixels * self.last_rate / width / 1e6
        if drag['kind'] == 'marker':
            self.spectrum_preview_frequency_mhz = self._limit_spectrum_dial(
                drag['frequency'] + delta_mhz, drag['center'], drag['doppler_mhz'])
        else:
            self.spectrum_pan_pixels = pixels
        self._draw_spectrum()
        return 'break'

    def _spectrum_release(self, event):
        drag = self.spectrum_drag
        if drag is None:
            return
        self._spectrum_drag_motion(event)
        selected = self.spectrum_preview_frequency_mhz
        pixels = 0
        if drag['kind'] == 'trace' and drag['moved']:
            width = max(1, self.spectrum.winfo_width())
            pixels = max(-width, min(width, event.x - drag['x']))
            selected = round(drag['center'] - pixels * self.last_rate / width / 1e6, 6)
        self.spectrum_drag = None
        self.spectrum_pan_pixels = 0
        self.spectrum_preview_frequency_mhz = None
        if drag['moved'] and selected is not None and .001 <= selected <= 1700:
            if drag['kind'] == 'marker':
                self.set_frequency(selected, keep_spectrum=True, center_mhz=drag['center'])
            else:
                self._set_spectrum_center(selected, pixels)
        elif not drag['moved'] and drag['kind'] == 'trace':
            self._spectrum_tune(event)
        else:
            self._draw_spectrum()
        self._spectrum_hover(event)
        return 'break'

    def _spectrum_tune(self, event):
        if self._frequency_hitbox(event):
            return 'break'
        width = max(1, self.spectrum.winfo_width())
        offset = (event.x / width - .5) * self.last_rate / 1e6
        try:
            center = self._receiver_center()
            selected = self._limit_spectrum_dial(center + offset - self._doppler_shift_hz() / 1e6,
                                                 center)
            self.set_frequency(selected, keep_spectrum=True, center_mhz=center)
        except ValueError:
            pass

    def _spectrum_wheel(self, event):
        hit = self._frequency_hitbox(event)
        remainder = getattr(self, '_spectrum_wheel_remainder', 0) + event.delta
        steps = int(remainder / 120)
        self._spectrum_wheel_remainder = remainder - steps * 120
        if steps:
            try:
                current = float(self.frequency.get())
            except ValueError:
                return 'break'
            step_mhz = hit[5] if hit else (10 if event.state & 0x0001 else 100) / 1e6
            selected = round(current + steps * step_mhz, 6)
            if .001 <= selected <= 1700:
                center = self._receiver_center()
                if hit and abs(self._limit_spectrum_dial(selected, center) - selected) > .0000005:
                    self.set_frequency(selected)
                else:
                    self.set_frequency(self._limit_spectrum_dial(selected, center),
                                       keep_spectrum=True, center_mhz=center)
        return 'break'

    @staticmethod
    def _xy(lat, lon, width, height, x0, y0):
        return x0 + (lon + 180) / 360 * width, y0 + (90 - lat) / 180 * height

    def _map_drag_start(self, event):
        self._map_drag_origin = (event.x, event.y, self.map_pan_x, self.map_pan_y)

    def _map_drag(self, event):
        if not hasattr(self, '_map_drag_origin'):
            return
        x, y, pan_x, pan_y = self._map_drag_origin
        self.map_pan_x = pan_x + event.x - x
        self.map_pan_y = pan_y + event.y - y
        self._draw_map()

    def _map_wheel(self, event):
        cw, ch = self.map_canvas.winfo_width(), self.map_canvas.winfo_height()
        if cw < 50 or ch < 50:
            return
        base_width = min(cw, ch * 2)
        old_width = base_width * self.map_zoom
        old_height = old_width / 2
        old_x = (cw - old_width) / 2 + self.map_pan_x
        old_y = (ch - old_height) / 2 + self.map_pan_y
        longitude_fraction = (event.x - old_x) / old_width
        latitude_fraction = (event.y - old_y) / old_height
        self.map_zoom = max(1.0, min(4.0, self.map_zoom * (1.2 if event.delta > 0 else 1 / 1.2)))
        new_width = base_width * self.map_zoom
        new_height = new_width / 2
        self.map_pan_x = event.x - longitude_fraction * new_width - (cw - new_width) / 2
        self.map_pan_y = event.y - latitude_fraction * new_height - (ch - new_height) / 2
        self._draw_map()

    def _map_reset(self, _event):
        self.map_zoom = 1.0
        self.map_pan_x = self.map_pan_y = 0.0
        self._draw_map()

    def _path(self, draw, points, width, height, x0, y0, color, dashed=False):
        if len(points) < 2:
            return
        dash_phase = 0.0
        for a, b in zip(points[:-1], points[1:]):
            lon_b = b['lon']
            if lon_b - a['lon'] > 180:
                lon_b -= 360
            elif lon_b - a['lon'] < -180:
                lon_b += 360
            p = self._xy(a['lat'], a['lon'], width, height, x0, y0)
            q = self._xy(b['lat'], lon_b, width, height, x0, y0)
            if dashed:
                length = math.dist(p, q)
                walked = 0.0
                while length > 0 and walked < length:
                    phase = (dash_phase + walked) % 14
                    on = phase < 8
                    step = max(1e-6, min(length - walked, (8 if on else 14) - phase))
                    start, end = walked / length, (walked + step) / length
                    if on:
                        for shift in (-width, 0, width):
                            draw.line((p[0] + shift + (q[0]-p[0])*start, p[1] + (q[1]-p[1])*start,
                                       p[0] + shift + (q[0]-p[0])*end, p[1] + (q[1]-p[1])*end), fill=color, width=2)
                    walked += step
                dash_phase = (dash_phase + length) % 14
            else:
                for shift in (-width, 0, width):
                    draw.line((p[0]+shift, p[1], q[0]+shift, q[1]), fill=color, width=2)

    def _draw_map(self):
        cw, ch = self.map_canvas.winfo_width(), self.map_canvas.winfo_height()
        if cw < 50 or ch < 50:
            return
        width = round(min(cw, ch * 2) * self.map_zoom)
        height = round(width / 2)
        x0 = y0 = 0
        if self.map_scaled_size != (width, height):
            self.map_scaled = self.map_base.resize((width, height), Image.Resampling.BILINEAR)
            self.map_scaled_size = (width, height)
        world = self.map_scaled.copy()
        draw = ImageDraw.Draw(world, 'RGBA')
        for lon in range(-180, 181, 30):
            x, _ = self._xy(0, lon, width, height, x0, y0)
            draw.line((x, y0, x, y0 + height), fill=(190, 240, 220, 90))
        for lat in range(-60, 91, 30):
            _, y = self._xy(lat, 0, width, height, x0, y0)
            draw.line((x0, y, x0 + width, y), fill=(190, 240, 220, 90))
        info = getattr(self, '_orbit_info', {})
        sat = info.get('iss')
        if sat:
            lat0, lon0, angle = sat['lat'], sat['lon'], sat['footprint_deg']
            ring = []
            for bearing in range(0, 361, 3):
                lat1, lon1, br = map(math.radians, (lat0, lon0, bearing))
                delta = math.radians(angle)
                la = math.asin(math.sin(lat1)*math.cos(delta) + math.cos(lat1)*math.sin(delta)*math.cos(br))
                lo = lon1 + math.atan2(math.sin(br)*math.sin(delta)*math.cos(lat1), math.cos(delta)-math.sin(lat1)*math.sin(la))
                ring.append({'lat': math.degrees(la), 'lon': (math.degrees(lo)+180)%360-180})
            self._path(draw, ring, width, height, x0, y0, (255, 215, 30, 240))
        self._path(draw, self.pass_next, width, height, x0, y0, (70, 225, 255, 255))
        self._path(draw, self.pass_current, width, height, x0, y0, (30, 255, 160, 255), dashed=True)
        active_id = str(self.app.settings.get('selected_satellite', '25544'))
        for item in info.get('tracked', []):
            if item['id'] == active_id:
                continue
            other = item['position']
            x, y = self._xy(other['lat'], other['lon'], width, height, x0, y0)
            draw.polygon([(x-4,y-5),(x+4,y-5),(x+5,y+5),(x-5,y+5)],
                         fill=(60,210,255,255), outline=(0,0,0,255))
            draw.rectangle((x-13,y-3,x-7,y+3), fill=(60,210,255,255))
            draw.rectangle((x+7,y-3,x+13,y+3), fill=(60,210,255,255))
            label = item['name'][:24]
            label_width = draw.textbbox((0, 0), label)[2]
            draw.text((x-label_width-8 if x + label_width + 8 > x0 + width else x+16, y-12),
                      label, fill=(255,255,255,255))
        if sat:
            x, y = self._xy(lat0, lon0, width, height, x0, y0)
            # Satellite silhouette with a central bus and two solar panels.
            draw.polygon([(x-5,y-6),(x+5,y-6),(x+7,y+6),(x-7,y+6)], fill=(255,235,40,255), outline=(0,0,0,255))
            draw.rectangle((x-17,y-4,x-9,y+4), fill=(48,196,255,255), outline=(255,235,40,255))
            draw.rectangle((x+9,y-4,x+17,y+4), fill=(48,196,255,255), outline=(255,235,40,255))
            draw.line((x-9,y,x+9,y), fill=(255,235,40,255), width=2)
            label = info['tle'].get('name', 'Satellite')[:24]
            label_width = draw.textbbox((0, 0), label)[2]
            draw.text((x-label_width-8 if x + label_width + 8 > x0 + width else x+17,y-13),
                      label, fill=(255,255,255,255))
        cfg = self.app.settings
        if cfg['home_lat'] is not None and cfg['home_lon'] is not None:
            x, y = self._xy(cfg['home_lat'], cfg['home_lon'], width, height, x0, y0)
            draw.ellipse((x-5,y-5,x+5,y+5), fill=(25,255,125,255), outline=(255,255,255,255))
            draw.text((x-39 if x > x0 + width - 45 else x+8,y-10), 'HOME', fill=(255,255,255,255))
        view_x = (cw - width) / 2 + self.map_pan_x
        view_y = (ch - height) / 2 + self.map_pan_y
        view_y = max(ch - height, min(0, view_y)) if height >= ch else max(0, min(ch - height, view_y))
        self.map_pan_y = view_y - (ch - height) / 2
        self.map_pan_x = (self.map_pan_x + width / 2) % width - width / 2
        view_x = (cw - width) / 2 + self.map_pan_x
        viewport = Image.new('RGB', (cw, ch), '#061611')
        first = math.floor(-view_x / width) - 1
        last = math.ceil((cw - view_x) / width) + 1
        for tile in range(first, last + 1):
            viewport.paste(world, (round(view_x + tile * width), round(view_y)))
        self.map_image = ImageTk.PhotoImage(viewport)
        self.map_canvas.delete('all')
        self.map_canvas.create_image(0, 0, image=self.map_image, anchor='nw')
        self.map_canvas.create_text(9, 8, text='DRAG TO PAN  ·  WHEEL TO ZOOM  ·  DOUBLE CLICK TO RESET',
                                    anchor='nw', fill=WHITE, font=('Segoe UI', 8, 'bold'))

    def _draw_decoder(self):
        image = self.mmsstv.image.copy() if self.mmsstv and self.mmsstv.image else None
        cw, ch = self.image_canvas.winfo_width(), self.image_canvas.winfo_height()
        self.image_canvas.delete('all')
        if image is None or cw < 10 or ch < 10:
            self.image_canvas.create_text(cw//2, ch//2, text='WAITING FOR SSTV SIGNAL', fill=GREEN,
                                          font=('Segoe UI', 14, 'bold'))
            return
        image.thumbnail((cw-10, ch-10), Image.Resampling.LANCZOS)
        self.decoder_image = ImageTk.PhotoImage(image)
        self.image_canvas.create_image(cw//2, ch//2, image=self.decoder_image)

    def close(self):
        try:
            self.app.tracker_stopped = True
            for after_id in (self.tick_after_id, self.restore_after_id):
                if after_id is not None:
                    self.root.after_cancel(after_id)
            self.save_settings()
            self._save_session()
            self.app.receiver.monitor_sink = None
            if self.audio_output_stream:
                self.audio_output_stream.close()
            if self.mmsstv_output_stream:
                self.mmsstv_output_stream.close()
            if self.mmsstv:
                self.mmsstv.close()
            self.app.receiver.stop()
        finally:
            self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    Desktop(root)
    root.mainloop()
