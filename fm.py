"""Continuous narrowband FM demodulation for SDR IQ samples."""
from __future__ import annotations

import math

import numpy as np


HF_MODES = ('USB', 'LSB', 'AM')


class StreamingFrequencyShifter:
    """Move a selected signal to the demodulator without moving the RF center."""

    def __init__(self, sample_rate):
        self.sample_rate = float(sample_rate)
        self.phase = 0.0

    def reset(self):
        self.phase = 0.0

    def process(self, iq, offset_hz):
        iq = np.asarray(iq, dtype=np.complex64)
        if not len(iq):
            return iq
        if abs(offset_hz) < .01:
            return iq if abs(self.phase) < .01 else (iq * np.exp(1j * self.phase)).astype(np.complex64)
        step = 2 * math.pi * offset_hz / self.sample_rate
        angles = self.phase + step * np.arange(len(iq))
        self.phase = (self.phase + step * len(iq)) % (2 * math.pi)
        return (iq * np.exp(1j * angles)).astype(np.complex64)


def digital_shift_hz(center_mhz, dial_mhz, mode):
    hf_offset = StreamingHFDemodulator.offset_hz if mode in HF_MODES else 0
    return (center_mhz - dial_mhz) * 1e6 - hf_offset


class StreamingFMDemodulator:
    def __init__(self, sample_rate=240000, audio_rate=24000, bandwidth_khz=12.5):
        self.sample_rate = int(sample_rate)
        self.decimation = max(1, round(self.sample_rate / audio_rate))
        self.audio_rate = self.sample_rate / self.decimation
        self.set_bandwidth(bandwidth_khz)
        self.reset()

    def set_bandwidth(self, bandwidth_khz):
        bandwidth_khz = float(bandwidth_khz)
        if getattr(self, 'bandwidth_khz', None) == bandwidth_khz:
            return
        self.bandwidth_khz = bandwidth_khz
        # Windowed-sinc RF filter: a cleaner adjacent-channel rejection than
        # a box average, with its history retained between SDR reads.
        taps = 129
        cutoff = min(self.sample_rate * .42, max(3000.0, bandwidth_khz * 500))
        offsets = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
        kernel = 2 * cutoff / self.sample_rate * np.sinc(2 * cutoff / self.sample_rate * offsets)
        kernel *= np.hamming(taps)
        self.rf_kernel = (kernel / kernel.sum()).astype(np.float32)
        self.rf_history = np.zeros(taps - 1, dtype=np.complex64)

    def reset(self):
        self.rf_history = np.zeros(len(self.rf_kernel) - 1, dtype=np.complex64)
        self.phase_history = np.zeros(3, dtype=np.float32)
        self.audio_tail = np.empty(0, dtype=np.float32)
        self.previous = None
        self.iq_dc = None
        self.audio_dc = None

    def process(self, iq):
        iq = np.asarray(iq, dtype=np.complex64)
        if not len(iq):
            return np.empty(0, dtype=np.float32)
        # Follow the RTL center spur gradually so each block does not impose
        # a new DC estimate on the FM phase.
        mean = complex(iq.mean())
        if self.iq_dc is None:
            self.iq_dc = mean
        else:
            fraction = 1 - math.exp(-len(iq) / (self.sample_rate * .5))
            self.iq_dc += fraction * (mean - self.iq_dc)
        iq = iq - self.iq_dc

        joined = np.concatenate((self.rf_history, iq))
        filtered = np.convolve(joined, self.rf_kernel, mode='valid')
        self.rf_history = joined[-len(self.rf_history):].copy()

        previous = filtered[0] if self.previous is None else self.previous
        delayed = np.empty_like(filtered)
        delayed[0], delayed[1:] = previous, filtered[:-1]
        self.previous = filtered[-1]
        phase = np.angle(filtered * np.conj(delayed)).astype(np.float32)
        joined_phase = np.concatenate((self.phase_history, phase))
        smoothed = np.convolve(joined_phase, np.full(4, .25, dtype=np.float32), mode='valid')
        self.phase_history = joined_phase[-3:].copy()

        combined = np.concatenate((self.audio_tail, smoothed))
        count = len(combined) // self.decimation
        self.audio_tail = combined[count * self.decimation:].copy()
        if not count:
            return np.empty(0, dtype=np.float32)
        audio = combined[:count * self.decimation].reshape(count, self.decimation).mean(axis=1)
        average = float(audio.mean())
        if self.audio_dc is None:
            self.audio_dc = average
        else:
            fraction = 1 - math.exp(-count / (self.audio_rate * .15))
            self.audio_dc += fraction * (average - self.audio_dc)
        return np.clip((audio - self.audio_dc) * 8.0, -1, 1).astype(np.float32)


class StreamingWFMDemodulator:
    """Streaming mono broadcast FM with 50 µs de-emphasis."""

    def __init__(self, sample_rate=240000, bandwidth_khz=200):
        self.sample_rate = int(sample_rate)
        self.bandwidth_khz = float(bandwidth_khz)
        self.decimation = max(1, round(self.sample_rate / 50000))
        self.audio_rate = self.sample_rate / self.decimation

        def lowpass(taps, cutoff):
            offsets = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
            kernel = 2 * cutoff / self.sample_rate * np.sinc(2 * cutoff / self.sample_rate * offsets)
            kernel *= np.hamming(taps)
            return (kernel / kernel.sum()).astype(np.float32)

        rf_cutoff = min(self.sample_rate * .44, max(55000, self.bandwidth_khz * 480))
        self.rf_kernel = lowpass(129, rf_cutoff)
        self.audio_kernel = lowpass(257, 15000)
        alpha = math.exp(-1 / (self.audio_rate * 50e-6))
        deemphasis = (1 - alpha) * alpha ** np.arange(64)
        self.deemphasis_kernel = (deemphasis / deemphasis.sum()).astype(np.float32)
        self.reset()

    def reset(self):
        self.rf_history = np.zeros(len(self.rf_kernel) - 1, dtype=np.complex64)
        self.audio_history = np.zeros(len(self.audio_kernel) - 1, dtype=np.float32)
        self.deemphasis_history = np.zeros(len(self.deemphasis_kernel) - 1, dtype=np.float32)
        self.previous = None
        self.sample_count = 0
        self.audio_dc = None

    def process(self, iq):
        iq = np.asarray(iq, dtype=np.complex64)
        if not len(iq):
            return np.empty(0, dtype=np.float32)
        joined = np.concatenate((self.rf_history, iq))
        filtered = np.convolve(joined, self.rf_kernel, mode='valid')
        self.rf_history = joined[-len(self.rf_history):].copy()
        previous = filtered[0] if self.previous is None else self.previous
        delayed = np.empty_like(filtered)
        delayed[0], delayed[1:] = previous, filtered[:-1]
        self.previous = filtered[-1]
        composite = (np.angle(filtered * np.conj(delayed)) *
                     (self.sample_rate / (2 * math.pi * 75000))).astype(np.float32)
        joined_audio = np.concatenate((self.audio_history, composite))
        mono = np.convolve(joined_audio, self.audio_kernel, mode='valid')
        self.audio_history = joined_audio[-len(self.audio_history):].copy()
        first = (-self.sample_count) % self.decimation
        self.sample_count += len(mono)
        reduced = mono[first::self.decimation]
        if not len(reduced):
            return np.empty(0, dtype=np.float32)
        average = float(reduced.mean())
        if self.audio_dc is None:
            self.audio_dc = average
        else:
            fraction = 1 - math.exp(-len(reduced) / (self.audio_rate * .15))
            self.audio_dc += fraction * (average - self.audio_dc)
        joined_deemphasis = np.concatenate((self.deemphasis_history, reduced - self.audio_dc))
        audio = np.convolve(joined_deemphasis, self.deemphasis_kernel, mode='valid')
        self.deemphasis_history = joined_deemphasis[-len(self.deemphasis_history):].copy()
        return np.clip(audio * .85, -1, 1).astype(np.float32)


class StreamingHFDemodulator:
    """USB/LSB/AM audio from IQ tuned 12 kHz above the dial frequency."""

    offset_hz = 12000

    def __init__(self, sample_rate=240000, bandwidth_khz=3.0, mode='USB'):
        if mode not in ('USB', 'LSB', 'AM'):
            raise ValueError('HF demodulation mode must be USB, LSB, or AM')
        self.sample_rate = float(sample_rate)
        self.decimation = max(1, round(self.sample_rate / 24000))
        self.audio_rate = self.sample_rate / self.decimation
        self.mode = mode
        self.bandwidth_khz = float(bandwidth_khz)
        upper = max(2500.0, min(6000.0, self.bandwidth_khz * 1000))
        rf_cutoff = min(self.sample_rate * .4, upper + 3000)
        taps = 257
        offsets = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
        kernel = 2 * rf_cutoff / self.sample_rate * np.sinc(2 * rf_cutoff / self.sample_rate * offsets)
        kernel *= np.hamming(taps)
        self.rf_kernel = (kernel / kernel.sum()).astype(np.float32)
        if mode != 'AM':
            low = 200.0
            half_width = (upper - low) / 2
            center = (upper + low) / 2 * (1 if mode == 'USB' else -1)
            offsets = np.arange(129, dtype=np.float64) - 64
            lowpass = 2 * half_width / self.audio_rate * np.sinc(2 * half_width / self.audio_rate * offsets)
            lowpass *= np.hamming(129)
            lowpass /= lowpass.sum()
            self.side_kernel = (lowpass * np.exp(2j * math.pi * center * offsets / self.audio_rate)).astype(np.complex64)
        self.reset()

    def reset(self):
        self.rf_history = np.zeros(len(self.rf_kernel) - 1, dtype=np.complex64)
        self.side_history = (np.zeros(len(self.side_kernel) - 1, dtype=np.complex64)
                             if self.mode != 'AM' else None)
        self.mixer_phase = 0.0
        self.sample_count = 0
        self.audio_dc = None
        self.rms = None

    def process(self, iq):
        iq = np.asarray(iq, dtype=np.complex64)
        if not len(iq):
            return np.empty(0, dtype=np.float32)
        step = 2 * math.pi * self.offset_hz / self.sample_rate
        angles = self.mixer_phase + step * np.arange(len(iq))
        mixed = iq * np.exp(1j * angles)
        self.mixer_phase = (self.mixer_phase + step * len(iq)) % (2 * math.pi)
        joined = np.concatenate((self.rf_history, mixed))
        filtered = np.convolve(joined, self.rf_kernel, mode='valid')
        self.rf_history = joined[-len(self.rf_history):].copy()
        first = (-self.sample_count) % self.decimation
        self.sample_count += len(iq)
        reduced = filtered[first::self.decimation]
        if not len(reduced):
            return np.empty(0, dtype=np.float32)
        if self.mode == 'AM':
            audio = np.abs(reduced).astype(np.float32)
            average = float(audio.mean())
            if self.audio_dc is None:
                self.audio_dc = average
            else:
                fraction = 1 - math.exp(-len(audio) / (self.audio_rate * .1))
                self.audio_dc += fraction * (average - self.audio_dc)
            audio -= self.audio_dc
        else:
            joined = np.concatenate((self.side_history, reduced))
            sideband = np.convolve(joined, self.side_kernel, mode='valid')
            self.side_history = joined[-len(self.side_history):].copy()
            audio = sideband.real.astype(np.float32)
        level = float(np.sqrt(np.mean(audio * audio)))
        if self.rms is None:
            self.rms = level
        else:
            fraction = 1 - math.exp(-len(audio) / (self.audio_rate * .5))
            self.rms += fraction * (level - self.rms)
        gain = min(30.0, .3 / max(self.rms, .001))
        return np.clip(audio * gain, -1, 1).astype(np.float32)


class StreamingAudioNoiseFilter:
    """Optional SSTV audio passband with state kept across SDR blocks."""

    def __init__(self, sample_rate):
        rate = float(sample_rate)
        offsets = np.arange(81, dtype=np.float64) - 40
        def lowpass(cutoff):
            return 2 * cutoff / rate * np.sinc(2 * cutoff / rate * offsets)
        kernel = (lowpass(3200) - lowpass(500)) * np.hamming(len(offsets))
        response = abs(np.sum(kernel * np.exp(-2j * math.pi * 1900 * offsets / rate)))
        self.kernel = (kernel / response).astype(np.float32)
        self.reset()

    def reset(self):
        self.history = np.zeros(len(self.kernel) - 1, dtype=np.float32)

    def process(self, audio):
        audio = np.asarray(audio, dtype=np.float32)
        if not len(audio):
            return audio
        joined = np.concatenate((self.history, audio))
        filtered = np.convolve(joined, self.kernel, mode='valid')
        self.history = joined[-len(self.history):].copy()
        return filtered.astype(np.float32)


class StreamingSquelch:
    """IQ-level squelch with hysteresis, a short hold, and click-free audio gating."""

    def __init__(self, iq_rate, audio_rate):
        self.iq_rate = float(iq_rate)
        self.audio_rate = float(audio_rate)
        self.reset()

    def reset(self):
        self.level_dbfs = -120.0
        self.is_open = True
        self.low_seconds = 0.0
        self.gate = 1.0

    def process(self, iq, audio, threshold_dbfs):
        iq = np.asarray(iq, dtype=np.complex64)
        audio = np.asarray(audio, dtype=np.float32)
        if len(iq):
            power = float(np.mean(iq.real * iq.real + iq.imag * iq.imag)) / 2
            measured = 10 * math.log10(max(power, 1e-12))
            self.level_dbfs = .8 * self.level_dbfs + .2 * measured if self.level_dbfs > -120 else measured
        if threshold_dbfs <= -120:
            self.is_open = True
            self.low_seconds = 0.0
        elif self.level_dbfs >= threshold_dbfs:
            self.is_open = True
            self.low_seconds = 0.0
        elif self.level_dbfs < threshold_dbfs - 3:
            self.low_seconds += len(iq) / self.iq_rate
            if self.low_seconds >= .2:
                self.is_open = False
        else:
            self.low_seconds = 0.0
        target = 1.0 if self.is_open else 0.0
        if not len(audio):
            return audio
        if target == self.gate:
            return audio if target else np.zeros_like(audio)
        faded = audio.copy()
        ramp_length = min(len(audio), max(1, round(self.audio_rate * .012)))
        faded[:ramp_length] *= np.linspace(self.gate, target, ramp_length, dtype=np.float32)
        faded[ramp_length:] *= target
        self.gate = target
        return faded


def make_demodulator(mode, sample_rate, bandwidth_khz):
    if mode == 'FM':
        return StreamingFMDemodulator(sample_rate, 24000, bandwidth_khz)
    if mode == 'WFM':
        return StreamingWFMDemodulator(sample_rate, bandwidth_khz)
    return StreamingHFDemodulator(sample_rate, bandwidth_khz, mode)
