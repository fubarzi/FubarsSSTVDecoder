"""VIS-aware FM SSTV decoder for PD modes and Robot 36."""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
import numpy as np
from PIL import Image


MODES = {
    'PD50': (93, 320, 256, 20, 2.08, 91.52, 388.16),
    'PD90': (99, 320, 256, 20, 2.08, 170.24, 703.04),
    'PD120': (95, 640, 496, 20, 2.08, 121.6, 508.48),
    'PD160': (98, 512, 400, 20, 2.08, 195.584, 804.416),
    'PD180': (96, 640, 496, 20, 2.08, 183.04, 754.24),
    'PD240': (97, 640, 496, 20, 2.08, 244.48, 1000),
    'PD290': (94, 800, 616, 20, 2.08, 228.8, 937.28),
    'Robot36': (8, 320, 240, 9, 3, 88, 300),
}
VIS = {spec[0]: name for name, spec in MODES.items()}


class StreamingToneTracker:
    """Estimate SSTV audio frequency from a continuous analytic signal."""

    smoothing_samples = 17
    delay_samples = 31 + (smoothing_samples - 1) // 2
    detail_samples = 7
    detail_delay_samples = 31 + (detail_samples - 1) // 2

    def __init__(self, rate=24000):
        self.rate = rate
        offsets = np.arange(63) - 31
        kernel = np.zeros(63, dtype=np.float32)
        odd = (offsets != 0) & (offsets % 2 != 0)
        kernel[odd] = 2 / (np.pi * offsets[odd])
        self.kernel = kernel * np.hamming(63)
        self.reset()

    def reset(self):
        self.history = np.zeros(62, dtype=np.float32)
        self.phase_history = np.zeros(self.smoothing_samples - 1, dtype=np.float32)
        self.previous = 0j
        self.detail_tones = np.empty(0, dtype=np.float32)

    def process(self, audio):
        audio = np.asarray(audio, dtype=np.float32)
        if not len(audio):
            return audio
        joined = np.concatenate((self.history, audio))
        quadrature = np.convolve(joined, self.kernel, mode='valid')
        delayed = joined[31:31 + len(audio)]
        analytic = delayed + 1j * quadrature
        previous = np.empty_like(analytic)
        previous[0], previous[1:] = self.previous, analytic[:-1]
        self.previous = analytic[-1]
        self.history = joined[-62:].copy()
        phase = np.angle(analytic * np.conj(previous)).astype(np.float32)
        joined_phase = np.r_[self.phase_history, phase]
        smooth = np.convolve(joined_phase,
                             np.full(self.smoothing_samples, 1 / self.smoothing_samples,
                                     dtype=np.float32), mode='valid')
        detail = np.convolve(joined_phase, np.full(self.detail_samples, 1 / self.detail_samples,
                                                   dtype=np.float32), mode='valid')[-len(phase):]
        tail = self.smoothing_samples - 1
        self.phase_history = phase[-tail:].copy() if len(phase) >= tail else np.r_[self.phase_history, phase][-tail:]
        tones = smooth * (self.rate / (2 * np.pi))
        self.detail_tones = detail * (self.rate / (2 * np.pi))
        tones[(tones < 900) | (tones > 2600) | (np.abs(analytic) < .015)] = np.nan
        self.detail_tones[(self.detail_tones < 900) | (self.detail_tones > 2600) |
                          (np.abs(analytic) < .015)] = np.nan
        return tones.astype(np.float32)


class SSTVDecoder:
    def __init__(self, on_update=None):
        self.on_update = on_update or (lambda event: None)
        self.manual = 'Auto'
        self.slant = 0
        self.reset()

    def reset(self):
        self.freq = []
        self.cross = []
        self.samples = 0
        self.prev = 0.0
        self.cross_cursor = 1
        self.vis_cursor = 600
        self.sync_cursors = {name: 0 for name in MODES}
        self.input_samples = 0
        self.input_last = 0.0
        self.input_rate = None
        self.mode = None
        self.frame_start = None
        self.next_pair = 0
        self.image = None
        self.last_vis = None
        self.last_vis_sample = 0
        self.complete = False
        self.pending = None
        self.tone_tracker = StreamingToneTracker()
        self.tone_values = np.empty(24000, dtype=np.float32)
        self.detail_values = np.empty(24000, dtype=np.float32)
        self.tone_count = 0
        self.on_update({'type': 'reset'})

    def add_audio(self, samples, rate=24000):
        samples = np.asarray(samples, dtype=np.float32)
        if not len(samples):
            return
        if self.input_rate is None:
            self.input_rate = rate
        elif abs(self.input_rate - rate) > 1:
            self.reset()
            self.input_rate = rate
        base = self.input_samples
        end = base + len(samples) - 1
        resampled = []
        while self.samples * rate / 24000 <= end:
            pos = self.samples * rate / 24000
            index = int(pos)
            fraction = pos - index
            a = self.input_last if index < base else float(samples[index - base])
            b = a if index + 1 > end else float(samples[index + 1 - base])
            value = a * (1 - fraction) + b * fraction
            resampled.append(value)
            if self.prev <= 0 < value:
                self.cross.append(self.samples - self.prev / (value - self.prev))
            self.prev = value
            self.samples += 1
        tones = self.tone_tracker.process(resampled)
        required = self.tone_count + len(tones)
        if required > len(self.tone_values):
            enlarged = np.empty(max(required, len(self.tone_values) * 2), dtype=np.float32)
            enlarged[:self.tone_count] = self.tone_values[:self.tone_count]
            self.tone_values = enlarged
            detail_enlarged = np.empty(len(enlarged), dtype=np.float32)
            detail_enlarged[:self.tone_count] = self.detail_values[:self.tone_count]
            self.detail_values = detail_enlarged
        self.tone_values[self.tone_count:required] = tones
        self.detail_values[self.tone_count:required] = self.tone_tracker.detail_tones
        self.tone_count = required
        self.input_samples += len(samples)
        self.input_last = float(samples[-1])
        self._frequencies()
        self._scan_vis()
        self._render_available()
        # Keep the clock bounded over long unattended listening sessions.
        if self.samples > 24000 * 360 and (self.mode is None or
                                            (self.complete and self.samples - self.last_vis_sample > 24000 * 60)):
            self.reset()

    def _frequencies(self):
        while len(self.cross) > self.cross_cursor + 2:
            target = len(self.freq) * 24
            while self.cross_cursor + 1 < len(self.cross) and self.cross[self.cross_cursor + 1] < target:
                self.cross_cursor += 1
            if self.cross_cursor + 2 >= len(self.cross):
                break
            gap = self.cross[self.cross_cursor + 2] - self.cross[self.cross_cursor - 1]
            self.freq.append(72000 / gap if gap > 0 else math.nan)

    def _average(self, start, length):
        values = [self.freq[i] for i in range(round(start), round(start + length), 3)
                  if 0 <= i < len(self.freq) and math.isfinite(self.freq[i])]
        return sum(values) / len(values) if values else math.nan

    def _tone_window_matches(self, start, length, target, tolerance, fraction):
        delay = self.tone_tracker.delay_samples
        indexes = np.arange(round(start * 24 + delay), round((start + length) * 24 + delay), 72)
        valid = (indexes >= 0) & (indexes < self.tone_count)
        if not np.any(valid):
            return False
        tones = self.tone_values[indexes[valid]]
        matches = np.count_nonzero(np.isfinite(tones) & (np.abs(tones - target) <= tolerance))
        return matches >= math.ceil(len(indexes) * fraction)

    def _tone_average(self, start, length):
        delay = self.tone_tracker.delay_samples
        low = max(0, round(start * 24 + delay))
        high = min(self.tone_count, round((start + length) * 24 + delay))
        values = self.tone_values[low:high:24]
        values = values[np.isfinite(values)]
        return float(np.median(values)) if len(values) >= max(3, length * .4) else math.nan

    def _valid_vis_leader(self, start):
        # A line or image patch can contain a short 1900 Hz tone. Require both
        # 300 ms leaders and their 10 ms break before accepting the VIS bits.
        return (self._tone_window_matches(start - 590, 260, 1900, 170, .55) and
                self._tone_window_matches(start - 308, 6, 1200, 170, .5) and
                self._tone_window_matches(start - 290, 260, 1900, 170, .55))

    def _scan_vis(self):
        end = len(self.freq) - 302
        t = self.vis_cursor
        while t < end:
            if abs(self._tone_average(t - 55, 35) - 1900) > 140 or abs(self._tone_average(t, 26) - 1200) > 120:
                t += 2
                continue
            if not self._valid_vis_leader(t):
                t += 2
                continue
            code, ones, valid = 0, 0, True
            for bit in range(7):
                value = self._tone_average(t + 30 + bit * 30 + 9, 12)
                if abs(value - 1100) < 130:
                    code |= 1 << bit
                    ones += 1
                elif abs(value - 1300) >= 130:
                    valid = False
                    break
            if not valid or code not in VIS:
                t += 2
                continue
            parity = self._tone_average(t + 249, 12)
            stop = self._tone_average(t + 279, 12)
            if abs(parity - (1100 if ones % 2 else 1300)) > 150 or abs(stop - 1200) > 150:
                t += 2
                continue
            self.last_vis = code
            self.last_vis_sample = self.samples
            self.pending = (VIS[code], t + 300)
            self.vis_cursor = t + 300
            self.on_update({'type': 'vis', 'mode': VIS[code], 'code': code})
            return
        self.vis_cursor = max(self.vis_cursor, end)

    def _scan_line_sync(self):
        """Recover a partial image when reception begins after the VIS header."""
        if self.mode is not None or self.pending is not None:
            return
        names = (self.manual,) if self.manual in MODES else MODES
        for name in names:
            spec = MODES[name]
            interval = 150.0 if name == 'Robot36' else spec[6]
            duration = spec[3]
            end = len(self.freq) - math.ceil(2 * interval + duration + 12)
            at = self.sync_cursors[name]
            while at <= end:
                value = self.freq[at]
                before = self.freq[max(0, at - 4)]
                if (math.isfinite(value) and abs(value - 1200) < 100 and
                        (at == 0 or not math.isfinite(before) or abs(before - 1200) > 140)):
                    first = self._sync_near(at, duration, 5)
                    if first is not None and abs(first - at) <= 5:
                        second = self._sync_near(first + interval, duration, 6)
                        third = self._sync_near(first + 2 * interval, duration, 6)
                        if (second is not None and third is not None and
                                abs(second - first - interval) <= 6 and
                                abs(third - second - interval) <= 8 and
                                self._start(name, first, recovered=True)):
                            self.sync_cursors[name] = at + 1
                            return
                at += 1
            self.sync_cursors[name] = max(self.sync_cursors[name], end + 1)

    def _sync_near(self, near, duration, window=20):
        low = max(0, math.floor(near - window))
        high = min(len(self.freq) - duration - 5, math.ceil(near + window))
        best, score = None, math.inf
        for at in range(low, high + 1):
            cost = sum(min(450, abs(self.freq[at + k] - 1200)) if math.isfinite(self.freq[at + k]) else 450
                       for k in range(1, duration - 1, 2))
            after = self.freq[at + duration + 3]
            if math.isfinite(after):
                cost += max(0, 200 - abs(after - 1200))
            if cost < score:
                best, score = at, cost
        return best if best is not None and score < duration * 80 else None

    def _start(self, name, near, recovered=False):
        spec = MODES[name]
        first = self._sync_near(near, spec[3], max(0, min(60, len(self.freq) - near - spec[3] - 5)))
        if first is None:
            return False
        self.mode, self.frame_start, self.next_pair, self.complete = name, first, 0, False
        self.image = np.zeros((spec[2], spec[1], 3), dtype=np.uint8)
        self.on_update({'type': 'start', 'mode': name, 'code': self.last_vis,
                        'recovered': recovered})
        return True

    def force_sync(self, name):
        if name == 'Auto':
            name = 'PD120'
        spec = MODES[name]
        for at in range(max(0, len(self.freq) - 1500), len(self.freq) - spec[3] - 5, 3):
            if abs(self._average(at, spec[3] - 1) - 1200) < 90 and self._start(name, at):
                return True
        return False

    def _channels(self, positions, sharp=False):
        # Pixel timing in PD modes can be below 0.2 ms. The 1 ms VIS/sync
        # estimates smear several pixels, so color uses individual tone cycles.
        sample_positions = np.asarray(positions, dtype=np.float64) * 24
        tones = np.full(len(sample_positions), 1500.0)
        usable = np.zeros(len(sample_positions), dtype=bool)
        if self.tone_count and len(sample_positions):
            # Undo the Hilbert and phase smoothing delay. Sync timing
            # still comes from the undelayed zero crossings.
            delay = (self.tone_tracker.detail_delay_samples if sharp else
                     self.tone_tracker.delay_samples)
            offsets = np.arange(-1, 2) if sharp else np.arange(-3, 4)
            indexes = np.rint(sample_positions + delay).astype(np.int64)[:, None] + offsets
            valid = (indexes >= 0) & (indexes < self.tone_count)
            values = (self.detail_values if sharp else self.tone_values)[
                np.clip(indexes, 0, self.tone_count - 1)]
            valid &= np.isfinite(values)
            ordered = np.sort(np.where(valid, values, np.inf), axis=1)
            counts = valid.sum(axis=1)
            usable = counts >= (2 if sharp else 3)
            tones[usable] = ordered[np.arange(len(tones))[usable], (counts[usable] - 1) // 2]
            if np.all(usable):
                return np.clip(np.rint((tones - 1500) * 255 / 800), 0, 255)
        if len(self.cross) >= 2 and len(sample_positions):
            low = max(0, bisect_left(self.cross, float(np.min(sample_positions))) - 2)
            high = min(len(self.cross), bisect_right(self.cross, float(np.max(sample_positions))) + 2)
            crossings = np.asarray(self.cross[low:high], dtype=np.float64)
            indexes = np.searchsorted(crossings, sample_positions, side='right')
            valid = (indexes > 0) & (indexes < len(crossings))
            if np.any(valid):
                gaps = crossings[indexes[valid]] - crossings[indexes[valid] - 1]
                fallback = np.divide(24000, gaps, out=np.full(len(gaps), 1500.0), where=gaps > 0)
                tones[valid] = np.where(usable[valid], tones[valid], fallback)
        tones[~np.isfinite(tones) | (tones < 900) | (tones > 2600)] = 1500
        return np.clip(np.rint((tones - 1500) * 255 / 800), 0, 255)

    def _sync_end(self, start_ms, duration_ms):
        """Locate the 1200-to-1500 Hz sync/porch edge at audio sample precision."""
        rough = (start_ms + duration_ms) * 24 + self.tone_tracker.detail_delay_samples
        low = max(7, round(rough - 36))
        high = min(self.tone_count - 7, round(rough + 36))
        if low >= high:
            return start_ms + duration_ms
        values = self.detail_values
        candidates = []
        for index in range(low, high):
            before = values[index - 6:index]
            after = values[index + 1:index + 7]
            if (np.count_nonzero(np.isfinite(before)) >= 4 and
                    np.count_nonzero(np.isfinite(after)) >= 4 and
                    1100 < np.nanmedian(before) < 1325 and
                    1375 < np.nanmedian(after) < 1650):
                candidates.append(index)
        if not candidates:
            return start_ms + duration_ms
        edge = min(candidates, key=lambda index: abs(index - rough))
        return (edge - self.tone_tracker.detail_delay_samples) / 24

    @staticmethod
    def _color(y, ry, by):
        yy = 1.164 * (y - 16)
        return np.clip(np.rint(np.stack((yy + 1.596 * (ry - 128),
                                          yy - .813 * (ry - 128) - .392 * (by - 128),
                                          yy + 2.017 * (by - 128)), axis=-1)), 0, 255).astype(np.uint8)

    def _render_available(self):
        if self.pending and len(self.freq) > self.pending[1] + 85:
            name, at = self.pending
            if self._start(name, at) or len(self.freq) > at + 350:
                self.pending = None
        if not self.mode or self.frame_start is None:
            return
        spec = MODES[self.mode]
        changed = False
        while self.next_pair < spec[2] // 2:
            pair = self.next_pair
            base = self.frame_start + pair * spec[6] * (1 + self.slant / 1e6)
            if base + spec[6] + 4 >= len(self.freq):
                break
            if self.mode == 'Robot36':
                self._render_robot(pair, base, spec)
            else:
                self._render_pd(pair, base, spec)
            self.next_pair += 1
            changed = True
        if changed:
            self.on_update({'type': 'progress', 'mode': self.mode, 'lines': self.next_pair * 2, 'total': spec[2]})
        if self.next_pair == spec[2] // 2 and not self.complete:
            self.complete = True
            self.on_update({'type': 'complete', 'mode': self.mode, 'lines': spec[2], 'total': spec[2]})

    def _render_pd(self, pair, expected, spec):
        sync = self._sync_near(expected, 20, 8)
        sync = expected if sync is None else sync
        start = self._sync_end(sync, spec[3]) + spec[4]
        pos = (np.arange(spec[1]) + .5) / spec[1] * spec[5]
        y0, ry, by, y1 = (self._channels(start + pos + index * spec[5], sharp=True)
                          for index in range(4))
        self.image[pair * 2] = self._color(y0, ry, by)
        self.image[pair * 2 + 1] = self._color(y1, ry, by)

    def _render_robot(self, pair, expected, spec):
        even = self._sync_near(expected, 9, 6)
        odd = self._sync_near(expected + 150, 9, 6)
        even, odd = expected if even is None else even, expected + 150 if odd is None else odd
        pos = (np.arange(spec[1]) + .5) / spec[1]
        even_start = self._sync_end(even, 9) + 3
        odd_start = self._sync_end(odd, 9) + 3
        ry = self._channels(even_start + 94 + pos * 44, sharp=True)
        by = self._channels(odd_start + 94 + pos * 44, sharp=True)
        self.image[pair * 2] = self._color(self._channels(even_start + pos * 88, sharp=True), ry, by)
        self.image[pair * 2 + 1] = self._color(self._channels(odd_start + pos * 88, sharp=True), ry, by)

    def pil_image(self):
        return Image.fromarray(self.image.copy()) if self.image is not None else None
