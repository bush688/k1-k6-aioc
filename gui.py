"""Windows control window for the K1 receive server."""

import base64
import ipaddress
import os
import queue
import socket
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from werkzeug.serving import make_server

import server
from desktop_audio import HeadphoneMonitor, DesktopMicrophone


def phone_addresses() -> list[str]:
    # ponytail: offer every local IPv4 address; automatic interface choice cannot know the phone's Wi-Fi subnet.
    excluded = ipaddress.ip_network("198.18.0.0/15")
    return [ip for ip in dict.fromkeys(socket.gethostbyname_ex(socket.gethostname())[2])
            if ipaddress.ip_address(ip).is_private and ipaddress.ip_address(ip) not in excluded
            and not ipaddress.ip_address(ip).is_loopback and not ipaddress.ip_address(ip).is_link_local]


class ControlWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("K1 远程收听服务端 · AIOC v8.8 · 电脑耳麦")
        self.root.geometry("980x940")
        self.root.minsize(760, 700)
        self.events: queue.Queue[tuple[bool, str]] = queue.Queue()
        self.channel_events: queue.Queue[tuple[int, dict | Exception]] = queue.Queue()
        self.loaded_channel: dict | None = None
        self.headphones = HeadphoneMonitor(server.audio, server.link)
        self.desktop_mic = DesktopMicrophone(server.link)
        self.desktop_held = False
        self.last_lcd: tuple[str, ...] | None = None
        self.last_settings_revision = -1
        self.last_voice_revision = -1
        self.voice_items = []
        self.voice_edit_events = queue.Queue()
        self.voice_form = None
        self.port_number = int(os.environ.get("K1_SERVER_PORT", "8090"))
        try:
            self.httpd = make_server("0.0.0.0", self.port_number, server.app, threaded=True)
        except OSError as exc:
            messagebox.showerror("服务端无法启动", f"端口 {self.port_number} 无法使用：{exc}\n请先关闭旧服务端，再重新启动。")
            self.root.destroy()
            raise SystemExit(1) from exc
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        ips = phone_addresses()
        self.local_url = f"http://127.0.0.1:{self.port_number}/?token={server.ACCESS_TOKEN}"
        self.phone_urls = [f"http://{ip}:{self.port_number}/?token={server.ACCESS_TOKEN}" for ip in ips]
        self.local_address_var = tk.StringVar(value=self.local_url)
        self.phone_address_var = tk.StringVar(value=self.phone_urls[-1] if self.phone_urls else "")
        self._build()
        self.refresh_ports()
        self.refresh_audio()
        self._sync_settings()
        self._work(server.restore_devices)
        self.root.after(500, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<ButtonRelease-1>", self.release_desktop_ptt, add="+")
        self.root.bind("<FocusOut>", lambda event: self.root.after_idle(self._desktop_focus_check), add="+")
        self.root.bind("<Unmap>", lambda event: self.release_desktop_ptt() if event.widget is self.root else None, add="+")
        self.root.after(100, self._desktop_tick)

    def _build(self) -> None:
        viewport = tk.Canvas(self.root, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=viewport.yview)
        scrollbar.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)
        outer = ttk.Frame(viewport, padding=14)
        content = viewport.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda event: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(content, width=event.width))
        ttk.Label(outer, text="K1 远程收听服务端", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        self.status = ttk.Label(outer, text="电台未连接 · 音频未采集")
        self.status.pack(anchor="w", pady=(2, 10))

        network = ttk.LabelFrame(outer, text="服务地址", padding=10)
        network.pack(fill="x", pady=5)
        ttk.Label(network, text="本机").grid(row=0, column=0, sticky="w")
        ttk.Entry(network, width=72, state="readonly", textvariable=self.local_address_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(network, text="打开完整控制页", command=lambda: webbrowser.open(self.local_url)).grid(row=0, column=2)
        ttk.Label(network, text="手机").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(network, width=72, state="readonly", textvariable=self.phone_address_var,
                     values=self.phone_urls).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(network, text="复制手机地址", command=self.copy_phone_url).grid(row=1, column=2)
        ttk.Label(network, text="选择与手机 Wi-Fi 同网段的地址").grid(row=2, column=1, sticky="w")
        network.columnconfigure(1, weight=1)

        radio = ttk.LabelFrame(outer, text="电台连接 · AIOC / K头 / K1 USB-C · 选择后自动保存", padding=10)
        radio.pack(fill="x", pady=5)
        self.port = ttk.Combobox(radio, state="readonly", width=45)
        self.port.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.baud = ttk.Combobox(radio, state="readonly", width=10, values=("38400",))
        self.baud.set("38400")
        self.baud.grid(row=0, column=1, padx=6)
        ttk.Button(radio, text="刷新端口", command=self.refresh_ports).grid(row=0, column=2, padx=3)
        ttk.Button(radio, text="连接电台", command=self.connect_radio).grid(row=0, column=3, padx=3)
        ttk.Button(radio, text="断开", command=lambda: self._work(server.disconnect_configured_radio)).grid(row=0, column=4)
        ttk.Label(radio, text="方式").grid(row=1, column=0, sticky="w")
        self.connection_mode = ttk.Combobox(radio, state="readonly", width=10, values=("serial", "tcp"))
        self.connection_mode.grid(row=1, column=1, sticky="w", pady=5)
        self.tcp_host = tk.StringVar(value="127.0.0.1")
        self.tcp_port = tk.StringVar(value="9527")
        ttk.Label(radio, text="TCP 地址").grid(row=1, column=2, sticky="e")
        ttk.Entry(radio, width=18, textvariable=self.tcp_host).grid(row=1, column=3, sticky="ew", padx=3)
        ttk.Entry(radio, width=7, textvariable=self.tcp_port).grid(row=1, column=4, sticky="ew")
        self.radio_indicator = tk.Label(radio, text="● 电台未连接", fg="#b42318", anchor="w",
                                        font=("Microsoft YaHei UI", 11, "bold"))
        self.radio_indicator.grid(row=2, column=0, columnspan=5, sticky="ew", padx=3, pady=(4, 0))
        ttk.Label(radio, text="PTT 方式").grid(row=3, column=0, sticky="w")
        self.ptt_mode = ttk.Combobox(radio, state="readonly", values=("自动识别", "AIOC DTR", "固件串口命令"), width=17)
        self.ptt_mode.grid(row=3, column=1, columnspan=2, sticky="w", pady=4)
        self.ptt_mode.set("自动识别")
        radio.columnconfigure(0, weight=1)

        audio = ttk.LabelFrame(outer, text="接收音频 / 输入电平", padding=10)
        audio.pack(fill="x", pady=5)
        self.audio_device = ttk.Combobox(audio, state="readonly", width=65)
        self.audio_device.grid(row=0, column=0, columnspan=4, sticky="ew", padx=(0, 6))
        ttk.Button(audio, text="刷新", command=self.refresh_audio).grid(row=0, column=4)
        ttk.Button(audio, text="启动采集", command=self.start_audio).grid(row=1, column=0, sticky="ew", pady=7, padx=3)
        ttk.Button(audio, text="停止采集", command=lambda: self._work(server.stop_configured_audio)).grid(row=1, column=1, sticky="ew", padx=3)
        ttk.Button(audio, text="网页收听", command=lambda: webbrowser.open(self.local_url)).grid(row=1, column=2, sticky="ew", padx=3)
        ttk.Label(audio, text="网页会尝试自动收听；手机可能需点一次播放").grid(row=1, column=3, columnspan=2, sticky="w", padx=8)
        self.audio_indicator = tk.Label(audio, text="● 音频未采集", fg="#b42318", anchor="w",
                                        font=("Microsoft YaHei UI", 11, "bold"))
        self.audio_indicator.grid(row=2, column=0, columnspan=5, sticky="ew", padx=4, pady=(3, 0))
        self.audio_level = ttk.Label(audio, text="采集输入音量：未采集", font=("Microsoft YaHei UI", 11, "bold"))
        self.audio_level.grid(row=3, column=0, columnspan=5, sticky="w", padx=4)
        ttk.Style().configure("Audio.Horizontal.TProgressbar", thickness=22)
        self.audio_meter = ttk.Progressbar(audio, maximum=100, style="Audio.Horizontal.TProgressbar")
        self.audio_meter.grid(row=4, column=0, columnspan=5, sticky="ew", padx=4, pady=(5, 0))
        ttk.Label(audio, text="AIOC 发射输出").grid(row=5, column=0, sticky="w")
        self.tx_output = ttk.Combobox(audio, state="readonly", width=55)
        self.tx_output.grid(row=5, column=1, columnspan=2, sticky="ew", padx=4)
        self.tx_gain = tk.StringVar(value="20")
        ttk.Spinbox(audio, from_=1, to=100, width=5, textvariable=self.tx_gain).grid(row=5, column=3, sticky="e")
        ttk.Button(audio, text="保存输出", command=self.save_tx_output).grid(row=5, column=4, sticky="ew")
        self.tx_audio_level = ttk.Label(audio, text="发射音频到达：未收到",
                                        font=("Microsoft YaHei UI", 11, "bold"))
        self.tx_audio_level.grid(row=6, column=0, columnspan=5, sticky="w", padx=4, pady=(9, 0))
        self.tx_audio_meter = ttk.Progressbar(audio, maximum=100, style="Audio.Horizontal.TProgressbar")
        self.tx_audio_meter.grid(row=7, column=0, columnspan=5, sticky="ew", padx=4, pady=(5, 0))
        preset_row = ttk.Frame(audio)
        preset_row.grid(row=8, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        ttk.Label(preset_row, text="预制语音").pack(side="left")
        self.voice_choice = ttk.Combobox(preset_row, state="readonly", width=44)
        self.voice_choice.pack(side="left", fill="x", expand=True, padx=6)
        self.voice_send_button = ttk.Button(preset_row, text="发送语音", command=self.send_voice)
        self.voice_send_button.pack(side="left", padx=3)
        ttk.Button(preset_row, text="停止", command=server.voice_presets.stop).pack(side="left", padx=3)
        ttk.Button(preset_row, text="添加 / 编辑", command=self.edit_voice).pack(side="left", padx=3)
        self.voice_status = ttk.Label(audio, text="添加文字并生成语音后，下拉选择并发送")
        self.voice_status.grid(row=9, column=0, columnspan=5, sticky="w", pady=4)
        for col in range(4):
            audio.columnconfigure(col, weight=1)

        desktop = ttk.LabelFrame(outer, text="电脑耳机收听 / 话筒发射", padding=10)
        desktop.pack(fill="x", pady=5, before=audio)
        self.headphone_device = ttk.Combobox(desktop, state="readonly", width=65)
        self.microphone_device = ttk.Combobox(desktop, state="readonly", width=65)
        self.headphone_volume = tk.StringVar(value=str(server.SETTINGS.get('desktop_listen_volume', 80)))
        self.microphone_gain = tk.StringVar(value=str(server.SETTINGS.get('desktop_mic_gain', 100)))
        ttk.Label(desktop, text="耳机输出").grid(row=0, column=0, sticky="w")
        self.headphone_device.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6)
        ttk.Button(desktop, text="刷新设备", command=self.refresh_audio).grid(row=0, column=4)
        ttk.Label(desktop, text="收听音量 %").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(desktop, from_=0, to=100, width=6, textvariable=self.headphone_volume).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(desktop, text="开启耳机收听", command=self.start_headphones).grid(row=1, column=2, pady=5)
        ttk.Button(desktop, text="停止收听", command=self.headphones.stop).grid(row=1, column=3)
        self.headphone_status = ttk.Label(desktop, text="选择电脑耳机；先启动上方电台接收采集")
        self.headphone_status.grid(row=2, column=0, columnspan=5, sticky="w")
        ttk.Label(desktop, text="电脑话筒").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.microphone_device.grid(row=3, column=1, columnspan=4, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(desktop, text="话筒增益 %").grid(row=4, column=0, sticky="w")
        ttk.Spinbox(desktop, from_=1, to=300, width=6, textvariable=self.microphone_gain).grid(row=4, column=1, sticky="w", padx=6)
        self.desktop_ptt = ttk.Button(desktop, text="按住 PTT 讲话")
        self.desktop_ptt.grid(row=4, column=2, pady=6)
        self.desktop_ptt.bind('<ButtonPress-1>', self.press_desktop_ptt)
        self.desktop_ptt.bind('<Leave>', self.release_desktop_ptt)
        self.desktop_ptt.bind('<KeyPress-space>', self.press_desktop_ptt)
        self.desktop_ptt.bind('<KeyRelease-space>', self.release_desktop_ptt)
        ttk.Button(desktop, text="停止发射", command=self.release_desktop_ptt).grid(row=4, column=3)
        self.desktop_mic_status = ttk.Label(desktop, text="按住按钮或聚焦后按住空格讲话，松开停止")
        self.desktop_mic_status.grid(row=5, column=0, columnspan=5, sticky="w")
        self.desktop_mic_meter = ttk.Progressbar(desktop, maximum=100)
        self.desktop_mic_meter.grid(row=6, column=0, columnspan=5, sticky="ew", pady=(5, 0))
        desktop.columnconfigure(1, weight=1)
        self.microphone_device.bind('<<ComboboxSelected>>', self.release_desktop_ptt)
        self.audio_device_locked = tk.BooleanVar(value=False)
        ttk.Checkbutton(desktop, text="锁定音频设备", variable=self.audio_device_locked,
                        command=self.apply_audio_device_lock).grid(row=7, column=0, columnspan=5, sticky="w", pady=(6, 0))
        for selector in self.audio_device_selectors():
            selector.bind("<MouseWheel>", lambda event: self.scroll_audio_selector(event, viewport))


        control = ttk.LabelFrame(outer, text="电脑控制", padding=10)
        control.pack(fill="x", pady=5)
        self.rx = tk.StringVar(value="439.500000")
        self.tx = tk.StringVar(value="439.500000")
        ttk.Label(control, text="接收 MHz").grid(row=0, column=0, sticky="w")
        ttk.Entry(control, textvariable=self.rx, width=16).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(control, text="设置接收", command=lambda: self.set_frequency("rx")).grid(row=0, column=2)
        ttk.Label(control, text="发射 MHz").grid(row=0, column=3, sticky="w", padx=(15, 0))
        ttk.Entry(control, textvariable=self.tx, width=16).grid(row=0, column=4, sticky="ew", padx=5)
        ttk.Button(control, text="设置发射频率", command=lambda: self.set_frequency("tx")).grid(row=0, column=5)
        for i, name in enumerate(("MENU", "UP", "DOWN", "EXIT", "F1", "F2")):
            ttk.Button(control, text=name, command=lambda key=name: self._work(server.link.send_viewer_key, key)).grid(row=1, column=i, sticky="ew", padx=3, pady=8)
        ttk.Button(control, text="监听开", command=lambda: self.set_monitor(True)).grid(row=2, column=0, columnspan=2, sticky="ew", padx=3)
        ttk.Button(control, text="监听关", command=lambda: self.set_monitor(False)).grid(row=2, column=2, columnspan=2, sticky="ew", padx=3)
        ttk.Label(control, text="AIOC 发射：使用手机或网页按住 PTT").grid(row=2, column=4, columnspan=2, sticky="w", padx=8)
        control.columnconfigure(1, weight=1)
        control.columnconfigure(4, weight=1)

        channel = ttk.LabelFrame(outer, text="信道写频 · 先按号读取现有信道", padding=10)
        channel.pack(fill="x", pady=5)
        self.channel_number = tk.StringVar(value="1")
        self.channel_name = tk.StringVar()
        self.channel_rx = tk.StringVar()
        self.channel_tx = tk.StringVar()
        channel_row = ttk.Frame(channel)
        channel_row.pack(fill="x")
        ttk.Label(channel_row, text="信道号").pack(side="left")
        ttk.Spinbox(channel_row, from_=1, to=1024, width=6, textvariable=self.channel_number, command=self.read_channel).pack(side="left", padx=4)
        ttk.Button(channel_row, text="读取", command=self.read_channel).pack(side="left", padx=4)
        for title, var, width in (("名称", self.channel_name, 15), ("接收 MHz", self.channel_rx, 13), ("发射 MHz", self.channel_tx, 13)):
            ttk.Label(channel_row, text=title).pack(side="left", padx=(7, 2))
            ttk.Entry(channel_row, textvariable=var, width=width).pack(side="left")
        ttk.Button(channel_row, text="写入并核验", command=self.write_channel).pack(side="left", padx=7)
        tone_row = ttk.Frame(channel)
        tone_row.pack(fill="x", pady=(8, 0))
        choices = server.channel_tone_choices()
        self.channel_tone_values = {x['label']: x['value'] for x in choices}
        self.channel_tone_vars = {}
        self.channel_tone_combos = {}
        for key, title in (("rx_tone", "接收亚音"), ("tx_tone", "发射亚音")):
            var = tk.StringVar(value="关闭")
            self.channel_tone_vars[key] = var
            ttk.Label(tone_row, text=title).pack(side="left", padx=(0, 5))
            combo = ttk.Combobox(tone_row, textvariable=var, width=27,
                                 values=list(self.channel_tone_values), state="disabled")
            combo.pack(side="left", padx=(0, 18))
            self.channel_tone_combos[key] = combo
        self.channel_status = ttk.Label(outer, text="选择信道号并读取，其他参数会保留")
        self.channel_status.pack(anchor="w")

        viewer = ttk.LabelFrame(outer, text="K5Viewer 实时画面", padding=10)
        viewer.pack(fill="both", expand=True, pady=5)
        self.image = tk.PhotoImage(width=128, height=64)
        self.image.put("#35492c", to=(0, 0, 128, 64))
        self.scaled = self.image.zoom(4, 4)
        self.lcd = ttk.Label(viewer, image=self.scaled)
        self.lcd.pack(pady=4)
        self.lcd_age = ttk.Label(viewer, text="等待电台画面")
        self.lcd_age.pack()

        logs = ttk.LabelFrame(outer, text="状态 / 最近操作", padding=8)
        logs.pack(fill="x", pady=5)
        self.log = tk.Text(logs, height=4, state="disabled", wrap="word")
        self.log.pack(fill="x")

    def copy_phone_url(self) -> None:
        if not self.phone_address_var.get():
            self.events.put((False, "未找到局域网地址；请检查电脑网络连接"))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.phone_address_var.get())
        self.events.put((True, "手机地址已复制"))

    def refresh_ports(self) -> None:
        ports = list(server.list_ports.comports())
        values = [f"{p.device}  —  {p.description}" for p in ports]
        previous = self.port.get()
        self.port["values"] = values
        if previous in values:
            self.port.set(previous)
        else:
            self.port.set("")

    def audio_device_selectors(self):
        return self.audio_device, self.tx_output, self.headphone_device, self.microphone_device

    def apply_audio_device_lock(self):
        state = "disabled" if self.audio_device_locked.get() else "readonly"
        for selector in self.audio_device_selectors():
            selector.configure(state=state)

    @staticmethod
    def scroll_audio_selector(event, viewport):
        if event.delta:
            steps = max(1, abs(event.delta) // 120)
            viewport.yview_scroll(-steps if event.delta > 0 else steps, "units")
        return "break"

    def refresh_audio(self) -> None:
        try:
            devices = server.audio.devices()
        except Exception as exc:
            self.events.put((False, f"列出音频设备失败：{exc}"))
            return
        values = [f"{d['id']}  —  {d['name']} · {d['api']} · {d['rate']} Hz" for d in devices]
        previous = self.audio_device.get()
        self.audio_device["values"] = values
        if previous in values:
            self.audio_device.set(previous)
        elif values:
            aioc = next((i for i, item in enumerate(values) if "AIOC" in item.upper()), None)
            if aioc is not None:
                self.audio_device.current(aioc)
            else:
                self.audio_device.set("")
        else:
            self.audio_device.set("")
        outputs = server.audio.output_devices()
        output_values = [f"{d['id']}  —  {d['name']} · {d['api']} · {d['rate']} Hz" for d in outputs]
        previous_output = self.tx_output.get()
        self.tx_output["values"] = output_values
        self.tx_output.set(previous_output if previous_output in output_values else "")
        for combo, items, key in ((self.headphone_device, outputs, 'desktop_headphones'),
                                  (self.microphone_device, devices, 'desktop_microphone')):
            previous = combo.get()
            candidates = [d for d in items if 'AIOC' not in d['name'].upper()]
            labels = [f"{d['id']}  —  {d['name']} · {d['api']} · {d['rate']} Hz" for d in candidates]
            combo['values'] = labels
            saved = server.SETTINGS.get(key, {})
            match = next((label for d, label in zip(candidates, labels)
                          if d['name'] == saved.get('name') and d['api'] == saved.get('api')), '')
            combo.set(previous if previous in labels else match)

    def _desktop_device(self, combo, direction):
        if not combo.get():
            raise ValueError('请先选择电脑耳机输出和话筒输入（不要选择 AIOC）')
        device = int(combo.get().split('  —  ', 1)[0])
        items = server.audio.devices() if direction == 'input' else server.audio.output_devices()
        selected = next((d for d in items if d['id'] == device), None)
        if selected is None or combo.get() != f"{selected['id']}  —  {selected['name']} · {selected['api']} · {selected['rate']} Hz":
            raise ValueError('音频设备已变化，请刷新后重新选择')
        return selected

    def start_headphones(self):
        try:
            device = self._desktop_device(self.headphone_device, 'output')
            volume = int(self.headphone_volume.get())
            if not 0 <= volume <= 100:
                raise ValueError('收听音量须为 0–100')
            server.save_settings({'desktop_headphones': device, 'desktop_listen_volume': volume})
            self._work(self.headphones.start, device['id'], volume)
        except ValueError as exc:
            messagebox.showwarning('耳机收听', str(exc))

    def press_desktop_ptt(self, event=None):
        if self.desktop_held or self.desktop_mic.busy:
            return 'break'
        try:
            if not server.link._aioc:
                raise ValueError('电脑话筒发射需要连接 AIOC，并选择 AIOC DTR 和发射输出')
            device = self._desktop_device(self.microphone_device, 'input')
            gain = int(self.microphone_gain.get())
            if not 1 <= gain <= 300:
                raise ValueError('话筒增益须为 1–300')
            server.save_settings({'desktop_microphone': device, 'desktop_mic_gain': gain})
            self.desktop_held = True
            self.desktop_mic.start(device['id'], gain)
        except ValueError as exc:
            self.desktop_mic.state = str(exc)
            self.desktop_mic_status.configure(text=str(exc))
        return 'break'

    def release_desktop_ptt(self, event=None):
        self.desktop_held = False
        self.desktop_mic.stop()
        return 'break' if event is not None and event.type == tk.EventType.KeyRelease else None

    def _desktop_focus_check(self):
        if self.root.focus_displayof() is None:
            self.release_desktop_ptt()

    def _desktop_tick(self):
        self.desktop_mic_meter['value'] = self.desktop_mic.level
        self.desktop_mic_status.configure(text=f'{self.desktop_mic.state} · 话筒电平 {self.desktop_mic.level}%')
        self.desktop_ptt.configure(text='松开发射停止' if self.desktop_held else '按住 PTT 讲话')
        try:
            self.headphones.gain = max(0, min(100, int(self.headphone_volume.get())))
        except ValueError:
            pass
        if self.headphones.thread is not None and self.headphones.thread.is_alive():
            text = '发射期间暂停耳机回放' if server.link._ptt_lease else '正在通过电脑耳机收听'
        else:
            text = '耳机收听未开启'
        self.headphone_status.configure(text=self.headphones.error or text)
        self.root.after(100, self._desktop_tick)

    def _sync_settings(self) -> None:
        saved = server.settings_json()
        self.last_settings_revision = saved["revision"]
        self.connection_mode.set(saved["mode"])
        self.ptt_mode.set({"auto": "自动识别", "aioc": "AIOC DTR", "firmware": "固件串口命令"}.get(saved["ptt_mode"], "自动识别"))
        self.baud.set("38400")
        self.tcp_host.set(saved["tcp_host"])
        self.tcp_port.set(str(saved["tcp_port"]))
        port_value = next((item for item in self.port["values"] if item.split("  —  ", 1)[0] == saved["port"]), "")
        self.port.set(port_value)
        audio_value = next((item for item in self.audio_device["values"] if item.split("  —  ", 1)[0] == str(saved["audio_device"])), "")
        self.audio_device.set(audio_value)
        tx_value = next((item for item in self.tx_output["values"] if item.split("  —  ", 1)[0] == str(saved["tx_output_device"])), "")
        self.tx_output.set(tx_value)
        self.tx_gain.set(str(saved["tx_gain"]))

    def _work(self, fn, *args) -> None:
        def run():
            try:
                fn(*args)
                self.events.put((True, "操作完成"))
            except Exception as exc:
                self.events.put((False, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def connect_radio(self) -> None:
        try:
            mode = self.connection_mode.get()
            selection = self.port.get()
            if mode == "serial" and not selection:
                raise ValueError("请先连接 K头写频线或 K1 USB-C 数据线，刷新后选择对应 COM 口")
            self._work(server.connect_configured_radio, mode, selection.split("  —  ", 1)[0],
                       int(self.baud.get()), self.tcp_host.get(), int(self.tcp_port.get()), None,
                       {"自动识别": "auto", "AIOC DTR": "aioc", "固件串口命令": "firmware"}[self.ptt_mode.get()])
        except ValueError as exc:
            messagebox.showwarning("连接设置无效", str(exc))

    def start_audio(self) -> None:
        selection = self.audio_device.get()
        if not selection:
            messagebox.showwarning("请选择音频设备", "请连接 AIOC 后刷新并选择录音设备。")
            return
        self._work(server.start_configured_audio, int(selection.split("  —  ", 1)[0]))

    def save_tx_output(self) -> None:
        selection = self.tx_output.get()
        if not selection:
            messagebox.showwarning("请选择输出设备", "请连接 AIOC 后刷新并选择其播放设备。")
            return
        try:
            gain = int(self.tx_gain.get())
        except ValueError:
            messagebox.showwarning("音量无效", "发射输出音量须为 1–100%")
            return
        self._work(server.set_tx_output, int(selection.split("  —  ", 1)[0]), None, gain)

    def set_frequency(self, kind: str) -> None:
        try:
            mhz = float((self.rx if kind == "rx" else self.tx).get())
            payload = server.frequency_payload(mhz)
        except (ValueError, OverflowError) as exc:
            messagebox.showerror("频率无效", str(exc))
            return

        def send():
            server.link.send(0x8888 if kind == "rx" else 0x7777, payload, f"设置{kind.upper()}频率 {mhz:.6f} MHz")
            setattr(server.link.state, "rx_mhz" if kind == "rx" else "tx_mhz", mhz)
        self._work(send)

    def set_monitor(self, enabled: bool) -> None:
        def send():
            server.link.send(0x5555, b"Y" if enabled else b"N", "监听开" if enabled else "监听关")
            server.link.state.monitor = enabled
        self._work(send)

    def read_channel(self) -> None:
        self.loaded_channel = None
        for combo in self.channel_tone_combos.values():
            combo.configure(state="disabled")
        try:
            number = server.channel_number(self.channel_number.get())
        except ValueError as exc:
            self.channel_status.configure(text=str(exc))
            return
        self.channel_status.configure(text=f"正在读取信道 {number}…")
        def run():
            try:
                self.channel_events.put((number, server.read_channel(number)))
            except Exception as exc:
                self.channel_events.put((number, exc))
        threading.Thread(target=run, daemon=True).start()

    def write_channel(self) -> None:
        try:
            number = server.channel_number(self.channel_number.get())
            if not self.loaded_channel or self.loaded_channel["number"] != number:
                raise ValueError("请先读取当前信道")
            data = {"etag": self.loaded_channel["etag"], "name": self.channel_name.get(),
                    "rx_mhz": self.channel_rx.get(), "tx_mhz": self.channel_tx.get(),
                    **{key: self.channel_tone_values[var.get()] for key, var in self.channel_tone_vars.items()}}
        except ValueError as exc:
            self.channel_status.configure(text=str(exc))
            return
        self.loaded_channel = None
        self.channel_status.configure(text=f"正在写入并回读信道 {number}…")
        def run():
            try:
                self.channel_events.put((number, server.write_channel(number, data)))
            except Exception as exc:
                self.channel_events.put((number, exc))
        threading.Thread(target=run, daemon=True).start()

    def _draw_lcd(self, pages: list[str]) -> None:
        snapshot = tuple(pages)
        if snapshot == self.last_lcd or len(pages) != 8 or not pages[0]:
            return
        self.last_lcd = snapshot
        raw = [base64.b64decode(page) for page in pages]
        for y in range(64):
            page = raw[y // 8]
            bit = 1 << (y % 8)
            row = "{" + " ".join("#e7f0dd" if page[x] & bit else "#35492c" for x in range(128)) + "}"
            self.image.put(row, to=(0, y))
        self.scaled = self.image.zoom(4, 4)
        self.lcd.configure(image=self.scaled)

    def _tick(self) -> None:
        if self.last_voice_revision != server.voice_presets.revision:
            index = self.voice_choice.current()
            selected = self.voice_items[index]['id'] if 0 <= index < len(self.voice_items) else None
            snapshot = server.voice_presets.snapshot()
            self.last_voice_revision = snapshot['revision']
            self.voice_items = snapshot['items']
            self.voice_choice['values'] = [x['label'] + ('（生成中）' if x['status'] in ('queued', 'generating') else '（生成失败）' if x['status'] == 'error' else '（未就绪）' if x['status'] != 'ready' else '') for x in self.voice_items]
            if self.voice_items:
                self.voice_choice.current(next((i for i, x in enumerate(self.voice_items) if x['id'] == selected), 0))
            else:
                self.voice_choice.set('')
        voice_state = server.voice_presets.state()
        voice_index = self.voice_choice.current()
        selected_voice = self.voice_items[voice_index] if 0 <= voice_index < len(self.voice_items) else None
        generation_message = '选择语音，点击发送'
        if selected_voice:
            if selected_voice['status'] == 'ready':
                generation_message = f"{'录音已保存' if selected_voice.get('source') == 'recording' else '生成成功'}：{selected_voice['label']}，可试听或发送"
            elif selected_voice['status'] == 'error':
                generation_message = '生成失败：' + selected_voice['error']
            else:
                generation_message = '等待合成' if selected_voice['status'] == 'queued' else '正在生成语音'
        self.voice_send_button.configure(state='disabled' if voice_state['active'] else 'normal')
        self.voice_status.configure(text=(f"正在发射：{voice_state['label']} · {voice_state['progress']}%" if voice_state['active'] else
                                         '发送失败：' + voice_state['error'] if voice_state['error'] else
                                         '发送完成，PTT 已释放' if voice_state['progress'] == 100 else generation_message))
        while not self.voice_edit_events.empty():
            form, ticket, result = self.voice_edit_events.get_nowait()
            if form['window'].winfo_exists():
                form['save'].configure(state='normal')
                if form['ticket'] == ticket:
                    if isinstance(result, Exception):
                        form['status'].configure(text='保存失败：' + str(result))
                    else:
                        form['item'] = result
                        form['status'].configure(text='已保存，正在生成；生成完成后可发送或试听')
        if self.voice_form and self.voice_form['window'].winfo_exists() and self.voice_form['item']:
            form = self.voice_form
            current = next((x for x in server.voice_presets.snapshot()['items'] if x['id'] == form['item']['id']), None)
            if current and current['revision'] == form['item']['revision']:
                if current['status'] == 'ready':
                    form['status'].configure(text=f"保存成功 · {'录音' if current.get('source') == 'recording' else '生成成功'} · {current.get('duration', 0):g} 秒，可试听和发送")
                elif current['status'] == 'error':
                    form['status'].configure(text='生成失败：' + current['error'])
                elif current['status'] in ('queued', 'generating'):
                    form['status'].configure(text=('等待前一条生成' if current['status'] == 'queued' else '正在生成') + f" · 已等待 {current.get('elapsed', 0)} 秒")
        if self.last_settings_revision != server.SETTINGS_REVISION:
            self.refresh_ports()
            self.refresh_audio()
            self._sync_settings()
        while not self.channel_events.empty():
            number, result = self.channel_events.get_nowait()
            if str(number) != self.channel_number.get():
                continue
            if isinstance(result, Exception):
                self.loaded_channel = None
                self.channel_status.configure(text=f"信道操作失败：{result}；请重新读取")
            else:
                self.loaded_channel = result
                self.channel_name.set(result["name"])
                self.channel_rx.set(result["rx_mhz"])
                self.channel_tx.set(result["tx_mhz"])
                for key, var in self.channel_tone_vars.items():
                    label = result[key + '_label']
                    self.channel_tone_values[label] = result[key]
                    var.set(label)
                    self.channel_tone_combos[key].configure(values=list(self.channel_tone_values), state="readonly")
                self.channel_status.configure(text=f"信道 {number} 已读取 / 回读 · {result['mode']} · {result['bandwidth']} · 其他参数保留")
        while not self.events.empty():
            ok, message = self.events.get_nowait()
            if not ok:
                messagebox.showerror("操作失败", message)
            else:
                self.status.configure(text=message)
        state = server.link.state
        radio_health = server.link.health()
        audio_health = server.audio.health()
        radio_text = {"online": "电台已响应 · 画面正常", "waiting": "串口已打开 · 等待电台回应",
                      "silent": "电台画面超时 · 检查连接", "disconnected": "电台未连接",
                      "error": f"连接中断 · {state.last_error or '读取线程已停止'}"}[radio_health]
        audio_text = {"capturing": f"正在采集 · {server.audio.sample_rate} Hz", "starting": "录音设备启动中",
                      "stopped": "音频未采集", "error": f"采集中断 · {server.audio.last_error or '录音设备无响应'}"}[audio_health]
        if radio_health == "waiting" and server.link._aioc and server.link.ptt_link_ready():
            radio_text = ("AIOC 已连接 · 硬件 PTT 可用 · 等待画面"
                          if server.link.tx_output_device is not None else "AIOC 已连接 · 请配置发射输出")
        self.radio_indicator.configure(text="● " + radio_text, fg="#167a45" if radio_health == "online" else "#a66a00" if radio_health == "waiting" else "#b42318")
        self.audio_indicator.configure(text="● " + audio_text, fg="#167a45" if audio_health == "capturing" else "#a66a00" if audio_health == "starting" else "#b42318")
        self.audio_meter.configure(value=server.audio.level if audio_health == "capturing" else 0)
        self.audio_level.configure(text=f"采集输入音量 {server.audio.level}% · {'有输入' if server.audio.level else '暂无输入信号'}" if audio_health == "capturing" else "采集输入音量：未采集")
        tx_recent = bool(server.link._tx_audio_at and time.monotonic() - server.link._tx_audio_at < 1)
        tx_level = server.link._tx_audio_level if tx_recent else 0
        self.tx_audio_meter.configure(value=tx_level)
        tx_stats = server.link._tx_buffer.stats()
        tx_detail = (f"待播放 {tx_stats['tx_buffer_ms']} ms · 断流 {tx_stats['tx_underruns']} 次"
                     if server.link._aioc else "已收到音频块")
        self.tx_audio_level.configure(text=f"发射音频到达：{tx_level}% · {tx_detail}" if tx_recent else "发射音频到达：未收到")
        self.status.configure(text=f"{radio_text} · {audio_text} · 收听端 {len(server.audio.listeners)}")
        self.lcd_age.configure(text="等待电台画面" if state.lcd_age_ms is None else f"画面延迟 {state.lcd_age_ms} ms")
        self._draw_lcd(state.lcd)
        recent = "\n".join(f"{entry['time']} {entry['text']}" for entry in state.logs[-4:])
        if self.log.get("1.0", "end-1c") != recent:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("1.0", recent)
            self.log.configure(state="disabled")
        self.root.after(500, self._tick)

    def close(self) -> None:
        self.release_desktop_ptt()
        self.headphones.stop()
        server.voice_presets.stop()
        server.audio.stop()
        server.link.close()
        self.httpd.shutdown()
        self.root.destroy()

    def send_voice(self) -> None:
        index = self.voice_choice.current()
        if not 0 <= index < len(self.voice_items):
            messagebox.showinfo('预制语音', '请先添加语音并选择一条内容。')
            return
        try:
            import winsound
            winsound.PlaySound(None, 0)
            server.voice_presets.send(self.voice_items[index]['id'])
        except Exception as exc:
            messagebox.showerror('无法发送', str(exc))

    def edit_voice(self) -> None:
        if self.voice_form and self.voice_form['window'].winfo_exists():
            self.voice_form['window'].lift()
            return
        try:
            voices = server.voice_presets.get_voices()
        except Exception as exc:
            messagebox.showerror('语音合成不可用', str(exc))
            return
        window = tk.Toplevel(self.root)
        window.title('预制语音 · 添加、编辑与删除')
        window.geometry('620x510')
        window.transient(self.root)
        voice_labels = {v['name']: v.get('label', v['name']) for v in voices}
        voice_names = {value: key for key, value in voice_labels.items()}
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill='both', expand=True)
        label, voice, rate = tk.StringVar(), tk.StringVar(), tk.StringVar(value='0')
        ttk.Label(panel, text='下拉菜单中的文字描述').pack(anchor='w')
        ttk.Entry(panel, textvariable=label).pack(fill='x', pady=(4, 10))
        ttk.Label(panel, text='要朗读的正文（最多 500 字 / 60 秒）').pack(anchor='w')
        text = tk.Text(panel, height=7, wrap='word', font=('Microsoft YaHei UI', 11))
        text.pack(fill='both', expand=True, pady=4)
        ttk.Label(panel, text='合成声音').pack(anchor='w')
        ttk.Combobox(panel, state='readonly', textvariable=voice, values=list(voice_labels.values())).pack(fill='x', pady=4)
        ttk.Label(panel, text='云希为联网中文男声：生成时将正文发送微软，音频缓存在本机。', wraplength=570).pack(anchor='w')
        line = ttk.Frame(panel)
        line.pack(fill='x', pady=6)
        ttk.Label(line, text='语速（0 正常，-3 慢，3 快）').pack(side='left')
        ttk.Spinbox(line, from_=-3, to=3, textvariable=rate, width=5).pack(side='left', padx=8)
        actions = ttk.Frame(panel)
        actions.pack(fill='x', pady=8)
        status = ttk.Label(panel, text='保存时自动合成；可先试听，再通过主窗口发送', wraplength=570)
        status.pack(anchor='w')
        form = {'window': window, 'item': None, 'ticket': 0, 'status': status}
        self.voice_form = form

        def new():
            form['ticket'] += 1
            form['item'] = None
            label.set('')
            text.configure(state='normal')
            text.delete('1.0', 'end')
            preferred = next((v['name'] for v in voices if v['culture'] == 'zh-CN' and v.get('gender') == 'Male'), None)
            selected = preferred or next((v['name'] for v in voices if v['culture'] == 'zh-CN'), voices[0]['name'] if voices else '')
            voice.set(voice_labels.get(selected, selected))
            rate.set('0')
            status.configure(text='输入描述和正文后保存')

        def save():
            try:
                data = {'label': label.get(), 'text': text.get('1.0', 'end-1c'), 'voice': voice_names.get(voice.get(), voice.get()), 'rate': int(rate.get())}
                if form['item']:
                    data.update(id=form['item']['id'], revision=form['item']['revision'])
                    data['source'] = form['item'].get('source', 'tts')
            except ValueError:
                status.configure(text='语速请输入 -3 至 3 的整数')
                return
            form['save'].configure(state='disabled')
            ticket = form['ticket']
            def work():
                try:
                    result = server.voice_presets.save(data)
                except Exception as exc:
                    result = exc
                self.voice_edit_events.put((form, ticket, result))
            threading.Thread(target=work, daemon=True).start()

        def delete():
            if not form['item']:
                return
            try:
                server.voice_presets.delete(form['item']['id'], form['item']['revision'])
                new()
                status.configure(text='已删除')
            except Exception as exc:
                status.configure(text=str(exc))

        def preview():
            try:
                if not form['item']:
                    raise ValueError('请先保存并生成语音')
                if server.link._ptt_lease:
                    raise ValueError('请先松开 PTT 再试听')
                import winsound
                winsound.PlaySound(str(server.voice_presets.ready_path(form['item']['id'])), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception as exc:
                status.configure(text=str(exc))

        ttk.Button(actions, text='添加新条目', command=new).pack(side='left', padx=3)
        form['save'] = ttk.Button(actions, text='保存并生成', command=save)
        form['save'].pack(side='left', padx=3)
        ttk.Button(actions, text='本机试听', command=preview).pack(side='left', padx=3)
        ttk.Button(actions, text='删除此条', command=delete).pack(side='left', padx=3)
        new()
        index = self.voice_choice.current()
        if 0 <= index < len(self.voice_items):
            item = self.voice_items[index]
            form['item'] = item.copy()
            label.set(item['label'])
            text.insert('1.0', item['text'])
            voice.set(voice_labels.get(item['voice'], item['voice']))
            rate.set(str(item['rate']))
            if item.get('source') == 'recording':
                text.insert('1.0', '这是手机录音条目。可修改上方文字描述；重新录音请在手机操作。')
                text.configure(state='disabled')

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    ControlWindow().run()
