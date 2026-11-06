"""Windows and cloud speech synthesis with exclusive, cancellable radio playback."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import threading
import time
import uuid
import wave
import asyncio

ONLINE_VOICE = 'zh-CN-YunxiNeural'
ONLINE_VOICE_INFO = {'name': ONLINE_VOICE, 'label': '云希 · 中文男声（联网）',
                     'culture': 'zh-CN', 'gender': 'Male', 'provider': 'Microsoft Edge'}


def powershell(script: str, data: dict | None = None) -> str:
    prefix = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
    encoded = base64.b64encode((prefix + script).encode('utf-16le')).decode('ascii')
    candidates = [os.environ.get('K1_TTS_POWERSHELL'), shutil.which('pwsh.exe'),
                  str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'PowerShell/7/pwsh.exe'),
                  str(Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe')]
    engine = next((p for p in candidates if p and Path(p).is_file()), 'powershell.exe')
    result = subprocess.run(
        [engine, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        input=json.dumps(data or {}, ensure_ascii=False).encode('utf-8'),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('本机语音合成失败：' + result.stderr.decode('utf-8', errors='replace')[-600:])
    return result.stdout.decode('utf-8-sig').strip()


def installed_voices() -> list[dict]:
    result = powershell("""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { @($s.GetInstalledVoices() | Where-Object Enabled | ForEach-Object {
    @{name=$_.VoiceInfo.Name; culture=$_.VoiceInfo.Culture.Name; gender=$_.VoiceInfo.Gender.ToString()}
}) | ConvertTo-Json -Compress } finally { $s.Dispose() }
""")
    voices = json.loads(result)
    return [ONLINE_VOICE_INFO.copy()] + (voices if isinstance(voices, list) else [voices])


def synthesize_online(text: str, rate: int, path: Path) -> None:
    import edge_tts
    import miniaudio
    media = path.with_suffix('.mp3')
    try:
        async def generate():
            speech = edge_tts.Communicate(text, ONLINE_VOICE, rate=f'{rate * 10:+d}%',
                                          connect_timeout=10, receive_timeout=30)
            await asyncio.wait_for(speech.save(str(media)), timeout=60)
        asyncio.run(generate())
        decoded = miniaudio.decode(media.read_bytes(), output_format=miniaudio.SampleFormat.SIGNED16,
                                  nchannels=1, sample_rate=16000)
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(decoded.samples.tobytes())
    except Exception as exc:
        raise RuntimeError('云希联网合成失败，请检查网络后重试（' + type(exc).__name__ + '）') from exc
    finally:
        media.unlink(missing_ok=True)


def synthesize(text: str, voice: str, rate: int, path: Path) -> None:
    if voice == ONLINE_VOICE:
        synthesize_online(text, rate, path)
        return
    powershell("""
$r = New-Object IO.StreamReader([Console]::OpenStandardInput(), [Text.Encoding]::UTF8)
$d = $r.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $s.SelectVoice([string]$d.voice)
    $s.Rate = [int]$d.rate
    $s.Volume = 100
    $f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $s.SetOutputToWaveFile([string]$d.path, $f)
    $s.Speak([string]$d.text)
} finally { $s.Dispose() }
""", {'text': text, 'voice': voice, 'rate': rate, 'path': str(path)})


class VoicePresets:
    def __init__(self, root: Path, link) -> None:
        self.root = root
        self.link = link
        self.lock = threading.RLock()
        self.synthesis_lock = threading.Lock()
        self.items: list[dict] = []
        self.jobs: dict[str, tuple[str, str]] = {}
        self.job_started: dict[str, float] = {}
        self.voices: list[dict] | None = None
        self.revision = 0
        self.active = False
        self.label = ''
        self.progress = 0
        self.error = ''
        self.lease = None
        self.cancelled = threading.Event()
        try:
            loaded = json.loads((root / 'presets.json').read_text(encoding='utf-8'))
            if not isinstance(loaded, list) or len(loaded) > 100:
                raise ValueError('Invalid preset list')
            self.items = loaded
        except FileNotFoundError:
            pass

    def get_voices(self) -> list[dict]:
        with self.lock:
            if self.voices is not None:
                return self.voices
        voices = installed_voices()
        with self.lock:
            self.voices = voices
        return voices

    @staticmethod
    def signature(item: dict) -> str:
        if item.get('source') == 'recording':
            key = item.get('audio_key', '')
            if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                raise ValueError('录音缓存标识无效')
            return key
        return hashlib.sha256(json.dumps([item['text'], item['voice'], item['rate']],
                                         ensure_ascii=False).encode('utf-8')).hexdigest()

    def audio_path(self, item: dict) -> Path:
        return self.root / (self.signature(item) + '.wav')

    def find(self, item_id: str) -> dict:
        item = next((x for x in self.items if x['id'] == item_id), None)
        if item is None:
            raise ValueError('预制语音不存在，请刷新列表')
        return item

    def persist(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / 'presets.tmp'
        temporary.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.root / 'presets.json')
        self.revision += 1

    def prune_cache(self) -> None:
        referenced = {self.signature(x) for x in self.items}
        for path in self.root.glob('*.wav'):
            if len(path.stem) == 64 and path.stem not in referenced:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # An open local preview may retain a Windows file handle.

    def snapshot(self) -> dict:
        with self.lock:
            items = []
            for item in self.items:
                status, error = self.jobs.get(item['id'], ('missing', ''))
                path = self.audio_path(item)
                ready = path.is_file()
                items.append({**item, 'source': item.get('source', 'tts'),
                              'status': 'ready' if ready else status, 'error': error,
                              'elapsed': round(time.monotonic() - self.job_started.get(item['id'], time.monotonic()))
                              if status in ('queued', 'generating') and not ready else 0})
            return {'items': items, 'revision': self.revision}

    def state(self) -> dict:
        with self.lock:
            return {'active': self.active, 'label': self.label, 'progress': self.progress,
                    'error': self.error}

    def save(self, data: dict) -> dict:
        label = str(data.get('label', '')).strip()
        if data.get('source') == 'recording':
            if not 1 <= len(label) <= 60:
                raise ValueError('描述需 1–60 字')
            with self.lock:
                previous = self.find(str(data.get('id', '')))
                if previous.get('source') != 'recording':
                    raise ValueError('请先录音并保存，再编辑录音描述')
                if data.get('revision') != previous['revision']:
                    raise ValueError('内容已在另一端修改，请重新选择')
                item = {**previous, 'label': label, 'revision': previous['revision'] + 1}
                old = self.items
                self.items = [item if x['id'] == item['id'] else x for x in self.items]
                try:
                    self.persist()
                except Exception:
                    self.items = old
                    raise
                return item.copy()
        text = str(data.get('text', '')).strip()
        voice = str(data.get('voice', ''))
        rate = int(data.get('rate', 0))
        if not 1 <= len(label) <= 60 or not 1 <= len(text) <= 500 or not -3 <= rate <= 3:
            raise ValueError('描述需 1–60 字，正文需 1–500 字，语速需 -3 至 3')
        voices = self.get_voices()
        if not voice:
            default = next((v for v in voices if v['culture'] == 'zh-CN' and v.get('gender') == 'Male'), None)
            if default is None:
                default = next((v for v in voices if v['culture'] == 'zh-CN'), None)
            if default is None:
                raise ValueError('未安装中文合成声音，请选择可用声音')
            voice = default['name']
        if voice not in [v['name'] for v in voices]:
            raise ValueError('请选择已安装的合成声音')
        chosen = next(v for v in voices if v['name'] == voice)
        if any('\u3400' <= character <= '\u9fff' for character in text) and not chosen['culture'].startswith('zh'):
            chinese = next((v['name'] for v in voices if v['culture'].startswith('zh')), None)
            raise ValueError('正文包含中文，当前选择的声音不支持中文。请改选 ' + (chinese or '已安装的中文声音'))
        with self.lock:
            item_id = str(data.get('id', ''))
            previous = self.find(item_id) if item_id else None
            if previous and data.get('revision') != previous['revision']:
                raise ValueError('内容已在另一端修改，请重新选择后编辑')
            if not previous and len(self.items) >= 100:
                raise ValueError('最多保存 100 条预制语音')
            item = {'id': item_id or uuid.uuid4().hex, 'label': label, 'text': text,
                    'voice': voice, 'rate': rate, 'source': 'tts',
                    'revision': previous['revision'] + 1 if previous else 1}
            old = list(self.items)
            self.items = [item if x['id'] == item['id'] else x for x in self.items] if previous else self.items + [item]
            try:
                self.persist()
            except Exception:
                self.items = old
                raise
            self.jobs[item['id']] = ('queued', '')
            self.job_started[item['id']] = time.monotonic()
            self.prune_cache()
            threading.Thread(target=self.generate, args=(item.copy(),), daemon=True).start()
            return item.copy()

    def generate(self, item: dict) -> None:
        path = self.audio_path(item)
        temporary = self.root / (uuid.uuid4().hex + '.wav')
        try:
            with self.synthesis_lock:
                with self.lock:
                    current = next((x for x in self.items if x['id'] == item['id']), None)
                    if not current or self.signature(current) != self.signature(item):
                        return
                    self.jobs[item['id']] = ('generating', '')
                    self.revision += 1
                if not path.is_file():
                    synthesize(item['text'], item['voice'], item['rate'], temporary)
                    with wave.open(str(temporary), 'rb') as audio:
                        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                            raise ValueError('合成音频格式不是 16 kHz 单声道 PCM')
                        duration = audio.getnframes() / 16000
                    if duration < .1:
                        raise ValueError('声音未能朗读正文，没有生成有效音频。中文请使用 Microsoft Huihui Desktop 等中文声音')
                    if duration > 60:
                        raise ValueError('合成语音需在 0.1–60 秒内，请缩短正文')
                    with self.lock:
                        if any(self.signature(x) == self.signature(item) for x in self.items):
                            os.replace(temporary, path)
            with self.lock:
                current = next((x for x in self.items if x['id'] == item['id']), None)
                if current and self.signature(current) == self.signature(item):
                    with wave.open(str(path), 'rb') as audio:
                        current['duration'] = round(audio.getnframes() / audio.getframerate(), 2)
                    self.jobs[item['id']] = ('ready', '')
                    self.persist()
        except Exception as exc:
            with self.lock:
                current = next((x for x in self.items if x['id'] == item['id']), None)
                if current and self.signature(current) == self.signature(item):
                    message = '语音合成超过 90 秒未完成，请重试或缩短正文' if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
                    self.jobs[item['id']] = ('error', message)
                    self.revision += 1
        finally:
            temporary.unlink(missing_ok=True)

    def save_recording(self, label: str, pcm: bytes, item_id: str = '', revision: int | None = None) -> dict:
        label = label.strip()
        if not 1 <= len(label) <= 60:
            raise ValueError('请填写 1–60 字的录音描述')
        if not 3200 <= len(pcm) <= 1920000 or len(pcm) % 2:
            raise ValueError('录音需为 0.1–60 秒的 16 kHz 单声道 PCM')
        key = hashlib.sha256(b'recording\0' + pcm).hexdigest()
        with self.lock:
            previous = self.find(item_id) if item_id else None
            if previous and previous['revision'] != revision:
                raise ValueError('内容已在另一端修改，请重新选择')
            if not previous and len(self.items) >= 100:
                raise ValueError('最多保存 100 条预制语音')
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.root / (uuid.uuid4().hex + '.wav')
            try:
                with wave.open(str(temporary), 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(pcm)
                os.replace(temporary, self.root / (key + '.wav'))
            finally:
                temporary.unlink(missing_ok=True)
            item = {'id': item_id or uuid.uuid4().hex, 'label': label, 'text': '',
                    'voice': '', 'rate': 0, 'source': 'recording', 'audio_key': key,
                    'duration': round(len(pcm) / 32000, 2),
                    'revision': previous['revision'] + 1 if previous else 1}
            old = self.items
            self.items = [item if x['id'] == item['id'] else x for x in self.items] if previous else self.items + [item]
            try:
                self.persist()
            except Exception:
                self.items = old
                raise
            self.jobs[item['id']] = ('ready', '')
            self.prune_cache()
            return item.copy()

    def delete(self, item_id: str, revision: int) -> None:
        with self.lock:
            item = self.find(item_id)
            if revision != item['revision']:
                raise ValueError('内容已在另一端修改，请刷新')
            old = self.items
            self.items = [x for x in self.items if x['id'] != item_id]
            try:
                self.persist()
            except Exception:
                self.items = old
                raise
            self.jobs.pop(item_id, None)
            self.prune_cache()

    def ready_path(self, item_id: str) -> Path:
        with self.lock:
            path = self.audio_path(self.find(item_id))
            if not path.is_file():
                raise ValueError('语音尚未生成，请保存并等待生成完成')
            return path

    def send(self, item_id: str) -> None:
        with self.lock:
            if self.active:
                raise ValueError('已有预制语音正在发送，请先停止')
            if not self.link._aioc:
                raise ValueError('预制语音发送需要连接 AIOC 并使用 AIOC DTR')
            path = self.ready_path(item_id)
            with wave.open(str(path), 'rb') as audio:
                if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                    raise ValueError('音频格式无效，请重新生成')
                pcm = audio.readframes(16000 * 60 + 1)
                if not pcm or len(pcm) > 16000 * 60 * 2:
                    raise ValueError('音频时长无效')
            self.active = True
            self.label = self.find(item_id)['label']
            self.progress = 0
            self.error = ''
            self.lease = None
            self.cancelled = threading.Event()
            threading.Thread(target=self.transmit, args=(pcm, self.cancelled), daemon=True).start()

    def stop(self) -> None:
        self.cancelled.set()
        with self.lock:
            lease = self.lease
        if lease:
            try:
                self.link.ptt('stop', lease)
            except (ValueError, RuntimeError):
                pass

    def transmit(self, pcm: bytes, cancelled: threading.Event) -> None:
        lease = None
        try:
            with self.lock:
                if cancelled.is_set():
                    return
                lease = self.link.ptt('start')
                self.lease = lease
            payload = bytes(6400) + pcm + bytes(6400)
            start = time.monotonic()
            for offset in range(0, len(payload), 3200):
                if cancelled.is_set():
                    break
                self.link.ptt('renew', lease)
                self.link.receive_tx_audio(lease, payload[offset:offset + 3200])
                with self.lock:
                    self.progress = min(99, round((offset + 3200) * 100 / len(payload)))
                if cancelled.wait(max(0, start + (offset + 3200) / 32000 - time.monotonic())):
                    break
            # Let queued audio and the driver tail finish; renew during the drain.
            until = time.monotonic() + min(.4, self.link._tx_buffer.stats()['tx_buffer_ms'] / 1000 + .08)
            while not cancelled.is_set() and time.monotonic() < until:
                self.link.ptt('renew', lease)
                cancelled.wait(.05)
            with self.lock:
                self.progress = 0 if cancelled.is_set() else 100
        except Exception as exc:
            if not cancelled.is_set():
                with self.lock:
                    self.error = str(exc)
        finally:
            if lease:
                try:
                    self.link.ptt('stop', lease)
                except (ValueError, RuntimeError):
                    pass
            with self.lock:
                self.active = False
                self.lease = None
