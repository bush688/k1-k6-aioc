import array
import contextlib
import math
import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import desktop_audio as desktop


class FakeRadio:
    def __init__(self):
        self._ptt_lease = None
        self.events = []
        self.audio = bytearray()
        self.received = threading.Event()

    def ptt(self, action, lease=''):
        if action == 'start':
            if self._ptt_lease:
                raise RuntimeError('PTT 正由另一端使用')
            self._ptt_lease = 'desktop'
        elif lease != self._ptt_lease:
            raise ValueError('PTT 已释放')
        self.events.append(action)
        if action == 'stop':
            self._ptt_lease = None
        return self._ptt_lease

    def receive_tx_audio(self, lease, pcm):
        if lease != self._ptt_lease:
            raise ValueError('错误的租约')
        self.audio.extend(pcm)
        self.received.set()


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = False
        self.started = threading.Event()
        self.closed = False

    def start(self):
        self.active = True
        self.started.set()

    def abort(self):
        self.active = False

    def close(self):
        self.closed = True

    def capture(self, sample=12000):
        frames = self.kwargs['blocksize']
        data = array.array('h', [sample] * (frames * self.kwargs['channels'])).tobytes()
        self.kwargs['callback'](data, frames, None, None)


DEVICE = {'name': 'Headset', 'hostapi': 0, 'default_samplerate': 48000,
          'max_input_channels': 2, 'max_output_channels': 2}


class DesktopAudioChecks(unittest.TestCase):
    def test_headphone_com_lifetime_covers_open_start_and_close_on_same_thread(self):
        events = []
        @contextlib.contextmanager
        def com():
            events.append(('enter', threading.get_ident()))
            try:
                yield
            finally:
                events.append(('exit', threading.get_ident()))
        class Stream(FakeStream):
            def __init__(self, **kwargs):
                events.append(('open', threading.get_ident()))
                super().__init__(**kwargs)
            def start(self):
                events.append(('start', threading.get_ident()))
                super().start()
            def close(self):
                events.append(('close', threading.get_ident()))
                super().close()
        source = SimpleNamespace(sample_rate=48000, lock=threading.Lock(), listeners=set(), health=lambda: 'capturing')
        monitor = desktop.HeadphoneMonitor(source, FakeRadio())
        with patch.object(desktop, 'windows_audio_thread', com), \
             patch.object(desktop.sd, 'query_devices', return_value=DEVICE), \
             patch.object(desktop.sd, 'query_hostapis', return_value={'name': 'Windows WASAPI'}), \
             patch.object(desktop.sd, 'RawOutputStream', side_effect=Stream):
            monitor.start(7)
            monitor.stop()
        self.assertEqual([e[0] for e in events], ['enter', 'open', 'start', 'close', 'exit'])
        self.assertEqual(len({e[1] for e in events}), 1)
        self.assertNotEqual(events[0][1], threading.get_ident())

    def test_resampling_duration_pitch_and_packet_boundaries(self):
        for source, target in ((44100, 16000), (48000, 16000), (44100, 48000), (16000, 48000)):
            with self.subTest(source=source, target=target):
                pcm = array.array('h', (int(10000 * math.sin(2 * math.pi * 1000 * n / source))
                                       for n in range(source))).tobytes()
                converter = desktop.MonoResampler(source, target)
                whole = converter.convert(pcm)
                split = desktop.MonoResampler(source, target)
                packets = b''.join(split.convert(pcm[i:i + 514]) for i in range(0, len(pcm), 514))
                self.assertEqual(packets, whole)
                values = array.array('h'); values.frombytes(whole)
                self.assertLessEqual(abs(len(values) - target), 3)
                crossings = sum(a <= 0 < b for a, b in zip(values, values[1:]))
                self.assertLessEqual(abs(crossings - 1000), 1)

    def test_stereo_downmix_has_correct_clock(self):
        converter = desktop.MonoResampler(48000, 16000, 2)
        pcm = array.array('h', [10000, -2000] * 480).tobytes()
        values = array.array('h'); values.frombytes(converter.convert(pcm))
        self.assertEqual(len(values), 160)
        self.assertTrue(all(v == 4000 for v in values))

    def test_headphones_mute_during_ptt_and_cleanup_on_rx_change(self):
        source = SimpleNamespace(sample_rate=48000, lock=threading.Lock(), listeners=set(), health=lambda: 'capturing')
        radio = FakeRadio()
        monitor = desktop.HeadphoneMonitor(source, radio)
        with patch.object(desktop.sd, 'query_devices', return_value=DEVICE), \
             patch.object(desktop.sd, 'query_hostapis', return_value={'name': 'Windows WASAPI'}), \
             patch.object(desktop.sd, 'RawOutputStream', side_effect=FakeStream):
            monitor.start(7, 50)
            stream = monitor.stream
            self.assertIn(monitor.packets, source.listeners)
            monitor.packets.put(array.array('h', [10000] * 960).tobytes())
            out = bytearray(960 * 4)
            monitor._render(out, 960, None, None)
            values = array.array('h'); values.frombytes(out)
            self.assertTrue(all(v == 5000 for v in values))
            radio._ptt_lease = 'phone'
            monitor.packets.put(array.array('h', [10000] * 960).tobytes())
            monitor._render(out, 960, None, None)
            self.assertEqual(out, bytes(len(out)))
            self.assertEqual(len(monitor.pending), 0)
            source.sample_rate = 44100
            with self.assertRaises(desktop.sd.CallbackAbort):
                monitor._render(out, 960, None, None)
            monitor.stop()
            self.assertFalse(source.listeners)
            self.assertTrue(stream.closed)

    def test_rejects_aioc_as_local_headset(self):
        with patch.object(desktop.sd, 'query_devices', return_value={**DEVICE, 'name': 'AIOC Audio'}), \
             patch.object(desktop.sd, 'query_hostapis', return_value={'name': 'MME'}):
            with self.assertRaisesRegex(ValueError, '电脑耳机'):
                desktop.selected_device(1, 'output')

    def run_mic(self, radio, exercise):
        mic = desktop.DesktopMicrophone(radio)
        created = threading.Event()
        streams = []
        def open_stream(**kwargs):
            stream = FakeStream(**kwargs)
            streams.append(stream)
            created.set()
            return stream
        with patch.object(desktop.sd, 'query_devices', return_value=DEVICE), \
             patch.object(desktop.sd, 'query_hostapis', return_value={'name': 'Windows WASAPI'}), \
             patch.object(desktop.sd, 'RawInputStream', side_effect=open_stream):
            mic.start(8, 300)
            self.assertTrue(created.wait(1))
            self.assertTrue(streams[0].started.wait(1))
            try:
                exercise(mic, streams[0])
            finally:
                mic.stop()
                mic.thread.join(2)
            self.assertFalse(mic.busy)
            self.assertTrue(streams[0].closed)
        return mic

    def test_microphone_sends_resampled_clipped_audio_and_releases(self):
        radio = FakeRadio()
        def exercise(mic, stream):
            self.assertNotIn('start', radio.events)  # Open the microphone before keying.
            stream.capture()
            self.assertTrue(radio.received.wait(1))
            values = array.array('h'); values.frombytes(radio.audio)
            self.assertEqual(len(values), 320)
            self.assertTrue(all(v == 32767 for v in values))
            mic.stop()
            self.assertIsNone(radio._ptt_lease)
        self.run_mic(radio, exercise)
        self.assertEqual(radio.events[-1], 'stop')

    def test_busy_phone_ptt_is_not_released_by_microphone_failure(self):
        radio = FakeRadio(); radio._ptt_lease = 'phone'
        def exercise(mic, stream):
            stream.capture()
            mic.thread.join(1)
            self.assertIn('另一端', mic.error)
        self.run_mic(radio, exercise)
        self.assertEqual(radio._ptt_lease, 'phone')
        self.assertNotIn('stop', radio.events)

    def test_microphone_stall_releases_ptt(self):
        radio = FakeRadio()
        def exercise(mic, stream):
            stream.capture()
            self.assertTrue(radio.received.wait(1))
            mic.thread.join(1.5)
            self.assertIn('无数据', mic.error)
            self.assertIsNone(radio._ptt_lease)
        self.run_mic(radio, exercise)

    def test_microphone_renews_while_audio_is_continuous(self):
        radio = FakeRadio()
        def exercise(mic, stream):
            for _ in range(8):
                stream.capture()
                threading.Event().wait(.05)
            self.assertGreaterEqual(radio.events.count('renew'), 2)
        self.run_mic(radio, exercise)

    def test_cancel_before_first_audio_never_keys_radio(self):
        radio = FakeRadio()
        def exercise(mic, stream):
            mic.stop()
            stream.capture()
        self.run_mic(radio, exercise)
        self.assertNotIn('start', radio.events)


if __name__ == '__main__':
    unittest.main()
