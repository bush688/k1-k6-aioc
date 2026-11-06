"""Local headset monitoring and hold-to-talk, sharing the radio's PTT lease."""
import array
import contextlib
import ctypes
import math
import queue
import sys
import threading
import time

import sounddevice as sd


@contextlib.contextmanager
def windows_audio_thread():
    """WASAPI must open/start/close its COM interfaces on an initialized thread."""
    initialized = False
    if sys.platform == 'win32':
        result = ctypes.windll.ole32.CoInitializeEx(None, 0)
        if result not in (0, 1, -2147417850):  # RPC_E_CHANGED_MODE: already initialized.
            raise RuntimeError(f'Windows 音频线程初始化失败：{result}')
        initialized = result in (0, 1)
    try:
        yield
    finally:
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


class MonoResampler:
    """Continuous PCM16 clock conversion, preserving phase across input packets."""
    def __init__(self, source_rate, target_rate, channels=1):
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        self.channels = channels
        self.position = 0
        self.index = 0
        self.previous = 0

    def convert(self, pcm):
        samples = array.array('h')
        samples.frombytes(pcm)
        result = array.array('h')
        for offset in range(0, len(samples), self.channels):
            value = sum(samples[offset:offset + self.channels]) // self.channels
            end = self.index * self.target_rate
            while self.position <= end:
                fraction = self.position - end + self.target_rate
                sample = value if self.index == 0 else self.previous + (value - self.previous) * fraction // self.target_rate
                result.append(sample)
                self.position += self.source_rate
            self.previous = value
            self.index += 1
        return result.tobytes()


def selected_device(device, direction):
    info = sd.query_devices(device)
    if info['max_' + direction + '_channels'] < 1:
        raise ValueError('请选择有效的耳机输出或话筒输入')
    api = sd.query_hostapis(info['hostapi'])['name']
    if 'AIOC' in info['name'].upper():
        raise ValueError('请选择电脑耳机/话筒，AIOC 设备用于电台音频')
    return info, ({'extra_settings': sd.WasapiSettings(auto_convert=False)} if api == 'Windows WASAPI' else {})


class HeadphoneMonitor:
    def __init__(self, source, radio):
        self.source, self.radio = source, radio
        self.control = threading.RLock()
        self.stream = None
        self.error = ''
        self.packets = queue.Queue(maxsize=8)
        self.pending = bytearray()
        self.gain = 100
        self.cancelled = threading.Event()
        self.thread = None

    def start(self, device, gain=100):
        with self.control:
            self.stop()
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError('耳机驱动仍在关闭，请稍后重试')
            self.cancelled = threading.Event()
            ready = threading.Event()
            self.error = ''
            self.thread = threading.Thread(target=self._worker, args=(device, gain, self.cancelled, ready), daemon=True)
            self.thread.start()
            if not ready.wait(4):
                self.cancelled.set()
                raise RuntimeError('耳机驱动启动超时，请重新选择设备')
            if self.error:
                raise RuntimeError(self.error)

    def _worker(self, device, gain, cancelled, ready):
        try:
            with windows_audio_thread():
                try:
                    self._start(device, gain)
                    ready.set()
                    while not cancelled.wait(.1):
                        if not self.stream.active:
                            self.error = self.error or '耳机设备已停止，请重新开启收听'
                            break
                finally:
                    self._stop()
        except Exception as exc:
            self.error = str(exc)
        finally:
            ready.set()

    def _start(self, device, gain):
        if self.source.health() not in ('starting', 'capturing'):
            raise ValueError('请先启动电台接收音频采集')
        info, options = selected_device(device, 'output')
        self.error = ''
        self.rate = int(info['default_samplerate'])
        self.channels = min(2, int(info['max_output_channels']))
        self.source_rate = self.source.sample_rate
        self.converter = MonoResampler(self.source_rate, self.rate)
        self.gain = max(0, min(100, int(gain)))
        self.packets = queue.Queue(maxsize=8)
        self.pending.clear()
        stream = sd.RawOutputStream(device=device, samplerate=self.rate, channels=self.channels,
                                    dtype='int16', blocksize=0, callback=self._render, **options)
        with self.source.lock:
            self.source.listeners.add(self.packets)
        try:
            stream.start()
            self.stream = stream
        except Exception:
            with self.source.lock:
                self.source.listeners.discard(self.packets)
            stream.close()
            raise

    def _render(self, outdata, frames, timing, status):
        outdata[:] = bytes(frames * self.channels * 2)
        # Do not call PortAudio (including Stream.active) from its audio callback.
        if self.source.sample_rate != self.source_rate:
            self.error = '接收采集已停止或设备已更换，请重新开启耳机收听'
            raise sd.CallbackAbort
        muted = bool(self.radio._ptt_lease)
        while True:
            try:
                packet = self.packets.get_nowait()
            except queue.Empty:
                break
            if not muted:
                self.pending.extend(self.converter.convert(packet))
        if muted:
            self.pending.clear()
            return
        limit = int(self.rate * .24) * 2
        if len(self.pending) > limit:
            del self.pending[:-limit]
        count = min(frames * 2, len(self.pending))
        samples = array.array('h')
        samples.frombytes(self.pending[:count])
        del self.pending[:count]
        output = array.array('h', (sample * self.gain // 100 for sample in samples for _ in range(self.channels)))
        outdata[:len(output) * 2] = output.tobytes()

    def stop(self):
        with self.control:
            self.cancelled.set()
            if self.thread is not None and self.thread is not threading.current_thread():
                self.thread.join(2)

    def _stop(self):
        with self.source.lock:
            self.source.listeners.discard(self.packets)
        if self.stream is not None:
            stream, self.stream = self.stream, None
            try:
                stream.abort()
            finally:
                stream.close()
        self.pending.clear()


class DesktopMicrophone:
    def __init__(self, radio):
        self.radio = radio
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.thread = None
        self.lease = None
        self.level = 0
        self.error = ''
        self.state = '待机'

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, device, gain=100):
        if self.busy:
            return
        self.cancelled = threading.Event()
        self.error = ''
        self.state = '正在启动话筒'
        self.thread = threading.Thread(target=self._run, args=(device, gain, self.cancelled), daemon=True)
        self.thread.start()

    def stop(self):
        self.cancelled.set()
        with self.lock:
            lease, self.lease = self.lease, None
            if lease:
                try:
                    self.radio.ptt('stop', lease)
                except (ValueError, RuntimeError):
                    pass

    def _run(self, device, gain, cancelled):
        try:
            with windows_audio_thread():
                self._capture_loop(device, gain, cancelled)
        except Exception as exc:
            self.error = str(exc)
            self.state = '发射失败：' + self.error

    def _capture_loop(self, device, gain, cancelled):
        stream = None
        lease = None
        packets = queue.Queue(maxsize=12)
        failure = []
        def capture(data, frames, timing, status):
            if status:
                failure.append('话筒音频中断：' + str(status))
                return
            try:
                packets.put_nowait((time.monotonic(), bytes(data)))
            except queue.Full:
                failure.append('话筒处理不及时，已停止发射')
        try:
            info, options = selected_device(device, 'input')
            rate = int(info['default_samplerate'])
            channels = min(2, int(info['max_input_channels']))
            converter = MonoResampler(rate, 16000, channels)
            gain = max(1, min(300, int(gain)))
            stream = sd.RawInputStream(device=device, samplerate=rate, channels=channels,
                                       dtype='int16', blocksize=max(1, rate // 50), callback=capture, **options)
            stream.start()
            opened_at = time.monotonic()
            last_packet = opened_at
            renewed = 0
            while not cancelled.is_set():
                if failure:
                    raise RuntimeError(failure[0])
                if not stream.active:
                    raise RuntimeError('话筒已断开，已停止发射')
                try:
                    captured_at, pcm = packets.get(timeout=.1)
                except queue.Empty:
                    if lease and time.monotonic() - last_packet > .5:
                        raise RuntimeError('话筒超过 0.5 秒无数据，已停止发射')
                    if not renewed:
                        # A driver that never calls back must not leave a stuck start operation.
                        if time.monotonic() - opened_at > 1:
                            raise RuntimeError('话筒未提供音频数据')
                    continue
                last_packet = time.monotonic()
                if time.monotonic() - captured_at > .25:
                    raise RuntimeError('话筒音频延迟过大，已停止发射')
                converted = array.array('h')
                converted.frombytes(converter.convert(pcm))
                if not converted:
                    continue
                converted = array.array('h', (max(-32768, min(32767, s * gain // 100)) for s in converted))
                rms = math.sqrt(sum(s * s for s in converted) / len(converted)) / 32768
                self.level = max(0, min(100, round((20 * math.log10(max(rms, 1e-6)) + 60) * 100 / 60)))
                with self.lock:
                    if cancelled.is_set():
                        break
                    if lease is None:
                        lease = self.radio.ptt('start')
                        self.lease = lease
                        self.state = '电脑话筒发射中'
                    if time.monotonic() - renewed >= .2:
                        self.radio.ptt('renew', lease)
                        renewed = time.monotonic()
                    payload = converted.tobytes()
                    for offset in range(0, len(payload), 3200):
                        self.radio.receive_tx_audio(lease, payload[offset:offset + 3200])
        except Exception as exc:
            if not cancelled.is_set():
                self.error = str(exc)
        finally:
            with self.lock:
                if self.lease == lease and lease:
                    try:
                        self.radio.ptt('stop', lease)
                    except (ValueError, RuntimeError):
                        pass
                    self.lease = None
            if stream is not None:
                try:
                    stream.abort()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            self.level = 0
            self.state = '发射失败：' + self.error if self.error else '待机 · PTT 已释放'
