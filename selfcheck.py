import queue
import math
import array
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server
import gui


class ReceiveCheck(unittest.TestCase):
    def test_phone_address_choices_exclude_virtual_meta_ip(self):
        with patch.object(gui.socket, "gethostname", return_value="host"), \
             patch.object(gui.socket, "gethostbyname_ex", return_value=("host", [], ["192.168.188.1", "10.70.8.42", "198.18.0.1"])):
            self.assertEqual(gui.phone_addresses(), ["192.168.188.1", "10.70.8.42"])

    def test_v63_rejects_wrong_uart_baud_before_opening_port(self):
        with patch.object(server.link, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "38400"):
                server.connect_configured_radio("serial", "COM3", 115200)
            connect.assert_not_called()

    def test_k1_usb_enables_dtr_without_keying_other_cables(self):
        ports = [SimpleNamespace(device="COM8", vid=0x36B7, pid=0xFFFF),
                 SimpleNamespace(device="COM9", vid=0x1A86, pid=0x7523)]
        opened = []
        class FakeSerial:
            def __init__(self, **kwargs):
                self.dtr = None
                self.rts = None
                self.port = None
            def open(self):
                opened.append((self.port, self.dtr, self.rts))
            def close(self): pass
        with patch.object(server.list_ports, "comports", return_value=ports), \
             patch.object(server.serial, "Serial", FakeSerial), \
             patch.object(server.RadioLink, "_mark_connected"):
            link = server.RadioLink()
            link.connect("com8")
            link.connect("COM9")
        self.assertEqual(opened, [("com8", True, False), ("COM9", False, False)])

    def test_generic_serial_can_be_selected_as_aioc_without_keying_on_open(self):
        port = SimpleNamespace(device="COM7", vid=0x1234, pid=0x5678, description="USB Serial")
        opened = []
        class FakeSerial:
            def __init__(self, **kwargs):
                self.dtr = None
                self.rts = None
                self.port = None
            def open(self): opened.append((self.port, self.dtr, self.rts))
            def close(self): pass
        with patch.object(server.list_ports, "comports", return_value=[port]), \
             patch.object(server.serial, "Serial", FakeSerial), \
             patch.object(server.RadioLink, "_mark_connected"):
            link = server.RadioLink()
            link.connect("COM7", ptt_mode="aioc")
        self.assertEqual(opened, [("COM7", False, False)])
        self.assertTrue(link._aioc)

    def test_access_and_receive_audio(self):
        client = server.app.test_client()
        self.assertEqual(client.get("/api/state").status_code, 403)
        response = client.get("/?token=" + server.ACCESS_TOKEN)
        self.assertEqual(response.status_code, 200)
        self.assertIn("K5Viewer", response.get_data(as_text=True))
        self.assertEqual(client.get("/api/audio/stream").status_code, 409)

        listener = queue.Queue(maxsize=1)
        server.audio.listeners.add(listener)
        try:
            server.audio._capture(memoryview(b"\x00\x00\xff\x7f"), 2, None, None)
            self.assertEqual(listener.get_nowait(), b"\x00\x00\xff\x7f")
            server.audio._capture(memoryview(b"\x01\x00"), 1, None, None)
            server.audio._capture(memoryview(b"\x02\x00"), 1, None, None)
            self.assertEqual(listener.get_nowait(), b"\x02\x00")
        finally:
            server.audio.listeners.remove(listener)

    def test_firmware_frame_and_ptt_lease(self):
        self.assertEqual(server.frequency_payload("439.500000"), (439500000).to_bytes(4, "little"))
        self.assertEqual(server.frame(0x8888, server.frequency_payload("439.500000"))[:2], b"\xab\xcd")
        with self.assertRaises(RuntimeError):
            server.link.send_viewer_key("PTT")
        link = server.RadioLink()
        link.state.connected = True
        link._reader_thread = type("Reader", (), {"is_alive": lambda self: True})()
        link._last_lcd_at = time.monotonic() - 30
        self.assertEqual(link.health(), "online")  # Static screen must not de-key remote PTT.
        commands = []
        with patch.object(link, "send_raw", side_effect=lambda data, *args: commands.append(data)):
            lease = link.ptt("start")
            with self.assertRaisesRegex(RuntimeError, "另一端"):
                link.ptt("start")
            self.assertEqual(link.ptt("renew", lease), lease)
            with self.assertRaisesRegex(ValueError, "无效"):
                link.ptt("stop", "other")
            link.ptt("stop", lease)
            self.assertIsNone(link._ptt_lease)
            self.assertEqual(commands, [b"\xaa\x55\x03\x10", b"\xaa\x55\x03\x10", b"\xaa\x55\x04\x10"])
            lease = link.ptt("start")
            link._ptt_deadline -= 1
            with self.assertRaisesRegex(ValueError, "已释放"):
                link.ptt("renew", lease)
            link._reader_thread = None
            link.close()
            self.assertEqual(commands[-1], b"\xaa\x55\x04\x10")
        watchdog = server.RadioLink()
        watchdog.state.connected = True
        watchdog._reader_thread = type("Reader", (), {"is_alive": lambda self: True})()
        watchdog._last_lcd_at = time.monotonic()
        watchdog._ptt_lease = "expired"
        watchdog._ptt_deadline = time.monotonic() - 1
        watchdog._sock = type("Socket", (), {"recv": lambda self, n: (watchdog._reader_stop.set(), b"\0")[1]})()
        sent = []
        with patch.object(watchdog, "send_raw", side_effect=lambda data, *args, **kwargs: sent.append(data)):
            watchdog._viewer_loop()
        self.assertEqual(sent[0], b"\xaa\x55\x04\x10")
        self.assertIsNone(watchdog._ptt_lease)
        client = server.app.test_client()
        self.assertEqual(client.post("/api/ptt", json={"action": "start"}).status_code, 403)
        with patch.object(server.link, "ptt", return_value="lease") as ptt:
            response = client.post("/api/ptt?token=" + server.ACCESS_TOKEN, json={"action": "start"})
            self.assertEqual(response.get_json()["lease"], "lease")
            ptt.assert_called_once_with("start", "")

    def test_aioc_ptt_keys_dtr_and_plays_phone_pcm_then_releases(self):
        events = []
        class FakeSerial:
            is_open = True
            rts = False
            @property
            def dtr(self): return getattr(self, "_dtr", False)
            @dtr.setter
            def dtr(self, value): self._dtr = value; events.append(("dtr", value))
        class FakeOutput:
            active = True
            def start(self): events.append(("audio", "start"))
            def abort(self): events.append(("audio", "abort"))
            def close(self): events.append(("audio", "close"))
        link = server.RadioLink()
        link.state.connected = True
        link._reader_thread = type("Reader", (), {"is_alive": lambda self: True})()
        self.assertEqual(link.health(), "waiting")
        link._ser = FakeSerial()
        link._aioc = True
        link.tx_output_device = 4
        with patch.object(server.sd, "RawOutputStream", return_value=FakeOutput()) as open_output, \
             patch.object(server.sd, "query_devices", return_value={"hostapi": 0}), \
             patch.object(server.sd, "query_hostapis", return_value={"name": "Windows WASAPI"}):
            lease = link.ptt("start")
            link.receive_tx_audio(lease, struct.pack("<hh", 12000, -12000) * 800)
            link.receive_tx_audio(lease, struct.pack("<hh", 12000, -12000) * 800)
            rendered = bytearray(1920)
            open_output.call_args.kwargs["callback"](rendered, 960, None, None)
            self.assertEqual(rendered[:12], struct.pack("<hhhhhh", 800, 1600, 2400, 800, -800, -2400))
            link.ptt("stop", lease)
            link._ser.is_open = False
            with self.assertRaises(RuntimeError):
                link.ptt("start")
        self.assertEqual(open_output.call_args.kwargs["device"], 4)
        self.assertEqual(open_output.call_args.kwargs["samplerate"], 48000)
        self.assertIn("extra_settings", open_output.call_args.kwargs)
        self.assertLess(events.index(("audio", "start")), events.index(("dtr", True)))
        self.assertLess(events.index(("dtr", False)), events.index(("audio", "abort")))
        self.assertFalse(link._ser.dtr)

    def test_aioc_output_failure_releases_ptt(self):
        class BrokenOutput:
            active = True
            def start(self): pass
            def abort(self): pass
            def close(self): pass
        link = server.RadioLink()
        link.state.connected = True
        link._reader_thread = type("Reader", (), {"is_alive": lambda self: True})()
        link._last_lcd_at = time.monotonic()
        link._ser = SimpleNamespace(dtr=False, rts=False, is_open=True)
        link._aioc = True
        link.tx_output_device = 4
        output = BrokenOutput()
        with patch.object(server.sd, "RawOutputStream", return_value=output), \
             patch.object(server.sd, "query_devices", return_value={"hostapi": 0}), \
             patch.object(server.sd, "query_hostapis", return_value={"name": "MME"}):
            lease = link.ptt("start")
            output.active = False
            with self.assertRaisesRegex(ValueError, "未启动"):
                link.receive_tx_audio(lease, struct.pack("<h", 1000))
        self.assertIsNone(link._ptt_lease)
        self.assertFalse(link._ser.dtr)

    def test_aioc_rejects_uart_writes_while_ptt_is_active(self):
        link = server.RadioLink()
        link.state.connected = True
        link._aioc = True
        link._ptt_lease = 'test'
        with self.assertRaisesRegex(RuntimeError, '发射期间暂停串口'):
            link.send_raw(b'\x55\xaa\x00\x00')

    def test_tx_buffer_bounds_delay_and_recovers_after_network_gap(self):
        buffer = server.TxAudioBuffer()
        out = bytearray(640)
        buffer.render(out, 320, None, None)
        self.assertEqual(out, bytes(640))
        buffer.push(struct.pack("<h", 1234) * 1600)
        for _ in range(5):
            buffer.render(out, 320, None, None)
            self.assertEqual(out, struct.pack("<h", 1234) * 320)
        buffer.render(out, 320, None, None)
        self.assertEqual(out, bytes(640))
        self.assertEqual(buffer.underruns, 1)
        buffer.push(struct.pack("<h", 5678) * 8000)
        self.assertEqual(buffer.stats()["tx_buffer_ms"], 240)
        self.assertEqual(buffer.dropped_bytes, 16000 - 7680)
        buffer.render(out, 320, None, None)
        self.assertEqual(out, struct.pack("<h", 5678) * 320)

    def test_valid_aioc_audio_renews_lease_but_never_revives_stopped_ptt(self):
        link = server.RadioLink()
        link._aioc = True
        link._ser = SimpleNamespace(dtr=True)
        link._tx_stream = SimpleNamespace(active=True, abort=lambda: None, close=lambda: None)
        link._ptt_lease = 'lease'
        link._ptt_deadline = 100.65
        with patch.object(server.time, 'monotonic', return_value=100.5):
            link.receive_tx_audio('lease', bytes(3200))
            self.assertAlmostEqual(link._ptt_deadline, 101.15)
            link.ptt('stop', 'lease')
            with self.assertRaisesRegex(ValueError, '已释放'):
                link.receive_tx_audio('lease', bytes(3200))
        self.assertFalse(link._ser.dtr)
        self.assertIsNone(link._ptt_lease)
        link._ptt_lease = 'expired'
        link._ptt_deadline = 101
        with patch.object(server.time, 'monotonic', return_value=101.1):
            with self.assertRaisesRegex(ValueError, '已释放'):
                link.receive_tx_audio('expired', bytes(3200))
        self.assertEqual(link._ptt_deadline, 101)

    def test_tx_resampling_preserves_duration_pitch_and_chunk_continuity(self):
        buffer = server.TxAudioBuffer(48000)
        source = array.array('h', (round(12000 * math.sin(2 * math.pi * 1000 * n / 16000)) for n in range(1600)))
        buffer.push(source[:800].tobytes())
        buffer.push(source[800:].tobytes())
        self.assertEqual(len(buffer.data), 9600)
        samples = array.array('h')
        samples.frombytes(buffer.data)
        crossings = sum(samples[i - 1] <= 0 < samples[i] for i in range(1, len(samples)))
        self.assertEqual(crossings, 100)  # 1 kHz for exactly 100 ms at 48 kHz.
        self.assertEqual(buffer.stats()['tx_buffer_ms'], 100)
        whole = server.TxAudioBuffer(48000)
        whole.push(source.tobytes())
        self.assertEqual(whole.data, buffer.data)

    def test_phone_uplink_meter_requires_active_lease_and_resets(self):
        client = server.app.test_client()
        path = "/api/tx/audio?token=" + server.ACCESS_TOKEN + "&lease=test-lease"
        tone = struct.pack("<" + "h" * 160, *([12000] * 160))
        self.assertEqual(client.post("/api/tx/audio?lease=test-lease", data=tone).status_code, 403)
        self.assertEqual(client.post(path, data=tone).status_code, 409)
        with server.link._ptt_lock:
            server.link._ptt_lease = "test-lease"
            server.link._ptt_deadline = time.monotonic() + 5
            server.link._tx_audio_bytes = 0
        try:
            self.assertEqual(client.post(path, data=b"x").status_code, 409)
            self.assertEqual(client.post(path, data=tone * 20).status_code, 400)
            self.assertEqual(client.post(path, data=bytes(len(tone))).status_code, 200)
            self.assertEqual(client.get("/api/state?token=" + server.ACCESS_TOKEN).get_json()["tx_audio_level"], 0)
            self.assertEqual(client.post(path, data=tone).status_code, 200)
            state = client.get("/api/state?token=" + server.ACCESS_TOKEN).get_json()
            self.assertTrue(state["tx_audio_recent"])
            self.assertGreater(state["tx_audio_level"], 0)
            self.assertEqual(state["tx_audio_bytes"], 2 * len(tone))
        finally:
            with server.link._ptt_lock:
                server.link._release_ptt()
        self.assertFalse(client.get("/api/state?token=" + server.ACCESS_TOKEN).get_json()["tx_audio_recent"])

    def test_channel_read_modify_verify_and_stale_guard(self):
        memory = {0: struct.pack("<II", 43950000, 500000) + bytes([0, 0, 0, 0x21, 0x9e, 0, 4, 0]),
                  0x4000: "测试一号".encode().ljust(16, b"\0"),
                  0x8000: bytes([5, 0xaa, 0x63, 0x55, 0x42, 0x66, 0x21, 0x77])}
        writes = []
        def read(offset, size):
            return memory[offset]
        def write(offset, data):
            writes.append((offset, data))
            memory[offset] = data
        with patch.object(server.link, "read_eeprom", side_effect=read), patch.object(server.link, "write_eeprom", side_effect=write):
            before = server.read_channel(1)
            self.assertEqual(before["tx_mhz"], "444.50000")
            result = server.write_channel(1, {"etag": before["etag"], "name": "新名称", "rx_mhz": "145.50000", "tx_mhz": "145.50000"})
            self.assertEqual(result["name"], "新名称")
            self.assertEqual([offset for offset, _ in writes], [0, 0x4000, 0x8000])
            self.assertEqual(memory[0][8:], bytes([0, 0, 0, 0x20, 0x9e, 0, 4, 0]))
            self.assertEqual(memory[0x8000][1:], bytes([0xaa, 0x63, 0x55, 0x42, 0x66, 0x21, 0x77]))
            with self.assertRaisesRegex(ValueError, "重新读取"):
                server.write_channel(1, {"etag": before["etag"], "name": "旧版本", "rx_mhz": "145.5", "tx_mhz": "145.5"})

    def test_channel_tones_independent_and_verified(self):
        memory = {0: struct.pack("<II", 43950000, 0) + bytes([12, 0, 0x21, 0x20, 0x9e, 0, 4, 0]),
                  0x4000: b"TEST".ljust(16, b"\0"),
                  0x8000: bytes([5, 0xaa, 0x63, 0x55, 0x42, 0x66, 0x21, 0x77])}
        def write(offset, data):
            memory[offset] = data
        with patch.object(server.link, "read_eeprom", side_effect=lambda offset, size: memory[offset]), patch.object(server.link, "write_eeprom", side_effect=write) as writer:
            before = server.read_channel(1)
            self.assertEqual((before["rx_tone"], before["tx_tone"]), ("ctcss:100.0", "dcs:023N"))
            data = {k: before[k] for k in ("etag", "name", "rx_mhz", "tx_mhz")}
            data.update(rx_tone="off", tx_tone="dcs:754I")
            after = server.write_channel(1, data)
            self.assertEqual((after["rx_tone"], after["tx_tone"]), ("off", "dcs:754I"))
            self.assertEqual(memory[0][8:11], bytes([0, 103, 0x30]))
            self.assertEqual(memory[0][11:], bytes([0x20, 0x9e, 0, 4, 0]))
            self.assertEqual(writer.call_count, 1)
            data.update(etag=after["etag"], rx_tone="ctcss:88.5", tx_tone=after["tx_tone"])
            after = server.write_channel(1, data)
            self.assertEqual(memory[0][8:11], bytes([8, 103, 0x31]))
            data.update(etag=after["etag"], tx_tone="dcs:999N")
            writer.reset_mock()
            with self.assertRaisesRegex(ValueError, "亚音"):
                server.write_channel(1, data)
            writer.assert_not_called()
            data.update(tx_tone="off")
            writer.side_effect = lambda *_: None
            with self.assertRaisesRegex(RuntimeError, "回读不一致"):
                server.write_channel(1, data)
        self.assertEqual(len(server.CHANNEL_TONES), 259)

    def test_channel_unknown_tone_preserved(self):
        raw = (struct.pack("<II", 43950000, 0) + bytes([250, 77, 0x05, 0x20, 0x9e, 0, 4, 0]),
               b"TEST".ljust(16, b"\0"), bytes([5, 0, 0, 0, 0, 0, 0, 0]))
        with patch.object(server, "_channel_raw", return_value=raw), patch.object(server.link, "write_eeprom") as writer:
            before = server.read_channel(1)
            self.assertEqual(before["rx_tone"], "raw:5:250")
            data = {k: before[k] for k in ("etag", "name", "rx_mhz", "tx_mhz", "rx_tone", "tx_tone")}
            server.write_channel(1, data)
            writer.assert_not_called()
            data["rx_tone"] = "raw:5:249"
            with self.assertRaisesRegex(ValueError, "亚音"):
                server.write_channel(1, data)

    def test_mixed_viewer_and_eeprom_reply(self):
        link = server.RadioLink()
        waiter = queue.Queue()
        link._reply_waiter = waiter
        body = struct.pack("<HHHBB", 0x051C, 4, 0, 0, 0)
        size = len(body)
        packet = b"\xab\xcd" + struct.pack("<H", size) + bytes(
            byte ^ server.OBFUSCATION[i % 16] for i, byte in enumerate(body)) + bytes(
            server.OBFUSCATION[(size + i) % 16] ^ 0xff for i in range(2)) + b"\xdc\xba"
        link._parse_viewer(b"\xaa\x55\x01\x00\x00\x0a" + packet[:6])
        link._parse_viewer(packet[6:])
        self.assertEqual(waiter.get_nowait(), (0x051C, struct.pack("<HBB", 0, 0, 0)))

    def test_stereo_capture_uses_device_rate_and_both_channels(self):
        capture = server.ReceiveAudio()
        opened = {}
        class Stream:
            def start(self): pass
            def stop(self): pass
            def close(self): pass
        def open_stream(**kwargs):
            opened.update(kwargs)
            return Stream()
        with patch.object(capture, "devices", return_value=[{"id": 69, "name": "B2", "channels": 2, "rate": 48000, "api": "Windows WASAPI"}]), patch.object(server.sd, "RawInputStream", side_effect=open_stream):
            capture.start(69)
        self.assertEqual((opened["samplerate"], opened["channels"]), (48000, 2))
        listener = queue.Queue(maxsize=1)
        capture.listeners.add(listener)
        capture._capture(memoryview(struct.pack("<hhhh", 0, 16000, 0, -16000)), 2, None, None)
        self.assertEqual(listener.get_nowait(), struct.pack("<hh", 8000, -8000))
        self.assertGreater(capture.level, 0)
        capture.stop()

    def test_shared_audio_setting_persists_and_rejects_stale_remote_change(self):
        devices = [{"id": 69, "name": "Voicemeeter Out B2", "api": "Windows WASAPI", "rate": 48000, "channels": 2}]
        with tempfile.TemporaryDirectory() as folder, patch.object(server, "SETTINGS_PATH", Path(folder) / "settings.json"), patch.dict(server.SETTINGS, {"token": "test-token"}, clear=True), patch.object(server, "SETTINGS_REVISION", 0), patch.object(server.audio, "devices", return_value=devices), patch.object(server.audio, "start") as start, patch.object(server.audio, "stream", None):
            server.start_configured_audio(69)
            self.assertEqual(start.call_args.args, (69,))
            saved = json.loads(server.SETTINGS_PATH.read_text(encoding="utf-8"))
            self.assertEqual(saved["audio_device"]["name"], "Voicemeeter Out B2")
            self.assertTrue(saved["audio_enabled"])
            client = server.app.test_client()
            settings = client.get("/api/settings?token=" + server.ACCESS_TOKEN).get_json()
            self.assertEqual(settings["audio_device"], 69)
            self.assertNotIn("token", settings)
            stale = client.post("/api/audio/stop?token=" + server.ACCESS_TOKEN, json={"revision": 0})
            self.assertEqual(stale.status_code, 409)
            self.assertTrue(server.SETTINGS["audio_enabled"])
            devices[0]["id"] = 71
            self.assertEqual(server.selected_audio_id(), 71)

    def test_aioc_output_setting_persists_and_rejects_stale_remote_change(self):
        outputs = [{"id": 4, "name": "AIOC Speaker", "api": "Windows WASAPI", "rate": 48000}]
        with tempfile.TemporaryDirectory() as folder, patch.object(server, "SETTINGS_PATH", Path(folder) / "settings.json"), patch.dict(server.SETTINGS, {"token": "test-token"}, clear=True), patch.object(server, "SETTINGS_REVISION", 0), patch.object(server.audio, "output_devices", return_value=outputs), patch.object(server.link, "_ptt_lease", None):
            server.set_tx_output(4, gain=20)
            saved = json.loads(server.SETTINGS_PATH.read_text(encoding="utf-8"))
            self.assertEqual(saved["tx_output_device"]["name"], "AIOC Speaker")
            self.assertEqual(saved["tx_gain"], 20)
            client = server.app.test_client()
            response = client.post("/api/audio/output?token=" + server.ACCESS_TOKEN,
                                   json={"device": 4, "gain": 100, "revision": 0})
            self.assertEqual(response.status_code, 409)
            outputs[0]["id"] = 5
            self.assertEqual(server.selected_tx_output_id(), 5)

    def test_live_connection_indicators_allow_static_screen_and_detect_disconnect(self):
        link = server.RadioLink()
        link.state.connected = True
        link._reader_thread = type("Reader", (), {"is_alive": lambda self: True})()
        link._connected_at = time.monotonic()
        self.assertEqual(link.health(), "waiting")
        link._last_lcd_at = time.monotonic()
        self.assertEqual(link.health(), "online")
        link._last_lcd_at -= 6
        self.assertEqual(link.health(), "online")
        link._sock = type("ClosedSocket", (), {"recv": lambda self, n: b""})()
        with patch.object(link, "send_raw"):
            link._viewer_loop()
        self.assertEqual(link.health(), "error")
        self.assertFalse(link.state.connected)

        capture = server.ReceiveAudio()
        capture.stream = type("LiveStream", (), {"active": True})()
        capture._started_at = time.monotonic()
        self.assertEqual(capture.health(), "starting")
        capture._last_capture_at = time.monotonic()
        self.assertEqual(capture.health(), "capturing")
        capture._last_capture_at -= 4
        self.assertEqual(capture.health(), "error")
        self.assertIn("3 秒", capture.last_error)


if __name__ == "__main__":
    unittest.main()
