from __future__ import annotations

import atexit
import array
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import serial
import sounddevice as sd
from flask import Flask, Response, jsonify, request, send_file
from voice_presets import VoicePresets
from voice_presets_ui import VOICE_HTML, VOICE_JS
from serial.tools import list_ports


SETTINGS_PATH = Path(os.environ.get("K1_REMOTE_SETTINGS") or
                     Path(os.environ.get("APPDATA") or Path.home()) / "K1RemoteReceive" / "settings.json")


def load_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


SETTINGS = load_settings()
SETTINGS_LOCK = threading.RLock()
SETTINGS_REVISION = 0
GENERATED_TOKEN = not SETTINGS.get("token")
SETTINGS["token"] = SETTINGS.get("token") or secrets.token_urlsafe(24)
ACCESS_TOKEN = os.environ.get("K1_REMOTE_TOKEN") or SETTINGS["token"]


def save_settings(changes: dict) -> None:
    global SETTINGS_REVISION
    with SETTINGS_LOCK:
        updated = {**SETTINGS, **changes}
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, SETTINGS_PATH)
        SETTINGS.clear()
        SETTINGS.update(updated)
        SETTINGS_REVISION += 1


if GENERATED_TOKEN:
    save_settings({})


OBFUSCATION = bytes([
    0x16, 0x6C, 0x14, 0xE6, 0x2E, 0x91, 0x0D, 0x40,
    0x21, 0x35, 0xD5, 0x40, 0x13, 0x03, 0xE9, 0x80,
])

CTCSS = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
    171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5,
    203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
]

DCS_CODES = '''023 025 026 031 032 036 043 047 051 053 054 065 071 072 073 074
114 115 116 122 125 131 132 134 143 145 152 155 156 162 165 172 174 205
212 223 225 226 243 244 245 246 251 252 255 261 263 265 266 271 274 306
311 315 325 331 332 343 346 351 356 364 365 371 411 412 413 423 431 432
445 446 452 454 455 462 464 465 466 503 506 516 523 526 532 546 565 606
612 624 627 631 632 654 662 664 703 712 723 731 732 734 743 754'''.split()
CHANNEL_TONES = [{'value': 'off', 'label': '关闭', 'kind': 0, 'index': 0}]
CHANNEL_TONES += [{'value': f'ctcss:{hz:.1f}', 'label': f'CTCSS {hz:.1f} Hz', 'kind': 1, 'index': i}
                  for i, hz in enumerate(CTCSS)]
CHANNEL_TONES += [{'value': f'dcs:{code}{polarity}', 'label': f'DCS D{code}{polarity}', 'kind': kind, 'index': i}
                  for kind, polarity in ((2, 'N'), (3, 'I')) for i, code in enumerate(DCS_CODES)]
CHANNEL_TONE_BY_VALUE = {x['value']: x for x in CHANNEL_TONES}
CHANNEL_TONE_BY_RAW = {(x['kind'], x['index']): x for x in CHANNEL_TONES}


def channel_tone(kind: int, index: int) -> dict:
    if kind == 0:
        return CHANNEL_TONES[0]
    return CHANNEL_TONE_BY_RAW.get((kind, index), {
        'value': f'raw:{kind}:{index}', 'label': f'未知亚音（类型 {kind} / 编号 {index}，保留）'})


def channel_tone_choices() -> list[dict]:
    return [{'value': x['value'], 'label': x['label']} for x in CHANNEL_TONES]


MODES = {"F": "FM", "A": "AM", "U": "USB"}
POWER_LEVELS = {0: "用户", 1: "低1", 2: "低2", 3: "低3", 4: "低4", 5: "低5", 6: "中", 7: "高"}
STEP_LEVELS = [
    ("0", "2.5k"), ("1", "5k"), ("2", "6.25k"), ("3", "10k"),
    ("4", "12.5k"), ("5", "25k"), ("6", "8.33k"), ("7", "0.01k"),
    ("8", "0.05k"), ("9", "0.1k"), ("10", "0.25k"), ("11", "0.5k"),
    ("12", "1k"), ("13", "1.25k"), ("14", "9k"), ("15", "15k"),
    ("16", "20k"), ("17", "30k"), ("18", "50k"), ("19", "100k"),
    ("20", "125k"), ("21", "200k"), ("22", "250k"), ("23", "500k"),
]
COMPANDER_LEVELS = ["关闭", "仅发射", "仅接收", "收发"]

PARAM_POWER = 1
PARAM_BANDWIDTH = 2
PARAM_SQUELCH = 3
PARAM_STEP = 4
PARAM_BCL = 5
PARAM_COMPANDER = 6

# K5Viewer serial key enum from the local v6.3 firmware's keyboard.h
KEY_CODES = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "MENU": 10, "UP": 11, "DOWN": 12, "EXIT": 13, "*": 14, "#": 15,
    "PTT": 16, "F2": 17, "F1": 18,
}


def crc_ccitt(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def frame(command_id: int, payload: bytes = b"") -> bytes:
    body = struct.pack("<HH", command_id, len(payload)) + payload
    body += struct.pack("<H", crc_ccitt(body))
    encrypted = bytes(b ^ OBFUSCATION[i % len(OBFUSCATION)] for i, b in enumerate(body))
    return b"\xAB\xCD" + struct.pack("<H", len(body) - 2) + encrypted + b"\xDC\xBA"


def frequency_payload(mhz: str | float | int) -> bytes:
    value = float(mhz)
    if not 18.0 <= value <= 1300.0:
        raise ValueError("频率范围应在 18.000000 到 1300.000000 MHz")
    return struct.pack("<I", int(round(value * 1_000_000)))


def setting_payload(param: int, value: int) -> bytes:
    return struct.pack("<Bi", param, int(value))


def normalize_tcp_host(value: str) -> str:
    host = value.strip()
    if "://" in host:
        parsed = urlsplit(host)
        host = parsed.hostname or host
    host = host.split("/", 1)[0].strip()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    if not host:
        raise ValueError("请输入 TCP 主机名或 IP，不要只填 http/https 地址")
    return host


def is_k1_usb_port(port: str) -> bool:
    # ponytail: The v6.3 USB CDC VID/PID is fixed here; custom USB IDs need an explicit setting later.
    return any(p.device.casefold() == port.casefold() and p.vid == 0x36B7 and p.pid == 0xFFFF
               for p in list_ports.comports())


def is_aioc_port(port: str) -> bool:
    # ponytail: recognize the official AIOC USB ID or its advertised name; an unlabelled clone needs a manual mode later.
    return any(p.device.casefold() == port.casefold() and
               ((p.vid == 0x1209 and p.pid == 0x7388) or "AIOC" in (getattr(p, "description", "") or "").upper())
               for p in list_ports.comports())


@dataclass
class RadioState:
    connection_type: str = "tcp"
    port: str | None = None
    baud: int = 38400
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 9527
    connected: bool = False
    rx_mhz: float = 439.500000
    tx_mhz: float = 439.500000
    rx_ctcss: str = "off"
    tx_ctcss: str = "off"
    mode: str = "F"
    monitor: bool = False
    power: int = 7
    bandwidth: int = 0
    squelch: int = 1
    radio_step: int = 4
    bcl: int = 0
    compander: int = 0
    lcd: list[str] = field(default_factory=lambda: [""] * 8)
    lcd_age_ms: int | None = None
    viewer_flags: int = 0
    last_error: str | None = None
    logs: list[dict] = field(default_factory=list)


class TxAudioBuffer:
    """16 kHz input, with explicit conversion to the output clock."""
    def __init__(self, sample_rate: int = 16000) -> None:
        if sample_rate not in (16000, 48000):
            raise ValueError("Unsupported TX sample rate")
        self.sample_rate = sample_rate
        self.previous_sample = 0
        self.lock = threading.Lock()
        self.data = bytearray()
        self.primed = False
        self.underruns = 0
        self.dropped_bytes = 0

    def push(self, pcm: bytes) -> None:
        with self.lock:
            if self.sample_rate == 48000:
                source = array.array("h")
                source.frombytes(pcm)
                converted = array.array("h")
                previous = self.previous_sample
                for sample in source:
                    delta = sample - previous
                    converted.extend((previous + delta // 3,
                                      previous + 2 * delta // 3, sample))
                    previous = sample
                self.previous_sample = previous
                pcm = converted.tobytes()
            self.data.extend(pcm)
            excess = len(self.data) - int(self.sample_rate * 2 * .24)
            if excess > 0:
                del self.data[:excess]
                self.dropped_bytes += excess

    def render(self, outdata, frames, timing, status) -> None:
        needed = frames * 2
        outdata[:] = bytes(needed)
        with self.lock:
            if not self.primed:
                # Native output needs a margin beyond one 100 ms HTTP packet.
                target = int(self.sample_rate * 2 * (.12 if self.sample_rate == 48000 else .1))
                if len(self.data) < target:
                    return
                self.primed = True
            count = min(needed, len(self.data))
            outdata[:count] = self.data[:count]
            del self.data[:count]
            if count < needed:
                self.underruns += 1
                self.primed = False

    def stats(self) -> dict:
        with self.lock:
            return {"tx_output_rate": self.sample_rate,
                    "tx_buffer_ms": round(len(self.data) * 500 / self.sample_rate),
                    "tx_underruns": self.underruns,
                    "tx_dropped_bytes": self.dropped_bytes}


class RadioLink:
    def __init__(self) -> None:
        self._write_lock = threading.Lock()
        self._ptt_lock = threading.Lock()
        self._ptt_lease: str | None = None
        self._ptt_deadline = 0.0
        self._tx_audio_level = 0
        self._tx_audio_at = 0.0
        self._tx_audio_bytes = 0
        self._channel_lock = threading.Lock()
        self._reply_waiter: queue.Queue[tuple[int, bytes]] | None = None
        self._timestamp = 0
        self._ser: serial.Serial | None = None
        self._aioc = False
        self.tx_output_device: int | None = None
        self.tx_gain = 20
        self._tx_stream = None
        self._tx_buffer = TxAudioBuffer()
        self._sock: socket.socket | None = None
        self._reader_stop = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._buf = bytearray()
        self._pages = [bytearray(128) for _ in range(8)]
        self._last_lcd = 0.0
        self._last_lcd_at = 0.0
        self._connected_at = 0.0
        self.state = RadioState()

    def log(self, level: str, text: str) -> None:
        self.state.logs.append({"time": time.strftime("%H:%M:%S"), "level": level, "text": text})
        self.state.logs = self.state.logs[-300:]

    def connect(self, port: str, baud: int = 38400, ptt_mode: str = "auto") -> None:
        self.close()
        if ptt_mode not in ("auto", "aioc", "firmware"):
            raise ValueError("PTT 方式无效")
        # AIOC may use modem lines for PTT; K1 USB-C CDC needs DTR for LCD replies.
        ser = serial.Serial(port=None, baudrate=baud, timeout=0.05, write_timeout=1)
        ser.dtr = is_k1_usb_port(port) and ptt_mode != "aioc"
        ser.rts = False
        ser.port = port
        ser.open()
        self._ser = ser
        self._aioc = ptt_mode == "aioc" or (ptt_mode == "auto" and is_aioc_port(port))
        self.state.connection_type = "serial"
        self.state.port = port
        self.state.baud = baud
        self._mark_connected()
        self.log("ok", f"已连接 {port} @ {baud}")

    def connect_tcp(self, host: str, port: int) -> None:
        self.close()
        sock = socket.create_connection((host, port), timeout=3)
        sock.settimeout(0.05)
        self._sock = sock
        self.state.connection_type = "tcp"
        self.state.tcp_host = host
        self.state.tcp_port = port
        self._mark_connected()
        self.log("ok", f"已连接 TCP {host}:{port}")

    def _mark_connected(self) -> None:
        self._buf.clear()
        self._timestamp = 0
        self._pages = [bytearray(128) for _ in range(8)]
        self.state.lcd = [""] * 8
        self.state.lcd_age_ms = None
        self._last_lcd = 0.0
        self._last_lcd_at = 0.0
        self._connected_at = time.monotonic()
        self.state.connected = True
        self.state.last_error = None
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(target=self._viewer_loop, daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        with self._ptt_lock:
            self._release_ptt()
        self._reader_stop.set()
        self._reply_waiter = None
        thread = self._reader_thread
        self._reader_thread = None
        if thread and thread.is_alive():
            thread.join(timeout=0.3)
        if self._ser is not None:
            try:
                self._ser.dtr = False
                self._ser.rts = False
                self._ser.close()
            finally:
                self._ser = None
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self.state.connected = False
        self._aioc = False
        self.state.last_error = None
        self._last_lcd = 0.0
        self._last_lcd_at = 0.0
        self.state.lcd_age_ms = None

    def health(self) -> str:
        if not self.state.connected:
            return "error" if self.state.last_error else "disconnected"
        if self._reader_thread is None or not self._reader_thread.is_alive():
            return "error"
        # ponytail: static LCD frames cannot prove liveness; serial errors detect hard loss, while a silent-open link needs a future firmware ACK.
        return "online" if self._last_lcd_at else "waiting"

    def ptt_link_ready(self) -> bool:
        health = self.health()
        if self._aioc:
            return health in ("waiting", "online") and self._ser is not None and self._ser.is_open
        return health == "online"

    def send_raw(self, data: bytes, label: str = "", log_tx: bool = True) -> None:
        if not self.state.connected:
            raise RuntimeError("电台未连接")
        with self._write_lock:
            if self._aioc and self._ptt_lease:
                raise RuntimeError("AIOC 发射期间暂停串口控制，请松开 PTT 后操作")
            if self._ser is not None and self._ser.is_open:
                self._ser.write(data)
                self._ser.flush()
            elif self._sock is not None:
                self._sock.sendall(data)
            else:
                raise RuntimeError("串口/TCP 未连接")
        if log_tx:
            self.log("tx", f"{label or 'RAW'} -> {data.hex(' ')}")

    def send(self, command_id: int, payload: bytes = b"", label: str = "") -> None:
        packet = frame(command_id, payload)
        self.send_raw(packet, label or hex(command_id), True)

    def send_viewer_key(self, key: str, long_press: bool = False) -> None:
        if key == "PTT":
            raise RuntimeError("PTT 必须使用按住/松开接口")
        code = KEY_CODES[key]
        packet_type = 0x04 if long_press else 0x03
        self.send_raw(bytes([0xAA, 0x55, packet_type, code]), f"按键 {key}")

    def _release_ptt(self, reason: str = "连接关闭或清理") -> None:
        active = self._ptt_lease is not None
        if active:
            self.log("ptt", f"释放 PTT：{reason} · 已收 {self._tx_audio_bytes} 字节 · {self._tx_buffer.stats()}")
        self._ptt_lease = None
        self._ptt_deadline = 0.0
        self._tx_audio_level = 0
        self._tx_audio_at = 0.0
        self._tx_audio_bytes = 0
        if active and self._aioc and self._ser is not None:
            try:
                self._ser.dtr = False
            except Exception as exc:
                self.log("err", f"AIOC PTT 释放失败: {exc}")
        elif active and self.state.connected:
            try:
                self.send_raw(b"\xaa\x55\x04\x10", "PTT 释放", False)
            except Exception as exc:
                self.log("err", f"PTT 释放命令失败: {exc}")
        # De-key before waiting for the audio driver, and discard pending speech.
        if self._tx_stream is not None:
            stream, self._tx_stream = self._tx_stream, None
            try:
                stream.abort()
            except Exception as exc:
                self.log("err", f"发射音频停止失败: {exc}")
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    self.log("err", f"发射音频关闭失败: {exc}")
        self._tx_buffer = TxAudioBuffer()

    def ptt(self, action: str, lease: str = "") -> str | None:
        with self._ptt_lock:
            now = time.monotonic()
            if action == "stop":
                if not lease or lease != self._ptt_lease:
                    raise ValueError("PTT 租约无效")
                self._release_ptt("客户端松手")
                return None
            if action not in ("start", "renew"):
                raise ValueError("PTT 操作无效")
            if not self.ptt_link_ready():
                raise RuntimeError("尚未收到电台画面或连接已断开，PTT 已锁定")
            if action == "start":
                if self._ptt_lease and now < self._ptt_deadline:
                    raise RuntimeError("PTT 正由另一端使用")
                if self._ptt_lease:
                    self._release_ptt()
                self._ptt_lease = secrets.token_urlsafe(16)
                self._tx_audio_level = 0
                self._tx_audio_at = 0.0
                self._tx_audio_bytes = 0
                if self._aioc:
                    if self.tx_output_device is None:
                        self._ptt_lease = None
                        raise RuntimeError("请先选择 AIOC 发射音频输出")
                    stream = None
                    try:
                        self._tx_buffer = TxAudioBuffer(48000)
                        output_info = sd.query_devices(self.tx_output_device)
                        output_api = sd.query_hostapis(output_info["hostapi"])["name"]
                        output_options = ({"extra_settings": sd.WasapiSettings(auto_convert=False)}
                                          if output_api == "Windows WASAPI" else {})
                        stream = sd.RawOutputStream(device=self.tx_output_device, samplerate=48000,
                                                    channels=1, dtype="int16", blocksize=960,
                                                    latency="low", callback=self._tx_buffer.render,
                                                    **output_options)
                        stream.start()
                        self._tx_stream = stream
                    except Exception as exc:
                        if stream is not None:
                            try:
                                stream.close()
                            except Exception:
                                pass
                        self._ptt_lease = None
                        raise RuntimeError(f"AIOC 发射音频输出无法启动: {exc}") from exc
            elif not lease or lease != self._ptt_lease or now >= self._ptt_deadline:
                raise ValueError("PTT 已释放或租约无效")
            try:
                if self._aioc and action == "start":
                    self._ser.rts = False
                    self._ser.dtr = True
                elif not self._aioc:
                    self.send_raw(b"\xaa\x55\x03\x10", "PTT 保持", False)
            except Exception:
                self._release_ptt()
                raise
            self._ptt_deadline = time.monotonic() + 0.65
            if action == "start":
                self.log("ptt", "PTT 已启动 · " + ("AIOC DTR" if self._aioc else "固件串口命令"))
            return self._ptt_lease

    def receive_tx_audio(self, lease: str, pcm: bytes) -> None:
        if not pcm or len(pcm) > 4096 or len(pcm) % 2:
            raise ValueError("音频块长度无效")
        with self._ptt_lock:
            if not lease or lease != self._ptt_lease or time.monotonic() >= self._ptt_deadline:
                raise ValueError("PTT 已释放或租约无效")
            samples = array.array("h")
            samples.frombytes(pcm)
            rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
            self._tx_audio_level = max(0, min(100, round((20 * math.log10(max(rms, 1e-6)) + 60) * 100 / 60)))
            self._tx_audio_at = time.monotonic()
            self._tx_audio_bytes += len(pcm)
            if self._aioc:
                if self._tx_stream is None or not self._tx_stream.active:
                    self._release_ptt()
                    raise ValueError("AIOC 发射音频输出未启动")
                self._tx_buffer.push(array.array("h", (int(sample * self.tx_gain / 100) for sample in samples)).tobytes())
                # Audio from the same still-valid lease is also a heartbeat.
                # Never revive an expired or explicitly stopped lease.
                self._ptt_deadline = time.monotonic() + 0.65

    def _viewer_loop(self) -> None:
        next_ping = 0.0
        while not self._reader_stop.is_set():
            try:
                with self._ptt_lock:
                    if self._ptt_lease and self._aioc and self._tx_stream is not None and not self._tx_stream.active:
                        self.state.last_error = "AIOC 发射音频输出已停止"
                        self._release_ptt("声卡输出已停止")
                    if self._ptt_lease and (time.monotonic() >= self._ptt_deadline or not self.ptt_link_ready()):
                        self._release_ptt("续约超时" if time.monotonic() >= self._ptt_deadline else "电台连接异常")
                now = time.time()
                with self._ptt_lock:
                    if now >= next_ping and not (self._aioc and self._ptt_lease):
                        self.send_raw(b"\x55\xAA\x00\x00", log_tx=False)
                        next_ping = now + 0.45

                data = b""
                if self._ser is not None and self._ser.is_open:
                    data = self._ser.read(512)
                elif self._sock is not None:
                    data = self._sock.recv(2048)
                    if not data:
                        raise ConnectionError("TCP 连接已关闭")
                else:
                    raise ConnectionError("串口/TCP 已关闭")
                if data:
                    self._parse_viewer(data)
                self._update_lcd_age()
            except (socket.timeout, serial.SerialTimeoutException):
                self._update_lcd_age()
            except Exception as exc:
                with self._ptt_lock:
                    self._release_ptt()
                if self.state.connected and not self._reader_stop.is_set():
                    self.state.last_error = str(exc)
                    self.state.connected = False
                    self.log("err", f"K5Viewer 读取中断: {exc}")
                return

    def _update_lcd_age(self) -> None:
        if self._last_lcd:
            self.state.lcd_age_ms = int((time.time() - self._last_lcd) * 1000)

    def _parse_viewer(self, data: bytes) -> None:
        self._buf.extend(data)
        while True:
            if not self._buf:
                return
            if self._buf[0] & 0xF0 == 0xF0 and len(self._buf) >= 2:
                self.state.viewer_flags = self._buf[0] & 0x0F
                del self._buf[0]
            if self._buf.startswith(b"\xAB\xCD"):
                if len(self._buf) < 4:
                    return
                size = int.from_bytes(self._buf[2:4], "little")
                if not 4 <= size <= 144:
                    del self._buf[0]
                    continue
                total = size + 8
                if len(self._buf) < total:
                    return
                packet = bytes(self._buf[:total])
                del self._buf[:total]
                pad = bytes(OBFUSCATION[(size + i) % 16] ^ 0xff for i in range(2))
                if packet[4 + size:] != pad + b"\xDC\xBA":
                    continue
                body = bytes(byte ^ OBFUSCATION[i % 16] for i, byte in enumerate(packet[4:4 + size]))
                command, length = struct.unpack_from("<HH", body)
                if length != len(body) - 4:
                    continue
                if self._reply_waiter is not None:
                    self._reply_waiter.put((command, body[4:]))
                continue
            idx = self._buf.find(b"\xAA\x55")
            reply_idx = self._buf.find(b"\xAB\xCD")
            if reply_idx >= 0 and (idx < 0 or reply_idx < idx):
                idx = reply_idx
            if idx < 0:
                del self._buf[:-1]
                return
            if idx:
                del self._buf[:idx]
            if len(self._buf) < 5:
                return
            packet_type = self._buf[2]
            size = (self._buf[3] << 8) | self._buf[4]
            if size > 4096:
                del self._buf[0]
                continue
            total = 5 + size + 1
            if len(self._buf) < total:
                return
            payload = bytes(self._buf[5:5 + size])
            end = self._buf[5 + size]
            del self._buf[:total]
            if end != 0x0A:
                continue
            if packet_type == 0x02:
                self._apply_lcd_delta(payload)

    def _exchange(self, command: int, payload: bytes, expected: int) -> bytes:
        if not self.state.connected or self._reader_thread is None or not self._reader_thread.is_alive():
            raise RuntimeError("电台未连接或读取线程已停止")
        waiter: queue.Queue[tuple[int, bytes]] = queue.Queue()
        self._reply_waiter = waiter
        try:
            self.send(command, payload, f"信道协议 {command:04X}")
            end = time.monotonic() + 2.5
            while time.monotonic() < end:
                try:
                    reply_id, data = waiter.get(timeout=max(0.01, end - time.monotonic()))
                except queue.Empty:
                    break
                if reply_id == expected:
                    return data
            raise TimeoutError(f"电台未回复 {expected:04X}；请检查串口和固件")
        finally:
            self._reply_waiter = None

    def _ensure_session(self) -> None:
        if self._timestamp:
            return
        timestamp = secrets.randbits(32) or 1
        reply = self._exchange(0x0514, struct.pack("<I", timestamp), 0x0515)
        if not reply:
            raise RuntimeError("电台会话应答无效")
        self._timestamp = timestamp

    def read_eeprom(self, offset: int, size: int) -> bytes:
        self._ensure_session()
        reply = self._exchange(0x051B, struct.pack("<HBBI", offset, size, 0, self._timestamp), 0x051C)
        if len(reply) != size + 4 or reply[:4] != struct.pack("<HBB", offset, size, 0):
            raise RuntimeError("电台返回的信道地址或长度不匹配")
        return reply[4:]

    def write_eeprom(self, offset: int, data: bytes) -> None:
        self._ensure_session()
        if offset % 8 or len(data) not in (8, 16):
            raise ValueError("信道写入地址和长度必须按 8 字节对齐")
        reply = self._exchange(0x051D, struct.pack("<HBBI", offset, len(data), 0, self._timestamp) + data, 0x051E)
        if reply[:2] != struct.pack("<H", offset):
            raise RuntimeError("电台写入应答地址不匹配")
        if self.read_eeprom(offset, len(data)) != data:
            raise RuntimeError(f"写入 {offset:04X} 后回读不一致；可能已锁定或写入失败")

    def _apply_lcd_delta(self, payload: bytes) -> None:
        for offset in range(0, len(payload) - 8, 9):
            chunk_idx = payload[offset]
            if chunk_idx >= 128:
                continue
            page = chunk_idx // 16
            chunk_in_line = chunk_idx % 16
            bit = chunk_in_line // 2
            base = (chunk_in_line % 2) * 64
            chunk = payload[offset + 1:offset + 9]
            for j, byte in enumerate(chunk):
                for k in range(8):
                    x = base + j * 8 + k
                    if byte & (1 << k):
                        self._pages[page][x] |= 1 << bit
                    else:
                        self._pages[page][x] &= ~(1 << bit)
        self.state.lcd = [base64.b64encode(page).decode("ascii") for page in self._pages]
        self._last_lcd = time.time()
        self._last_lcd_at = time.monotonic()
        self.state.lcd_age_ms = 0


link = RadioLink()
voice_presets = VoicePresets(SETTINGS_PATH.parent / 'voice-presets', link)
atexit.register(voice_presets.stop)
app = Flask(__name__)
atexit.register(link.close)


def channel_number(value) -> int:
    number = int(value)
    if not 1 <= number <= 1024:
        raise ValueError("信道号须为 1–1024")
    return number


def channel_frequency(value) -> int:
    try:
        units = Decimal(str(value)) * 100000
    except (InvalidOperation, TypeError):
        raise ValueError("请输入有效 MHz 频率") from None
    if units != units.to_integral_value() or not 1800000 <= units <= 130000000:
        raise ValueError("频率须在 18–1300 MHz，精度为 10 Hz")
    return int(units)


def channel_band(units: int) -> int:
    return next((i for i, lower in reversed(list(enumerate(
        (1800000, 10800000, 13700000, 17400000, 35000000, 40000000, 47000000)
    ))) if units >= lower), 0)


def _channel_raw(number: int) -> tuple[bytes, bytes, bytes]:
    index = number - 1
    return (link.read_eeprom(index * 16, 16),
            link.read_eeprom(0x4000 + index * 16, 16),
            link.read_eeprom(0x8000 + (index // 4) * 8, 8))


def _channel_result(number: int, raw: tuple[bytes, bytes, bytes]) -> dict:
    freq, name, attrs = raw
    rx, offset = struct.unpack_from("<II", freq)
    if rx in (0, 0xffffffff):
        raise ValueError("这个信道没有已存储频率；本工具只修改现有信道")
    direction = freq[11] & 0x0f
    if direction not in (0, 1, 2):
        raise ValueError("现有信道的发射偏移方向无法识别")
    tx = rx + (offset if direction == 1 else -offset if direction == 2 else 0)
    try:
        title = name.split(b"\x00", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("现有信道名不是 UTF-8；为避免损坏，请先用原写频工具处理") from None
    return {"number": number, "name": title, "rx_mhz": f"{rx / 100000:.5f}",
            "tx_mhz": f"{tx / 100000:.5f}",
            "rx_tone": channel_tone(freq[10] & 15, freq[8])['value'],
            "rx_tone_label": channel_tone(freq[10] & 15, freq[8])['label'],
            "tx_tone": channel_tone(freq[10] >> 4, freq[9])['value'],
            "tx_tone_label": channel_tone(freq[10] >> 4, freq[9])['label'],
            "mode": ("FM", "AM", "USB")[min(freq[11] >> 4, 2)],
            "bandwidth": "窄带" if freq[12] & 2 else "宽带",
            "power": (freq[12] >> 2) & 7, "step": freq[14],
            "etag": hashlib.sha256(b"".join(raw)).hexdigest()}


def read_channel(number: int) -> dict:
    number = channel_number(number)
    with link._channel_lock:
        return _channel_result(number, _channel_raw(number))


def write_channel(number: int, data: dict) -> dict:
    number = channel_number(number)
    if not isinstance(data, dict):
        raise ValueError("信道数据格式无效")
    name = str(data.get("name", ""))
    if any(not (32 <= ord(c) <= 126 or (len(c.encode("utf-8")) == 3 and c.encode("utf-8")[0] in range(0xe4, 0xf0))) for c in name):
        raise ValueError("信道名只支持可打印 ASCII 和固件支持的三字节汉字")
    name_bytes = name.encode("utf-8")
    if len(name_bytes) > 15:
        raise ValueError("信道名最多 15 个 UTF-8 字节")
    rx = channel_frequency(data.get("rx_mhz"))
    tx = channel_frequency(data.get("tx_mhz"))
    if abs(tx - rx) > 0xffffffff:
        raise ValueError("收发频差过大")
    with link._channel_lock:
        raw = _channel_raw(number)
        current = _channel_result(number, raw)
        if not data.get("etag") or data["etag"] != current["etag"]:
            raise ValueError("信道内容已经变化，请重新读取后再写入")
        index = number - 1
        freq = bytearray(raw[0])
        for field, code_position, shift in (('rx_tone', 8, 0), ('tx_tone', 9, 4)):
            if field not in data:
                continue  # Older clients keep the existing tones unchanged.
            value = data[field]
            if not isinstance(value, str):
                raise ValueError('请选择有效的接收/发射亚音')
            if value == current[field]:
                continue  # Preserve unused indices and unknown codes byte-for-byte.
            tone = CHANNEL_TONE_BY_VALUE.get(value)
            if tone is None:
                raise ValueError('亚音设置无效，请重新读取并选择列表中的亚音')
            freq[code_position] = tone['index']
            freq[10] = (freq[10] & ~(15 << shift)) | (tone['kind'] << shift)
        struct.pack_into("<II", freq, 0, rx, abs(tx - rx))
        freq[11] = (freq[11] & 0xf0) | (0 if tx == rx else 1 if tx > rx else 2)
        title = name_bytes.ljust(16, b"\x00")
        attrs = bytearray(raw[2])
        position = (index % 4) * 2
        attrs[position] = (attrs[position] & 0xf8) | channel_band(rx)
        # ponytail: write only changed blocks; if a later block fails, surface the partial write and require a fresh read.
        for offset, before, after in (
            (index * 16, raw[0], bytes(freq)),
            (0x4000 + index * 16, raw[1], title),
            (0x8000 + (index // 4) * 8, raw[2], bytes(attrs)),
        ):
            if before != after:
                link.write_eeprom(offset, after)
        readback = _channel_raw(number)
        if readback != (bytes(freq), title, bytes(attrs)):
            raise RuntimeError('写入后回读不一致，可能存在部分写入；请重新读取当前信道')
        result = _channel_result(number, readback)
        link.log("ok", f"信道 {number} 已写入并回读验证")
        return result


class ReceiveAudio:
    def __init__(self) -> None:
        self.stream = None
        self.device = None
        self.listeners: set[queue.Queue[bytes]] = set()
        self.lock = threading.Lock()
        self.last_error = None
        self.sample_rate = 0
        self.channels = 0
        self.level = 0
        self._started_at = 0.0
        self._last_capture_at = 0.0

    def devices(self) -> list[dict]:
        apis = sd.query_hostapis()
        return [{"id": i, "name": str(d["name"]), "channels": int(d["max_input_channels"]),
                 "api": str(apis[d["hostapi"]]["name"]), "rate": int(d["default_samplerate"])}
                for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] >= 1]

    def output_devices(self) -> list[dict]:
        apis = sd.query_hostapis()
        return [{"id": i, "name": str(d["name"]), "api": str(apis[d["hostapi"]]["name"]),
                 "rate": int(d["default_samplerate"])}
                for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] >= 1]

    def start(self, device: int) -> None:
        selected = next((d for d in self.devices() if d["id"] == device), None)
        if selected is None:
            raise ValueError("请选择有效的录音设备")
        self.stop()
        self.last_error = None
        self._started_at = time.monotonic()
        self.sample_rate = selected["rate"]
        self.channels = min(2, selected["channels"])
        try:
            stream = sd.RawInputStream(device=device, samplerate=self.sample_rate, channels=self.channels,
                                       dtype="int16", blocksize=0, callback=self._capture)
        except sd.PortAudioError as exc:
            if "-2005401480" in str(exc):
                self.last_error = "DirectSound 设备无驱动响应"
                raise ValueError("DirectSound 设备无驱动响应；请选同名 B2 的 WASAPI 设备（截图中为 69）") from exc
            self.last_error = str(exc)
            raise
        try:
            stream.start()
        except Exception as exc:
            stream.close()
            self.last_error = str(exc)
            raise
        self.stream = stream
        self.device = device

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.device = None
        self.sample_rate = 0
        self.channels = 0
        self.level = 0
        self._started_at = 0.0
        self._last_capture_at = 0.0
        self.last_error = None

    def health(self) -> str:
        if self.stream is None:
            return "error" if self.last_error else "stopped"
        try:
            if not self.stream.active:
                self.last_error = "录音流已停止"
                return "error"
        except Exception as exc:
            self.last_error = str(exc)
            return "error"
        # ponytail: a live input stream calls back continually, including during silence; 3 s without callbacks indicates capture has stalled.
        if time.monotonic() - (self._last_capture_at or self._started_at) > 3:
            self.last_error = "超过 3 秒未收到音频数据"
            return "error"
        return "capturing" if self._last_capture_at else "starting"

    def _capture(self, data, frames, timing, status) -> None:
        self._last_capture_at = time.monotonic()
        self.last_error = str(status) if status else None
        samples = array.array("h")
        samples.frombytes(bytes(data))
        if self.channels == 2:
            samples = array.array("h", ((samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)))
        if samples:
            rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
            # ponytail: map -60..0 dBFS to a visible 0..100 bar; this indicates signal presence, not calibrated loudness.
            self.level = max(0, min(100, round((20 * math.log10(max(rms, 1e-6)) + 60) * 100 / 60)))
        packet = samples.tobytes()
        with self.lock:
            for listener in self.listeners:
                if listener.full():
                    try:
                        listener.get_nowait()
                    except queue.Empty:
                        pass
                listener.put_nowait(packet)

    def subscribe(self):
        listener: queue.Queue[bytes] = queue.Queue(maxsize=8)
        with self.lock:
            self.listeners.add(listener)
        try:
            while self.stream is not None:
                try:
                    yield listener.get(timeout=1)
                except queue.Empty:
                    continue
        finally:
            with self.lock:
                self.listeners.discard(listener)


audio = ReceiveAudio()
atexit.register(audio.stop)


def selected_audio_id() -> int | None:
    saved = SETTINGS.get("audio_device")
    if not isinstance(saved, dict):
        return None
    matches = [d for d in audio.devices() if d["name"] == saved.get("name") and d["api"] == saved.get("api")]
    if len(matches) == 1:
        return matches[0]["id"]
    return next((d["id"] for d in matches if d["id"] == saved.get("id")), None)


def selected_tx_output_id() -> int | None:
    saved = SETTINGS.get("tx_output_device")
    if not isinstance(saved, dict):
        return None
    matches = [d for d in audio.output_devices() if d["name"] == saved.get("name") and d["api"] == saved.get("api")]
    if len(matches) == 1:
        return matches[0]["id"]
    return next((d["id"] for d in matches if d["id"] == saved.get("id")), None)


def settings_json() -> dict:
    with SETTINGS_LOCK:
        saved = SETTINGS.copy()
        revision = SETTINGS_REVISION
    try:
        audio_id = selected_audio_id()
        tx_output_id = selected_tx_output_id()
    except Exception:
        audio_id = None
        tx_output_id = None
    return {"mode": saved.get("mode", "serial"), "port": saved.get("port", ""),
            "ptt_mode": saved.get("ptt_mode", "auto"),
            "baud": saved.get("baud", 38400), "tcp_host": saved.get("tcp_host", "127.0.0.1"),
            "tcp_port": saved.get("tcp_port", 9527), "radio_enabled": saved.get("radio_enabled", False),
            "audio_device": audio_id, "audio_device_name": (saved.get("audio_device") or {}).get("name", ""),
            "tx_output_device": tx_output_id, "tx_output_name": (saved.get("tx_output_device") or {}).get("name", ""),
            "tx_gain": saved.get("tx_gain", 20),
            "audio_enabled": saved.get("audio_enabled", False), "revision": revision}


class SettingsChangedError(ValueError):
    pass


def check_revision(expected: int | None) -> None:
    if expected is not None and expected != SETTINGS_REVISION:
        raise SettingsChangedError("设置已在另一端修改，请刷新设置后重试")


def connect_configured_radio(mode: str, port: str = "", baud: int = 38400,
                             host: str = "127.0.0.1", tcp_port: int = 9527,
                             expected_revision: int | None = None, ptt_mode: str = "auto") -> None:
    with SETTINGS_LOCK:
        check_revision(expected_revision)
        if ptt_mode not in ("auto", "aioc", "firmware"):
            raise ValueError("PTT 方式无效")
        if mode == "serial":
            port = port.strip()
            if not port:
                raise ValueError("请选择电台对应的 COM 口")
            if baud != 38400:
                raise ValueError("本项目使用的 6.3 固件 K头 UART 固定为 38400，请改选 38400")
            link.connect(port, baud, ptt_mode)
        elif mode == "tcp":
            if ptt_mode == "aioc":
                raise ValueError("AIOC DTR PTT 需要本机串口，不能通过 TCP 透传")
            host = normalize_tcp_host(host)
            if not 1 <= tcp_port <= 65535:
                raise ValueError("TCP 端口须为 1–65535")
            link.connect_tcp(host, tcp_port)
        else:
            raise ValueError("连接方式无效")
        try:
            save_settings({"mode": mode, "port": port, "baud": baud, "ptt_mode": ptt_mode,
                           "tcp_host": host, "tcp_port": tcp_port, "radio_enabled": True})
        except Exception:
            link.close()
            raise


def disconnect_configured_radio(expected_revision: int | None = None) -> None:
    with SETTINGS_LOCK:
        check_revision(expected_revision)
        link.close()
        save_settings({"radio_enabled": False})


def start_configured_audio(device: int, expected_revision: int | None = None) -> None:
    with SETTINGS_LOCK:
        check_revision(expected_revision)
        selected = next((d for d in audio.devices() if d["id"] == device), None)
        if selected is None:
            raise ValueError("请选择有效的录音设备")
        audio.start(device)
        try:
            save_settings({"audio_device": {key: selected[key] for key in ("id", "name", "api")},
                           "audio_enabled": True})
        except Exception:
            audio.stop()
            raise


def stop_configured_audio(expected_revision: int | None = None) -> None:
    with SETTINGS_LOCK:
        check_revision(expected_revision)
        audio.stop()
        save_settings({"audio_enabled": False})


def set_tx_output(device: int, expected_revision: int | None = None, gain: int = 20) -> None:
    with SETTINGS_LOCK:
        check_revision(expected_revision)
        if not 1 <= gain <= 100:
            raise ValueError("发射输出音量须为 1–100%")
        selected = next((d for d in audio.output_devices() if d["id"] == device), None)
        if selected is None:
            raise ValueError("请选择有效的 AIOC 发射音频输出")
        with link._ptt_lock:
            if link._ptt_lease:
                raise ValueError("请先松开发射键，再更改输出设备")
            save_settings({"tx_output_device": {key: selected[key] for key in ("id", "name", "api")}, "tx_gain": gain})
            link.tx_output_device = device
            link.tx_gain = gain


def restore_devices() -> None:
    saved = settings_json()
    link.tx_output_device = selected_tx_output_id()
    link.tx_gain = int(saved["tx_gain"])
    if saved["radio_enabled"]:
        try:
            connect_configured_radio(saved["mode"], saved["port"], int(saved["baud"]),
                                     saved["tcp_host"], int(saved["tcp_port"]), ptt_mode=saved["ptt_mode"])
        except Exception as exc:
            link.log("err", f"恢复电台连接失败：{exc}")
    if saved["audio_enabled"]:
        try:
            device = selected_audio_id()
            if device is None:
                raise RuntimeError("保存的录音设备未找到；请在设置中重新选择")
            start_configured_audio(device)
        except Exception as exc:
            link.log("err", f"恢复音频采集失败：{exc}")


@app.before_request
def require_access():
    if request.args.get("token") == ACCESS_TOKEN or request.cookies.get("k1_access") == ACCESS_TOKEN:
        return None
    return jsonify({"error": "访问口令无效；请使用服务端显示的完整地址"}), 403


@app.after_request
def remember_access(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    if request.args.get("token") == ACCESS_TOKEN:
        response.set_cookie("k1_access", ACCESS_TOKEN, httponly=True, samesite="Lax")
    return response


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>K1 远程收听与画面</title>
<style>
:root{color-scheme:dark;--bg:#151918;--panel:#202724;--panel2:#27312d;--line:#40504a;--screen:#c4d0b1;--screenText:#11190f;--text:#edf4ee;--muted:#a9bbb2;--accent:#55d18e;--bad:#d46969}
*{box-sizing:border-box}body{margin:0;height:100vh;overflow:hidden;background:var(--bg);color:var(--text);font:15px/1.4 "Microsoft YaHei","Segoe UI",sans-serif}
button,input,select{min-height:44px;border-radius:6px;border:1px solid var(--line);background:#101513;color:var(--text);padding:0 10px;font:inherit}
button{cursor:pointer;background:var(--panel2);font-weight:700}button.primary,.stepgrid button.active{background:var(--accent);color:#07120d;border-color:var(--accent)}button.warn{background:#4c3b23;color:#ffe0a2}button.danger{background:#452a2d;color:#ffd9d9}label{display:block;margin:8px 0 5px;color:var(--muted)}
.app{height:100vh;max-width:980px;margin:0 auto;display:flex;flex-direction:column}.topbar{height:52px;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-bottom:1px solid var(--line)}.brand{font-size:18px;font-weight:800}.status{color:var(--muted);white-space:nowrap}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--bad);margin-right:6px}.dot.on{background:var(--accent)}
.screenWrap{flex:0 0 auto;padding:12px;background:#111514}.screen{background:#060707;border:10px solid #060707;border-radius:7px;box-shadow:0 10px 28px rgba(0,0,0,.35)}#lcdCanvas{width:100%;image-rendering:pixelated;display:block;background:var(--screen);border-radius:2px}
.controls{flex:1 1 auto;overflow:auto;padding:12px}.group{border-top:1px solid var(--line);padding-top:12px;margin-top:10px}.group:first-child{border-top:0;margin-top:0;padding-top:0}h2{margin:0 0 8px;font-size:17px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:9px}.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.keypad{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.keypad button{min-height:48px}.activefreq{border-color:var(--accent);box-shadow:0 0 0 2px rgba(85,209,142,.22);background:#122b20}
.knobArea{display:grid;grid-template-columns:190px 1fr;gap:12px;align-items:center}.knob{width:180px;aspect-ratio:1;border-radius:50%;margin:4px auto;position:relative;touch-action:none;user-select:none;background:radial-gradient(circle at 50% 50%,#607068 0 38%,#26302d 39% 55%,#56645f 56% 67%,#1a211f 68%);box-shadow:inset 0 0 0 4px #0a0e0c,0 8px 22px rgba(0,0,0,.35)}.knob::after{content:"";position:absolute;width:9px;height:72px;border-radius:999px;background:#b9c8c1;left:50%;top:25px;transform:translateX(-50%) rotate(-62deg);transform-origin:50% 65px}.hint{color:var(--muted);font-size:13px;text-align:center}.log{min-height:112px;max-height:190px;overflow:auto;background:#0d1110;border:1px solid var(--line);border-radius:6px;padding:8px;font:12px/1.45 Consolas,monospace}.ok{color:#8fe0bd}.tx{color:#8dc7ff}.err{color:#ff9b9b}
.modalBack{position:fixed;inset:0;display:none;place-items:center;background:rgba(0,0,0,.55);padding:16px}.modalBack.show{display:grid}.modal{width:min(520px,100%);max-height:90vh;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;box-shadow:0 18px 60px rgba(0,0,0,.5)}
.audioBar{padding:10px 12px;border-bottom:1px solid var(--line);background:var(--panel)}.audioBar select{width:100%;margin:4px 0 8px}.audioBar .grid2{align-items:center}.audioBar button{width:100%}
@media(max-width:620px){.screen{min-height:190px}.knobArea{grid-template-columns:1fr}.keypad{grid-template-columns:repeat(4,1fr)}}
/* UV Studio-style placement: screen and keypad form the main working surface. */
:root{color-scheme:light;--bg:#f6f7f9;--panel:#fff;--panel2:#e9edf3;--line:#c9d0da;--screen:#35492c;--screenText:#e7f0dd;--text:#273447;--muted:#637489;--accent:#2f69ed;--bad:#d34b4b}
body{height:auto;min-height:100vh;overflow:auto}.app{height:auto;max-width:1440px;padding:16px 24px 30px;gap:14px}.topbar{border:0;border-radius:12px;background:#fff;box-shadow:0 2px 12px #26354a18}.topbar button{min-width:92px}.stage{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,.8fr);gap:22px;align-items:start}.screenWrap{padding:10px;border-radius:14px;background:#e5e8eb;box-shadow:0 8px 20px #23313e24}.screen{border:0;box-shadow:none;background:var(--screen)}#lcdCanvas{background:var(--screen);aspect-ratio:2/1}.keyPanel{padding:14px;border:1px solid #bcc6d1;border-radius:18px;background:linear-gradient(145deg,#e8ecf1,#cbd4df);box-shadow:0 10px 24px #22334727}.keyPanel h2{font-size:13px;color:#566479}.keypad{grid-template-columns:repeat(5,1fr)}.keypad button{min-height:58px;background:linear-gradient(#f9fafc,#dbe2ea);border-color:#b8c3d0;box-shadow:0 2px 4px #24384a20;color:#526176}.keypad button:disabled{opacity:.48}.pttRow{display:flex;justify-content:center}.pttRow button{width:112px;height:112px;min-height:112px;border:4px solid #eb5757;border-radius:50%;color:#d54343;background:white;font-size:20px}.pttRow button:disabled{opacity:.58}.channelPanel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 16px;box-shadow:0 3px 12px #23313e12}.channelPanel h2{margin:0 0 8px}.channelScroll{overflow-x:auto}.channelLine{display:flex;gap:8px;align-items:center;min-width:910px}.channelLine label{margin:0;white-space:nowrap}.channelLine input{min-width:0}.channelLine #channelNumber{width:88px}.channelLine #channelName{width:160px}.channelLine #channelRx,.channelLine #channelTx{width:125px}.channelLine button{white-space:nowrap}.channelLine #writeChannel:disabled{opacity:.45;cursor:not-allowed}.channelTones{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:10px}.channelTones label{margin:0}.channelTones select{width:210px}.channelInfo{margin-top:6px;text-align:left}.audioBar{border:1px solid var(--line);border-radius:12px}.audioBar select{width:min(500px,100%)}.audioBar .grid2{display:inline-flex;gap:8px;margin-right:8px}.audioBar button{width:auto}.controls{flex:none;overflow:visible;border:1px solid var(--line);border-radius:12px;background:white}.controls summary{cursor:pointer;font-weight:700;padding:10px}.controlsContent{padding:0 12px 12px}.modalBack{z-index:10}button,input,select{background:#fff;color:var(--text)}button{background:var(--panel2)}button.primary{color:#fff}.hint{color:var(--muted)}.log{background:#f5f7fa}.knob{opacity:.9}
@media(max-width:740px){.app{padding:8px;gap:9px}.brand{font-size:15px}.topbar{padding:6px;gap:5px}.topbar button{min-width:66px;padding:0 6px}.status{font-size:12px;overflow:hidden;text-overflow:ellipsis}.stage{grid-template-columns:1fr;gap:10px}.screenWrap{max-width:100%;padding:7px}.keyPanel{width:min(100%,460px);margin:auto;padding:10px}.keypad{grid-template-columns:repeat(5,1fr);gap:5px}.keypad button{min-height:48px;padding:0 3px;font-size:13px}.pttRow{justify-content:flex-end;padding-right:14px}.pttRow button{width:78px;height:78px;min-height:78px;font-size:14px}.channelPanel{padding:10px}.audioBar .grid2{display:flex;margin:6px 0}.audioBar button{flex:1}.screen{min-height:0}}
.levelBox{margin-top:10px}.levelHead{display:flex;justify-content:space-between;gap:10px;font-weight:700}.levelTrack{height:22px;margin-top:6px;border-radius:12px;background:#dce2e8;overflow:hidden}.levelFill{height:100%;width:0;background:#43b877;transition:width .15s}.levelFill.loud{background:#e7a93b}
.pttControl{touch-action:none;user-select:none}.pttControl.pressed{background:#e64b4b;color:white;border-color:#b72f2f}
.txMeters{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:5px 8px;font-size:12px}.txMeters .levelTrack{height:12px;margin-top:2px}.txMeters .levelHead{font-weight:500}
.topSettings summary{cursor:pointer;padding:5px 12px;color:var(--muted);font-size:13px}.screenAndPtt{min-width:0}.screenAndPtt .pttRow{margin-top:10px}
@media(max-width:740px){.topSettings summary{padding:2px 8px}.stage{display:contents}.screenAndPtt{position:sticky;top:0;z-index:3;background:var(--bg);padding-bottom:5px;box-shadow:0 8px 15px #26354a18}.screenAndPtt .pttRow{justify-content:center;margin-top:3px}.screenAndPtt .pttRow button{width:72px;height:72px;min-height:72px}.keyPanel{width:100%}}
.keypad[hidden],#keypadK6[hidden]{display:none}.keypad.k6-nav{grid-template-columns:repeat(4,1fr)}.keypad.k6-digits,.keypad.k6-side{grid-template-columns:repeat(3,1fr);margin-top:8px}
</style>
</head>
<body>
<div class="app">
  <details class="topSettings"><summary>连接状态与设置 ▾</summary><div class="topbar"><div class="brand">K1 远程收听</div><div class="status"><span id="dot" class="dot"></span><span id="status">未连接</span></div><button id="tapToListen" class="primary" onclick="listen()" hidden>点击收听</button><button onclick="openConnect()">设置</button></div></details>
  <div class="stage"><div class="screenAndPtt"><div class="screenWrap"><div class="screen"><canvas id="lcdCanvas" width="128" height="64"></canvas></div><div class="hint" id="lcdAge">K5Viewer 实时屏幕等待中</div></div>
  <div class="pttRow"><button class="pttControl" disabled title="按住发射，松开停发">PTT<br><small id="pttStatus">等待电台画面</small></button></div><div class="txMeters"><div><div class="levelHead"><span>手机麦克风</span><span id="micLevel">未启动</span></div><div id="micTrack" class="levelTrack" role="meter" aria-label="手机麦克风发射语音" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="micFill" class="levelFill"></div></div></div><div><div class="levelHead"><span>服务端收到</span><span id="txAudioLevel">未收到</span></div><div id="txAudioTrack" class="levelTrack" role="meter" aria-label="服务端收到发射语音" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="txAudioFill" class="levelFill"></div></div></div></div></div>
    <div class="keyPanel"><h2><label for="keypadModel">遥控键盘</label> <select id="keypadModel"><option value="k1">UV-K1</option><option value="k6">UV-K6 v1</option></select></h2><div class="keypad" id="keypadK1"><button class="pttControl" disabled>PTT</button><button onclick="sendKey('MENU')">MENU</button><button onclick="sendKey('UP')">◀</button><button onclick="sendKey('DOWN')">▶</button><button onclick="sendKey('EXIT')">EXIT</button><button onclick="sendKey('F1')">F1</button><button onclick="sendKey('1')">1</button><button onclick="sendKey('2')">2</button><button onclick="sendKey('3')">3</button><button onclick="sendKey('*')">*</button><button onclick="sendKey('F2')">F2</button><button onclick="sendKey('4')">4</button><button onclick="sendKey('5')">5</button><button onclick="sendKey('6')">6</button><button onclick="sendKey('0')">0</button><button class="pttControl" disabled>PTT</button><button onclick="sendKey('7')">7</button><button onclick="sendKey('8')">8</button><button onclick="sendKey('9')">9</button><button onclick="sendKey('#')">#</button></div><div id="keypadK6" hidden><div class="keypad k6-nav"><button onclick="sendKey('MENU')">M</button><button onclick="sendKey('UP')">▲</button><button onclick="sendKey('DOWN')">▼</button><button onclick="sendKey('EXIT')">EXIT</button></div><div class="keypad k6-digits"><button onclick="sendKey('1')">1</button><button onclick="sendKey('2')">2</button><button onclick="sendKey('3')">3</button><button onclick="sendKey('4')">4</button><button onclick="sendKey('5')">5</button><button onclick="sendKey('6')">6</button><button onclick="sendKey('7')">7</button><button onclick="sendKey('8')">8</button><button onclick="sendKey('9')">9</button><button onclick="sendKey('*')">*</button><button onclick="sendKey('0')">0</button><button onclick="sendKey('#')">F/#</button></div><div class="keypad k6-side"><button class="pttControl" disabled>PTT</button><button onclick="sendKey('F1')">侧键 1</button><button onclick="sendKey('F2')">侧键 2</button></div><div class="hint">远程按键需刷入支持按键的 K6 固件</div></div></div></div>
  <div class="channelPanel"><h2>信道写频</h2><div class="channelScroll"><div class="channelLine"><label for="channelNumber">信道号</label><input id="channelNumber" type="number" min="1" max="1024" value="1" onchange="loadChannel()"><button onclick="loadChannel()">读取</button><label for="channelName">名称</label><input id="channelName" maxlength="15" disabled><label for="channelRx">接收 MHz</label><input id="channelRx" inputmode="decimal" disabled><label for="channelTx">发射 MHz</label><input id="channelTx" inputmode="decimal" disabled><button id="writeChannel" class="primary" onclick="saveChannel()" disabled>写入并核验</button></div></div><div class="channelTones"><label for="channelRxTone">接收亚音</label><select id="channelRxTone" disabled>__CHANNEL_TONES__</select><label for="channelTxTone">发射亚音</label><select id="channelTxTone" disabled>__CHANNEL_TONES__</select></div><div class="hint channelInfo" id="channelInfo">选定信道号后读取电台中已有的信道。</div></div>
  <div class="audioBar"><div class="grid2"><button class="primary" onclick="listen()">手机 / 电脑收听</button><button onclick="stopListening()">停止收听</button></div><div class="hint" id="audioStatus">等待服务端采集</div><div class="hint" id="playbackStatus">准备收听</div><div class="levelBox"><div class="levelHead"><span>采集输入音量</span><span id="audioLevel">未采集</span></div><div id="levelTrack" class="levelTrack" role="meter" aria-label="采集输入音量" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="levelFill" class="levelFill"></div></div></div></div>
  <details class="controls"><summary>频率、监听、参数与日志</summary><div class="controlsContent">
    <div class="group">
      <h2>频率与波轮</h2>
      <div class="grid2"><div><label>接收/下行 MHz</label><input id="rxInput" inputmode="decimal" value="439.500000"></div><div><label>发射/上行 MHz</label><input id="txInput" inputmode="decimal" value="439.500000"></div></div>
      <div class="grid2"><button onclick="sendParam('rx')">设置接收</button><button onclick="sendParam('tx')">设置发射</button></div>
      <div class="knobArea"><div><div id="knob" class="knob"></div><div class="hint" id="knobTarget">当前：接收频率</div></div><div><label>波轮步进</label><div class="grid4 stepgrid" id="steps"><button data-step="0.0005">0.5k</button><button data-step="0.001">1k</button><button data-step="0.005" class="active">5k</button><button data-step="0.0125">12.5k</button></div></div></div>
    </div>
    <div class="group">
      <h2>通联常用参数</h2>
      <div class="grid2"><div><label>接收 CTCSS 亚音</label><select id="rxTone"><option value="off">关闭</option>__TONES__</select></div><div><label>发射 CTCSS 亚音</label><select id="txTone"><option value="off">关闭</option>__TONES__</select></div></div>
      <div class="grid2"><button onclick="sendParam('rx_ctcss')">设置接收亚音</button><button onclick="sendParam('tx_ctcss')">设置发射亚音</button></div>
      <div class="grid2"><div><label>调制模式</label><select id="mode"><option value="F">FM</option><option value="A">AM</option><option value="U">USB</option></select></div><div><label>发射功率</label><select id="power"><option value="7">高</option><option value="6">中</option><option value="5">低5</option><option value="4">低4</option><option value="3">低3</option><option value="2">低2</option><option value="1">低1</option><option value="0">用户</option></select></div></div>
      <div class="grid2"><button onclick="sendParam('mode')">设置模式</button><button onclick="sendParam('power')">设置功率</button></div>
      <div class="grid2"><div><label>静噪 SQL</label><select id="squelch"><option>0</option><option selected>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option><option>7</option><option>8</option><option>9</option></select></div><div><label>电台菜单步进</label><select id="radioStep">__STEPS__</select></div></div>
      <div class="grid2"><button onclick="sendParam('squelch')">设置静噪</button><button onclick="sendParam('radio_step')">设置步进</button></div>
      <div class="grid2"><button class="warn" onclick="sendParam('monitor_on')">监听开</button><button onclick="sendParam('monitor_off')">监听关</button></div>
    </div>
    <div class="group">
      <h2>高级参数</h2>
      <div class="grid2"><div><label>宽/窄带</label><select id="bandwidth"><option value="0">宽带 WIDE</option><option value="1">窄带 NARROW</option></select></div><div><label>忙道锁定 BCL</label><select id="bcl"><option value="0">关闭</option><option value="1">开启</option></select></div></div>
      <div class="grid2"><button onclick="sendParam('bandwidth')">设置宽窄带</button><button onclick="sendParam('bcl')">设置 BCL</button></div>
      <div><label>压扩 Compander</label><select id="compander"><option value="0">关闭</option><option value="1">仅发射</option><option value="2">仅接收</option><option value="3">收发</option></select></div><button onclick="sendParam('compander')">设置压扩</button>
    </div>
    <div class="group"><h2>日志</h2><div class="log" id="log"></div></div>
  </div></details>
</div>
<div class="modalBack" id="connectModal"><div class="modal"><h2>服务器设置</h2><div class="hint" id="settingsHint">电脑窗口与远程网页共用设置，成功连接后自动保存。</div><label>笔记本到电台</label><select id="modeLink" onchange="toggleLinkMode()"><option value="serial">电台串口（AIOC / K头 / USB-C）</option><option value="tcp">电台串口 TCP 透传</option></select><div id="tcpBox" class="grid2" style="display:none"><div><label>TCP 地址</label><input id="tcpHost" value="127.0.0.1"></div><div><label>TCP 端口</label><input id="tcpPort" inputmode="numeric" value="9527"></div></div><div id="serialBox"><label>串口</label><select id="port"></select><label>波特率</label><select id="baud"><option selected>38400</option></select><label>PTT 方式</label><select id="pttMode"><option value="auto">自动识别</option><option value="aioc">AIOC DTR</option><option value="firmware">固件串口命令</option></select></div><div class="grid2" style="margin-top:12px"><button class="primary" onclick="connect()">连接并保存</button><button class="danger" onclick="disconnect()">断开</button></div><label for="audioDevice">接收音频设备</label><select id="audioDevice"></select><div class="grid2"><button class="primary" onclick="startServerAudio()">采集并保存</button><button onclick="stopServerAudio()">停止采集</button></div><label for="txOutput">AIOC 发射音频输出</label><select id="txOutput"></select><label for="txGain">发射输出音量 %（先从 20% 校准）</label><input id="txGain" type="number" min="1" max="100" value="20"><button onclick="saveTxOutput()">保存发射输出</button><div class="grid2"><button onclick="loadSettings()">刷新设置</button><button onclick="closeConnect()">关闭</button></div></div></div>
<script>
const $=id=>document.getElementById(id);
async function api(path,opts={}){const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opts));const d=await r.json();if(!r.ok)throw new Error(d.error||r.statusText);return d}
 let pttHeld=false,pttLease=null,pttTimer=null,pttGeneration=0,pttRenewing=false,pttError="";
 function setMeter(fill,track,label,level,status){$(fill).style.width=`${level}%`;$(track).setAttribute('aria-valuenow',level);$(label).textContent=status}
 window.k1MicLevel=level=>{if(pttHeld&&pttLease)setMeter('micFill','micTrack','micLevel',level,`${level}%`)};
 window.k1MicFailed=message=>{$('playbackStatus').textContent='手机麦克风失败：'+message;pttRelease()};
 function pttRelease(){pttGeneration++;pttHeld=false;clearInterval(pttTimer);pttTimer=null;document.querySelectorAll('.pttControl').forEach(b=>b.classList.remove('pressed'));if(typeof K1Audio!=='undefined')K1Audio.stopTx();setMeter('micFill','micTrack','micLevel',0,'未启动');setMeter('txAudioFill','txAudioTrack','txAudioLevel',0,'未收到');const lease=pttLease;pttLease=null;if(lease)api('/api/ptt',{method:'POST',body:JSON.stringify({action:'stop',lease}),keepalive:true}).catch(()=>{});$('pttStatus').textContent='已释放'}
 for(const button of document.querySelectorAll('.pttControl')){button.addEventListener('pointerdown',async e=>{if(pttHeld||button.disabled||voiceRecording)return;e.preventDefault();pttHeld=true;pttError="";const generation=++pttGeneration;button.setPointerCapture(e.pointerId);button.classList.add('pressed');$('pttStatus').textContent='请求中';try{const result=await api('/api/ptt',{method:'POST',body:JSON.stringify({action:'start'})});if(!pttHeld||generation!==pttGeneration){api('/api/ptt',{method:'POST',body:JSON.stringify({action:'stop',lease:result.lease}),keepalive:true}).catch(()=>{});return}pttLease=result.lease;if(typeof K1Audio!=='undefined'){setMeter('micFill','micTrack','micLevel',0,'正在启动');K1Audio.startTx(pttLease)}else setMeter('micFill','micTrack','micLevel',0,'请使用手机 App');pttTimer=setInterval(async()=>{if(!pttHeld||!pttLease||pttRenewing)return;pttRenewing=true;try{await api('/api/ptt',{method:'POST',body:JSON.stringify({action:'renew',lease:pttLease})})}catch(error){$('playbackStatus').textContent='PTT 中断：'+error.message;pttRelease();pttError=error.message;$('pttStatus').textContent=pttError}finally{pttRenewing=false}},200)}catch(error){if(generation===pttGeneration){$('playbackStatus').textContent='PTT 失败：'+error.message;pttRelease();pttError=error.message;$('pttStatus').textContent=pttError}}});for(const event of ['pointerup','pointercancel','lostpointercapture'])button.addEventListener(event,pttRelease)}
 window.addEventListener('blur',pttRelease);window.addEventListener('pagehide',pttRelease);document.addEventListener('visibilitychange',()=>{if(document.hidden)pttRelease()});
let loadedChannel=null,channelRequest=0;
function channelEditable(ok){for(const id of ['channelName','channelRx','channelTx','channelRxTone','channelTxTone'])$(id).disabled=!ok;$('writeChannel').disabled=!ok}
function renderChannelFields(d){$('channelName').value=d.name;$('channelRx').value=d.rx_mhz;$('channelTx').value=d.tx_mhz;for(const [id,key] of [['channelRxTone','rx_tone'],['channelTxTone','tx_tone']]){const select=$(id);for(const option of [...select.options])if(option.dataset.unknown)option.remove();if(![...select.options].some(o=>o.value===d[key])){const option=new Option(d[key+'_label'],d[key]);option.dataset.unknown='1';select.add(option)}select.value=d[key]}}
async function loadChannel(){const n=Number($('channelNumber').value),requestId=++channelRequest;loadedChannel=null;channelEditable(false);$('channelInfo').textContent=`正在读取信道 ${n}…`;if(!Number.isInteger(n)||n<1||n>1024){$('channelInfo').textContent='信道号须为 1–1024';return}try{const d=await api(`/api/channel/${n}`);if(requestId!==channelRequest||Number($('channelNumber').value)!==n)return;loadedChannel=d;renderChannelFields(d);channelEditable(true);$('channelInfo').textContent=`信道 ${n} 已读取 · ${d.mode} / ${d.bandwidth} / 功率 ${d.power} / 步进 ${d.step}；其他参数原样保留` }catch(e){if(requestId===channelRequest)$('channelInfo').textContent=`读取失败：${e.message}`}}
async function saveChannel(){const n=Number($('channelNumber').value);if(!loadedChannel||loadedChannel.number!==n){channelEditable(false);$('channelInfo').textContent='请先读取当前信道';return}channelEditable(false);$('channelInfo').textContent=`正在写入并回读信道 ${n}…`;try{const d=await api(`/api/channel/${n}`,{method:'POST',body:JSON.stringify({etag:loadedChannel.etag,name:$('channelName').value,rx_mhz:$('channelRx').value,tx_mhz:$('channelTx').value,rx_tone:$('channelRxTone').value,tx_tone:$('channelTxTone').value})});if(Number($('channelNumber').value)!==n)return;loadedChannel=d;renderChannelFields(d);$('channelInfo').textContent=`信道 ${n} 已写入并回读一致`;channelEditable(true)}catch(e){loadedChannel=null;$('channelInfo').textContent=`写入失败：${e.message}；请重新读取`;channelEditable(false)}}
$('channelNumber').addEventListener('input',()=>{loadedChannel=null;channelEditable(false);$('channelInfo').textContent='选择信道号后读取现有内容'});
function val(id,value){if(document.activeElement!==$(id))$(id).value=value}
 function render(s){$('dot').className='dot'+(s.radio_health==='online'?' on':'');$('status').textContent=({online:'电台已响应',waiting:'等待电台回应',silent:'电台画面超时',disconnected:'电台未连接',error:'电台连接中断'})[s.radio_health];const ready=s.ptt_ready??(s.radio_health==='online'&&s.lcd_age_ms!==null);document.querySelectorAll('.pttControl').forEach(b=>b.disabled=!ready);if(!ready&&pttHeld)pttRelease();else if(pttHeld)$('pttStatus').textContent=s.tx_led?'发射中':'已按下';else $('pttStatus').textContent=pttError|| (ready?'按住发射':(s.radio_health==='waiting'?'等待画面或 AIOC 发射输出':'PTT 未就绪'));val('rxInput',Number(s.rx_mhz).toFixed(6));val('txInput',Number(s.tx_mhz).toFixed(6));val('rxTone',s.rx_ctcss||'off');val('txTone',s.tx_ctcss||'off');val('mode',s.mode);val('power',String(s.power));val('squelch',s.squelch);val('radioStep',s.radio_step);val('bandwidth',s.bandwidth);val('bcl',s.bcl);val('compander',s.compander);drawLcd(s.lcd);$('lcdAge').textContent=s.lcd_age_ms===null?'K5Viewer 实时屏幕等待中':`距上次画面变化 ${s.lcd_age_ms} ms`;$('audioStatus').textContent=({capturing:`笔记本采集中 · ${s.audio_listeners} 个收听端`,starting:'录音设备启动中',stopped:'接收音频未启动',error:`采集中断 · ${s.audio_error||'录音设备无响应'}`})[s.audio_health];const log=$('log');log.replaceChildren(...s.logs.map(e=>{const d=document.createElement('div');d.className=e.level;d.textContent=`${e.time} [${e.level}] ${e.text}`;return d}));log.scrollTop=log.scrollHeight}
async function loadPorts(){const d=await api('/api/ports');$('port').replaceChildren(...d.ports.map(p=>{const o=document.createElement('option');o.value=p.device;o.textContent=`${p.device} - ${p.description}`;return o}))}
function toggleLinkMode(){const tcp=$('modeLink').value==='tcp';$('tcpBox').style.display=tcp?'grid':'none';$('serialBox').style.display=tcp?'none':'block'}
function openConnect(){$('connectModal').classList.add('show');loadSettings().catch(e=>{$('settingsHint').textContent=e.message})}function closeConnect(){$('connectModal').classList.remove('show')}
const nativeApp=navigator.userAgent.includes('K1RemoteAndroid/1');
let settingsRevision=-1,autoAttempted=false,settingsPending=null;
function loadSettings(){if(settingsPending)return settingsPending;settingsPending=(async()=>{const [p,d,o,s]=await Promise.all([api('/api/ports'),api('/api/audio/devices'),api('/api/audio/outputs'),api('/api/settings')]);$('port').replaceChildren(...p.ports.map(item=>{const opt=document.createElement('option');opt.value=item.device;opt.textContent=`${item.device} - ${item.description}`;return opt}));const choices=(items,label)=>{const blank=document.createElement('option');blank.value='';blank.textContent=label;return [blank,...items.map(item=>{const opt=document.createElement('option');opt.value=item.id;opt.textContent=`${item.id}: ${item.name} · ${item.api} · ${item.rate} Hz`;return opt})]};$('audioDevice').replaceChildren(...choices(d.devices,'请选择接收音频设备'));$('txOutput').replaceChildren(...choices(o.devices,'请选择 AIOC 发射输出'));$('modeLink').value=s.mode;$('pttMode').value=s.ptt_mode;$('port').value=s.port;$('baud').value=38400;$('tcpHost').value=s.tcp_host;$('tcpPort').value=s.tcp_port;$('audioDevice').value=s.audio_device??'';$('txOutput').value=s.tx_output_device??'';$('txGain').value=s.tx_gain;toggleLinkMode();settingsRevision=s.revision;$('settingsHint').textContent=`电脑与网页共用设置 · 电台${s.radio_enabled?'自动连接':'已断开'} · 音频${s.audio_enabled?'自动采集':'已停止'}${s.audio_device_name?' · '+s.audio_device_name:''}`})().finally(()=>{settingsPending=null});return settingsPending}
async function refresh(){const s=await api('/api/state');render(s);const level=s.audio_active?s.audio_level:0;$('levelFill').style.width=`${level}%`;$('levelFill').classList.toggle('loud',level>=80);$('levelTrack').setAttribute('aria-valuenow',level);$('audioLevel').textContent=s.audio_active?`${level}% · ${level>0?'有输入':'暂无输入信号'}`:'未采集';setMeter('txAudioFill','txAudioTrack','txAudioLevel',s.tx_audio_recent?s.tx_audio_level:0,s.tx_audio_recent?`${s.tx_audio_level}% · 已到达`:'未收到');if(s.settings_revision!==settingsRevision)loadSettings().catch(()=>{});if(s.audio_active&&!autoAttempted&&!nativeApp){autoAttempted=true;listen(true)}if(!s.audio_active){if(player)stopListening();autoAttempted=false}}
async function connect(){try{await api('/api/connect',{method:'POST',body:JSON.stringify({mode:modeLink.value,ptt_mode:pttMode.value,port:port.value,baud:Number(baud.value),host:tcpHost.value,tcp_port:Number(tcpPort.value),revision:settingsRevision})});await loadSettings();await refresh();closeConnect()}catch(e){alert(e.message);if(e.message.includes('另一端'))await loadSettings()}}
async function disconnect(){try{await api('/api/disconnect',{method:'POST',body:JSON.stringify({revision:settingsRevision})});await loadSettings();await refresh()}catch(e){alert(e.message);await loadSettings()}}
let wheelStep=.005,activeFreq='rx',knobAngle=-62,lastAngle=null,wheelRemainder=0,knobTimer=null;
function setWheelStep(v){wheelStep=Number(v);document.querySelectorAll('#steps button').forEach(b=>b.classList.toggle('active',Number(b.dataset.step)===wheelStep))}
function selectFreqTarget(kind){activeFreq=kind;$('rxInput').classList.toggle('activefreq',kind==='rx');$('txInput').classList.toggle('activefreq',kind==='tx');$('knobTarget').textContent='当前：'+(kind==='rx'?'接收频率':'发射频率')}
function tuneTicks(ticks){const input=$(activeFreq==='rx'?'rxInput':'txInput');const next=Math.max(18,Math.min(1300,Number(input.value||0)+ticks*wheelStep));input.value=next.toFixed(6);knobAngle=(knobAngle+ticks*10)%360;$('knob').style.transform=`rotate(${knobAngle+62}deg)`;clearTimeout(knobTimer);knobTimer=setTimeout(()=>sendParam(activeFreq),350)}
function pointerAngle(e){const r=$('knob').getBoundingClientRect();return Math.atan2(e.clientY-(r.top+r.height/2),e.clientX-(r.left+r.width/2))*180/Math.PI}
document.addEventListener('focusin',e=>{if(e.target.id==='rxInput')selectFreqTarget('rx');if(e.target.id==='txInput')selectFreqTarget('tx')});document.addEventListener('click',e=>{if(e.target.matches('#steps button'))setWheelStep(e.target.dataset.step)});
$('knob').addEventListener('pointerdown',e=>{lastAngle=pointerAngle(e);wheelRemainder=0;$('knob').setPointerCapture(e.pointerId);e.preventDefault()});$('knob').addEventListener('pointermove',e=>{if(lastAngle===null)return;let delta=pointerAngle(e)-lastAngle;if(delta>180)delta-=360;if(delta<-180)delta+=360;lastAngle+=delta;wheelRemainder+=delta;const ticks=Math.trunc(wheelRemainder/16);if(ticks){wheelRemainder-=ticks*16;tuneTicks(ticks)}e.preventDefault()});$('knob').addEventListener('pointerup',()=>{lastAngle=null});$('knob').addEventListener('pointercancel',()=>{lastAngle=null});
async function sendParam(kind){const body={kind,rx_mhz:rxInput.value,tx_mhz:txInput.value,rx_ctcss:rxTone.value,tx_ctcss:txTone.value,mode:mode.value,power:power.value,bandwidth:bandwidth.value,squelch:squelch.value,radio_step:radioStep.value,bcl:bcl.value,compander:compander.value};try{render(await api('/api/send',{method:'POST',body:JSON.stringify(body)}))}catch(e){alert(e.message);await refresh()}}
const keypadModel = $('keypadModel');
function selectKeypad(model) {
    keypadModel.value = model === 'k6' ? 'k6' : 'k1';
    $('keypadK1').hidden = keypadModel.value !== 'k1';
    $('keypadK6').hidden = keypadModel.value !== 'k6';
}
selectKeypad(localStorage.getItem('k1.keypadModel'));
keypadModel.addEventListener('change', () => {
    selectKeypad(keypadModel.value);
    localStorage.setItem('k1.keypadModel', keypadModel.value);
});
async function sendKey(key){try{render(await api('/api/key',{method:'POST',body:JSON.stringify({key})}))}catch(e){alert(e.message)}}
async function startServerAudio(){if($('audioDevice').value===''){alert('请先选择录音设备');return}try{await api('/api/audio/start',{method:'POST',body:JSON.stringify({device:Number($('audioDevice').value),revision:settingsRevision})});await loadSettings();await refresh()}catch(e){alert(e.message);if(e.message.includes('另一端'))await loadSettings()}}
async function stopServerAudio(){try{await api('/api/audio/stop',{method:'POST',body:JSON.stringify({revision:settingsRevision})});stopListening();await loadSettings();await refresh()}catch(e){alert(e.message);await loadSettings()}}
async function saveTxOutput(){if($('txOutput').value===''){alert('请先选择 AIOC 发射输出');return}try{await api('/api/audio/output',{method:'POST',body:JSON.stringify({device:Number($('txOutput').value),gain:Number($('txGain').value),revision:settingsRevision})});await loadSettings()}catch(e){alert(e.message);await loadSettings()}}
let player=null,audioAbort=null,playAt=0;
async function listen(auto=false){stopListening();autoAttempted=true;$('playbackStatus').textContent='正在连接收听流…';try{player=new AudioContext();if(auto&&player.state!=='running'){stopListening();$('tapToListen').hidden=false;document.querySelector('.topSettings').open=true;$('playbackStatus').textContent='浏览器需要点一次“点击收听”';return}await player.resume();$('tapToListen').hidden=true;const state=await api('/api/state');if(!state.audio_active||!state.audio_rate)throw Error('请先启动服务端采集');const rate=state.audio_rate;audioAbort=new AbortController();const response=await fetch('/api/audio/stream',{signal:audioAbort.signal});if(!response.ok)throw Error((await response.json()).error);const reader=response.body.getReader();let pending=new Uint8Array(0);while(true){const {value,done}=await reader.read();if(done)break;const joined=new Uint8Array(pending.length+value.length);joined.set(pending);joined.set(value,pending.length);const length=joined.length&~1;pending=joined.slice(length);if(!length)continue;const samples=new Float32Array(length/2);const view=new DataView(joined.buffer,joined.byteOffset,length);for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(i*2,true)/32768;const buffer=player.createBuffer(1,samples.length,rate);buffer.copyToChannel(samples,0);const source=player.createBufferSource();source.buffer=buffer;source.connect(player.destination);if(playAt<player.currentTime||playAt>player.currentTime+0.8)playAt=player.currentTime+0.16;source.start(playAt);playAt+=buffer.duration;$('playbackStatus').textContent='正在播放接收音频'}$('playbackStatus').textContent='音频流已结束'}catch(e){stopListening();if(e.name!=='AbortError')$('playbackStatus').textContent=`收听失败：${e.message}`}}
function stopListening(){if(audioAbort){audioAbort.abort();audioAbort=null}if(player){player.close();player=null}playAt=0;$('tapToListen').hidden=true;$('playbackStatus').textContent='未开始收听'}
function drawLcd(pages){const c=$('lcdCanvas'),ctx=c.getContext('2d');ctx.fillStyle='#35492c';ctx.fillRect(0,0,128,64);if(!pages||pages.length!==8||!pages[0]){ctx.fillStyle='#e7f0dd';ctx.font='8px monospace';ctx.fillText('LCD LIVE',42,28);ctx.fillText('waiting...',40,40);return}ctx.fillStyle='#e7f0dd';for(let page=0;page<8;page++){const bin=atob(pages[page]);for(let x=0;x<128;x++){const b=bin.charCodeAt(x);for(let bit=0;bit<8;bit++)if(b&(1<<bit))ctx.fillRect(x,page*8+bit,1,1)}}}
if(nativeApp){document.querySelector('.audioBar .grid2').style.display='none';$('playbackStatus').textContent='使用 App 底部按钮后台收听'}loadSettings().then(refresh).catch(e=>{$('audioStatus').textContent=e.message});selectFreqTarget('rx');setInterval(()=>refresh().catch(()=>{}),350);
</script></body></html>"""


HTML = HTML.replace('<div class="channelPanel">', VOICE_HTML + '<div class="channelPanel">', 1)
HTML = HTML.replace('</script></body>', VOICE_JS + '</script></body>', 1)


@app.get("/")
def index() -> str:
    tones = "\n".join(f'<option value="{i}">{tone:.1f} Hz</option>' for i, tone in enumerate(CTCSS))
    steps = "\n".join(f'<option value="{value}"{" selected" if value == "4" else ""}>{label}</option>' for value, label in STEP_LEVELS)
    channel_tones = "".join(f'<option value="{x["value"]}">{x["label"]}</option>' for x in CHANNEL_TONES)
    return HTML.replace("__TONES__", tones).replace("__STEPS__", steps).replace("__CHANNEL_TONES__", channel_tones)


def state_json() -> dict:
    s = link.state
    radio_health = link.health()
    audio_health = audio.health()
    return {
        "connection_type": s.connection_type,
        "port": s.port,
        "baud": s.baud,
        "tcp_host": s.tcp_host,
        "tcp_port": s.tcp_port,
        "connected": s.connected,
        "radio_health": radio_health,
        "rx_mhz": s.rx_mhz,
        "tx_mhz": s.tx_mhz,
        "tx_ctcss": s.tx_ctcss,
        "rx_ctcss": s.rx_ctcss,
        "tx_ctcss_hz": None if s.tx_ctcss == "off" else CTCSS[int(s.tx_ctcss)],
        "rx_ctcss_hz": None if s.rx_ctcss == "off" else CTCSS[int(s.rx_ctcss)],
        "mode": s.mode,
        "monitor": s.monitor,
        "power": s.power,
        "bandwidth": s.bandwidth,
        "squelch": s.squelch,
        "radio_step": s.radio_step,
        "bcl": s.bcl,
        "compander": s.compander,
        "lcd": s.lcd,
        "lcd_age_ms": s.lcd_age_ms,
        "viewer_flags": s.viewer_flags,
        "tx_led": bool(s.viewer_flags & 2) and s.lcd_age_ms is not None and s.lcd_age_ms < 5000,
        "ptt_ready": link.ptt_link_ready() and (not link._aioc or link.tx_output_device is not None),
        "ptt_commanded": bool(link._ptt_lease and time.monotonic() < link._ptt_deadline),
        "tx_audio_level": link._tx_audio_level if time.monotonic() - link._tx_audio_at < 1 else 0,
        "tx_audio_recent": bool(link._tx_audio_at and time.monotonic() - link._tx_audio_at < 1),
        "tx_audio_bytes": link._tx_audio_bytes,
        **link._tx_buffer.stats(),
        "last_error": s.last_error,
        "logs": s.logs,
        "audio_active": audio_health in ("starting", "capturing"),
        "audio_health": audio_health,
        "audio_device": audio.device,
        "audio_rate": audio.sample_rate,
        "audio_channels": audio.channels,
        "audio_level": audio.level,
        "audio_listeners": len(audio.listeners),
        "audio_error": audio.last_error,
        "settings_revision": SETTINGS_REVISION,
        "voice_tx": voice_presets.state(),
    }


@app.get('/api/voice/presets')
def voice_list():
    return jsonify(voice_presets.snapshot())


@app.get('/api/voice/state')
def voice_state():
    return jsonify(voice_presets.state())


@app.post('/api/voice/recording')
def voice_recording():
    if request.content_length is None or not 3200 <= request.content_length <= 1920000:
        return jsonify({'error': '录音需为 0.1–60 秒'}), 413
    try:
        revision = request.args.get('revision')
        item = voice_presets.save_recording(request.args.get('label', ''), request.get_data(cache=False),
                                           request.args.get('id', ''), int(revision) if revision else None)
        return jsonify(item)
    except (ValueError, OSError) as exc:
        return jsonify({'error': str(exc)}), 409


@app.get('/api/voice/voices')
def voice_voices():
    try:
        return jsonify({'voices': voice_presets.get_voices()})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 409


@app.post('/api/voice/presets')
def voice_save():
    try:
        return jsonify(voice_presets.save(request.get_json(silent=True) or {}))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 409


@app.delete('/api/voice/presets/<item_id>')
def voice_delete(item_id):
    try:
        voice_presets.delete(item_id, (request.get_json(silent=True) or {}).get('revision'))
        return jsonify({'ok': True})
    except (ValueError, OSError) as exc:
        return jsonify({'error': str(exc)}), 409


@app.get('/api/voice/presets/<item_id>/audio')
def voice_preview(item_id):
    try:
        return send_file(voice_presets.ready_path(item_id), mimetype='audio/wav', max_age=0)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409


@app.post('/api/voice/send')
def voice_send():
    try:
        voice_presets.send(str((request.get_json(silent=True) or {}).get('id', '')))
        return jsonify({'ok': True})
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({'error': str(exc)}), 409


@app.post('/api/voice/stop')
def voice_stop():
    voice_presets.stop()
    return jsonify({'ok': True})


@app.get("/api/ports")
def ports() -> Response:
    return jsonify({"ports": [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in list_ports.comports()]})


@app.get("/api/audio/devices")
def audio_devices() -> Response:
    return jsonify({"devices": audio.devices()})


@app.get("/api/audio/outputs")
def audio_outputs() -> Response:
    return jsonify({"devices": audio.output_devices()})


@app.post("/api/audio/output")
def select_audio_output() -> Response:
    try:
        data = request.get_json(force=True)
        set_tx_output(int(data["device"]), data.get("revision"), int(data.get("gain", 20)))
    except SettingsChangedError as exc:
        return jsonify({"error": str(exc)}), 409
    except (ValueError, KeyError, TypeError, sd.PortAudioError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(settings_json())


@app.post("/api/audio/start")
def start_audio() -> Response:
    try:
        data = request.get_json(force=True)
        start_configured_audio(int(data["device"]), data.get("revision"))
    except SettingsChangedError as exc:
        return jsonify({"error": str(exc)}), 409
    except (ValueError, KeyError, TypeError, sd.PortAudioError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(state_json())


@app.post("/api/audio/stop")
def stop_audio() -> Response:
    try:
        stop_configured_audio((request.get_json(silent=True) or {}).get("revision"))
    except SettingsChangedError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(state_json())


@app.get("/api/audio/stream")
def audio_stream() -> Response:
    if audio.health() not in ("starting", "capturing"):
        return jsonify({"error": "请先在电脑服务端或设置中选择录音设备并启动采集"}), 409
    return Response(audio.subscribe(), mimetype="application/octet-stream",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/state")
def get_state() -> Response:
    return jsonify(state_json())


@app.get("/api/settings")
def get_settings() -> Response:
    return jsonify(settings_json())


@app.get("/api/channel/<int:number>")
def get_channel(number: int) -> Response:
    try:
        return jsonify(read_channel(number))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.get('/api/channel-tone-options')
def get_channel_tone_options():
    return jsonify({'options': channel_tone_choices()})


@app.post("/api/channel/<int:number>")
def put_channel(number: int) -> Response:
    try:
        return jsonify(write_channel(number, request.get_json(force=True)))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        link.log("err", f"信道 {number} 写入失败: {exc}")
        return jsonify({"error": str(exc)}), 503


@app.get("/api/capabilities")
def capabilities() -> Response:
    return jsonify({
        "firmware": "本地 v6.3 + ENABLE_FEAT_F4HWN_K5VIEWER + DTrac 参数",
        "screen": "K5Viewer 实时 LCD 差分帧",
        "common": ["接收频率", "发射频率", "接收亚音", "发射亚音", "模式", "功率", "静噪", "步进", "监听"],
        "extended": ["宽/窄带", "BCL 忙道锁定", "压扩"],
    })


@app.post("/api/connect")
def connect_radio() -> Response:
    data = request.get_json(force=True)
    mode = str(data.get("mode") or "serial")
    try:
        connect_configured_radio(mode, str(data.get("port") or ""), int(data.get("baud") or 38400),
                                 str(data.get("host") or "127.0.0.1"), int(data.get("tcp_port") or 9527),
                                 data.get("revision"), str(data.get("ptt_mode") or "auto"))
    except SettingsChangedError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        link.state.last_error = str(exc)
        link.log("err", f"连接失败: {exc}")
        return jsonify({"error": str(exc)}), 500
    return jsonify(state_json())


@app.post("/api/disconnect")
def disconnect_radio() -> Response:
    try:
        disconnect_configured_radio((request.get_json(silent=True) or {}).get("revision"))
    except SettingsChangedError as exc:
        return jsonify({"error": str(exc)}), 409
    link.log("ok", "已断开")
    return jsonify(state_json())


def send_tone(kind: str, value: str) -> None:
    code = 255 if value == "off" else int(value)
    if code != 255 and not 0 <= code < len(CTCSS):
        raise ValueError("亚音索引无效")
    if kind == "rx_ctcss":
        link.send(0x9998, struct.pack("<B", code), "关闭接收亚音" if code == 255 else f"设置接收 CTCSS {CTCSS[code]:.1f} Hz")
        link.state.rx_ctcss = "off" if code == 255 else str(code)
    else:
        link.send(0x9999, struct.pack("<B", code), "关闭发射亚音" if code == 255 else f"设置发射 CTCSS {CTCSS[code]:.1f} Hz")
        link.state.tx_ctcss = "off" if code == 255 else str(code)


@app.post("/api/send")
def send_command() -> Response:
    data = request.get_json(force=True)
    kind = str(data.get("kind") or "")
    try:
        if kind == "rx":
            mhz = float(data["rx_mhz"])
            link.send(0x8888, frequency_payload(mhz), f"设置接收频率 {mhz:.6f} MHz")
            link.state.rx_mhz = mhz
        elif kind == "tx":
            mhz = float(data["tx_mhz"])
            link.send(0x7777, frequency_payload(mhz), f"设置发射频率 {mhz:.6f} MHz")
            link.state.tx_mhz = mhz
        elif kind in ("ctcss", "tx_ctcss"):
            send_tone("tx_ctcss", str(data.get("tx_ctcss", data.get("ctcss", "off"))))
        elif kind == "rx_ctcss":
            send_tone("rx_ctcss", str(data.get("rx_ctcss", "off")))
        elif kind == "mode":
            mode = str(data.get("mode", "F")).upper()
            if mode not in MODES:
                raise ValueError("调制模式无效")
            link.send(0x6666, mode.encode("ascii"), f"设置模式 {MODES[mode]}")
            link.state.mode = mode
        elif kind == "power":
            value = int(data.get("power", 7))
            if value not in POWER_LEVELS:
                raise ValueError("功率档位无效")
            link.send(0x9997, setting_payload(PARAM_POWER, value), f"设置功率 {POWER_LEVELS[value]}")
            link.state.power = value
        elif kind == "bandwidth":
            value = int(data.get("bandwidth", 0))
            if value not in (0, 1):
                raise ValueError("宽窄带无效")
            link.send(0x9997, setting_payload(PARAM_BANDWIDTH, value), "设置宽带" if value == 0 else "设置窄带")
            link.state.bandwidth = value
        elif kind == "squelch":
            value = int(data.get("squelch", 1))
            if not 0 <= value <= 9:
                raise ValueError("静噪范围应为 0-9")
            link.send(0x9997, setting_payload(PARAM_SQUELCH, value), f"设置静噪 SQL {value}")
            link.state.squelch = value
        elif kind == "radio_step":
            value = int(data.get("radio_step", 4))
            if not 0 <= value <= 23:
                raise ValueError("步进索引无效")
            label = dict(STEP_LEVELS)[str(value)]
            link.send(0x9997, setting_payload(PARAM_STEP, value), f"设置电台菜单步进 {label}")
            link.state.radio_step = value
        elif kind == "bcl":
            value = int(data.get("bcl", 0))
            if value not in (0, 1):
                raise ValueError("BCL 值无效")
            link.send(0x9997, setting_payload(PARAM_BCL, value), "开启 BCL" if value else "关闭 BCL")
            link.state.bcl = value
        elif kind == "compander":
            value = int(data.get("compander", 0))
            if not 0 <= value < len(COMPANDER_LEVELS):
                raise ValueError("压扩值无效")
            link.send(0x9997, setting_payload(PARAM_COMPANDER, value), f"设置压扩 {COMPANDER_LEVELS[value]}")
            link.state.compander = value
        elif kind == "monitor_on":
            link.send(0x5555, b"Y", "监听开")
            link.state.monitor = True
        elif kind == "monitor_off":
            link.send(0x5555, b"N", "监听关")
            link.state.monitor = False
        else:
            return jsonify({"error": "未知命令"}), 400
        link.state.last_error = None
    except Exception as exc:
        link.state.last_error = str(exc)
        link.log("err", str(exc))
        return jsonify({"error": str(exc)}), 500
    return jsonify(state_json())


@app.post("/api/key")
def send_key() -> Response:
    data = request.get_json(force=True)
    key = str(data.get("key") or "").upper()
    if key not in KEY_CODES:
        return jsonify({"error": "未知按键"}), 400
    try:
        link.send_viewer_key(key)
    except Exception as exc:
        link.state.last_error = str(exc)
        link.log("err", str(exc))
        return jsonify({"error": str(exc)}), 500
    return jsonify(state_json())


@app.post("/api/ptt")
def ptt_control() -> Response:
    data = request.get_json(silent=True) or {}
    try:
        lease = link.ptt(str(data.get("action") or ""), str(data.get("lease") or ""))
        return jsonify({"ok": True, "lease": lease})
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.post("/api/tx/audio")
def tx_audio() -> Response:
    if request.content_length is None or not 0 < request.content_length <= 4096:
        return jsonify({"error": "音频块长度无效"}), 400
    try:
        link.receive_tx_audio(request.args.get("lease", ""), request.get_data(cache=False))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"ok": True})


@app.get("/api/events")
def events() -> Response:
    def stream():
        last = -1
        while True:
            if len(link.state.logs) != last:
                last = len(link.state.logs)
                yield f"data: {json.dumps(state_json(), ensure_ascii=False)}\n\n"
            time.sleep(0.5)
    return Response(stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    restore_devices()
    host_ip = socket.gethostbyname(socket.gethostname())
    print(f"Local:  http://127.0.0.1:8090/?token={ACCESS_TOKEN}")
    print(f"Phone:  http://{host_ip}:8090/?token={ACCESS_TOKEN}")
    app.run(host="0.0.0.0", port=8090, debug=False, threaded=True)
