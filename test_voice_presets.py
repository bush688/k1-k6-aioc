import tempfile
import time
import unittest
import wave
import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import voice_presets as module


def fake_synthesis(text, voice, rate, path):
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(16000))  # Half a second, no recorded personal audio.


class FakeLink:
    def __init__(self):
        self._aioc = True
        self.current = None
        self.events = []
        self._tx_buffer = SimpleNamespace(stats=lambda: {'tx_buffer_ms': 0})

    def ptt(self, action, lease=None):
        if action == 'start':
            if self.current:
                raise RuntimeError('PTT 正由另一端使用')
            self.current = 'owned-lease'
        elif lease != self.current:
            raise ValueError('PTT 租约无效')
        elif action == 'stop':
            self.current = None
        self.events.append(action)
        return self.current

    def receive_tx_audio(self, lease, pcm):
        if lease != self.current:
            raise ValueError('wrong lease')
        if not 0 < len(pcm) <= 4096 or len(pcm) % 2:
            raise ValueError('wrong PCM packet')
        self.events.append(('pcm', len(pcm)))


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.link = FakeLink()
        self.manager = module.VoicePresets(Path(self.directory.name), self.link)
        self.manager.voices = [{'name': '中文测试', 'culture': 'zh-CN'}]
        self.synthesis = patch.object(module, 'synthesize', side_effect=fake_synthesis)
        self.synthesis.start()
        self.addCleanup(self.synthesis.stop)

    def wait(self, predicate):
        until = time.monotonic() + 4
        while not predicate():
            if time.monotonic() >= until:
                self.fail('Background work did not finish')
            time.sleep(.01)

    def create(self):
        item = self.manager.save({'label': '测试描述', 'text': '测试正文', 'rate': 0})
        self.wait(lambda: self.manager.snapshot()['items'][0]['status'] == 'ready')
        return item

    def test_create_generate_persist_edit_and_delete(self):
        item = self.create()
        persisted = module.VoicePresets(Path(self.directory.name), self.link)
        self.assertEqual(persisted.snapshot()['items'][0]['text'], '测试正文')
        changed = self.manager.save({**item, 'text': '修改正文'})
        self.wait(lambda: self.manager.snapshot()['items'][0]['status'] == 'ready')
        self.assertEqual(changed['revision'], 2)
        with self.assertRaisesRegex(ValueError, '另一端修改'):
            self.manager.save(item)
        with self.assertRaisesRegex(ValueError, '另一端修改'):
            self.manager.delete(item['id'], 1)
        self.manager.delete(item['id'], 2)
        self.assertEqual(self.manager.snapshot()['items'], [])
        with self.assertRaises(ValueError):
            self.manager.ready_path(item['id'])

    def test_send_finishes_and_releases_own_lease(self):
        item = self.create()
        self.manager.send(item['id'])
        self.wait(lambda: not self.manager.state()['active'])
        self.assertIsNone(self.link.current)
        self.assertEqual(self.link.events[0], 'start')
        self.assertEqual(self.link.events[-1], 'stop')
        self.assertEqual(self.manager.state()['progress'], 100)
        self.assertFalse(self.manager.state()['error'])

    def test_stop_releases_immediately_and_does_not_send_rest(self):
        item = self.create()
        self.manager.send(item['id'])
        self.wait(lambda: self.link.current is not None)
        self.manager.stop()
        self.assertIsNone(self.link.current)
        self.wait(lambda: not self.manager.state()['active'])
        self.assertLess(sum(x[1] for x in self.link.events if isinstance(x, tuple)), 28800)

    def test_manual_ptt_is_never_released_or_taken_over(self):
        item = self.create()
        self.link.current = 'manual'
        self.manager.send(item['id'])
        self.wait(lambda: not self.manager.state()['active'])
        self.assertEqual(self.link.current, 'manual')
        self.assertIn('另一端', self.manager.state()['error'])
        self.assertNotIn('stop', self.link.events)

    def test_output_failure_releases_ptt(self):
        item = self.create()
        with patch.object(self.link, 'receive_tx_audio', side_effect=OSError('USB removed')):
            self.manager.send(item['id'])
            self.wait(lambda: not self.manager.state()['active'])
        self.assertIsNone(self.link.current)
        self.assertIn('USB removed', self.manager.state()['error'])

    def test_invalid_text_voice_and_rate_rejected(self):
        for data in ({'label': '', 'text': 'x'}, {'label': 'x', 'text': 'x' * 501},
                     {'label': 'x', 'text': 'x', 'rate': 4},
                     {'label': 'x', 'text': 'x', 'voice': 'missing'}):
            with self.assertRaises(ValueError):
                self.manager.save(data)

    def test_chinese_text_cannot_select_an_english_only_voice(self):
        self.manager.voices.append({'name': 'English', 'culture': 'en-US'})
        with self.assertRaisesRegex(ValueError, '不支持中文'):
            self.manager.save({'label': '中文', 'text': '这是中文测试', 'voice': 'English'})
        self.assertEqual(self.manager.items, [])

    def test_default_chinese_voice_prefers_male(self):
        self.manager.voices.append({'name': '中文男声', 'culture': 'zh-CN', 'gender': 'Male'})
        item = self.create()
        self.assertEqual(item['voice'], '中文男声')

    def test_cloud_voice_decodes_bytes_and_writes_standard_pcm_in_chinese_path(self):
        path = Path(self.directory.name) / '中文目录' / '语音.wav'
        path.parent.mkdir()
        async def save(destination):
            Path(destination).write_bytes(b'test-mp3')
        samples = array.array('h', [123] * 1600)
        with patch('edge_tts.Communicate', return_value=SimpleNamespace(save=save)) as communicate, \
             patch('miniaudio.decode', return_value=SimpleNamespace(samples=samples)) as decode:
            module.synthesize_online('测试', 2, path)
        self.assertEqual(communicate.call_args.kwargs['rate'], '+20%')
        self.assertEqual(decode.call_args.args[0], b'test-mp3')
        self.assertFalse(path.with_suffix('.mp3').exists())
        with wave.open(str(path), 'rb') as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (16000, 1, 2))
            self.assertEqual(audio.readframes(1600), samples.tobytes())

    def test_cloud_failure_is_reported_without_using_another_voice(self):
        self.synthesis.stop()
        path = Path(self.directory.name) / 'failed.wav'
        async def fail(destination):
            Path(destination).write_bytes(b'partial')
            raise TimeoutError('network')
        with patch('edge_tts.Communicate', return_value=SimpleNamespace(save=fail)), \
             patch.object(module, 'powershell') as local:
            with self.assertRaisesRegex(RuntimeError, '云希联网合成失败'):
                module.synthesize('测试', module.ONLINE_VOICE, 0, path)
            local.assert_not_called()
        self.assertFalse(path.with_suffix('.mp3').exists())

    def test_generation_failure_is_visible_and_not_transmittable(self):
        with patch.object(module, 'synthesize', side_effect=RuntimeError('engine failed')):
            item = self.manager.save({'label': 'x', 'text': 'x'})
            self.wait(lambda: self.manager.snapshot()['items'][0]['status'] == 'error')
        self.assertIn('engine failed', self.manager.snapshot()['items'][0]['error'])
        with self.assertRaises(ValueError):
            self.manager.send(item['id'])

    def test_authenticated_crud_preview_and_send_api(self):
        import server
        with patch.object(server, 'voice_presets', self.manager):
            client = server.app.test_client()
            self.assertEqual(client.get('/api/voice/presets').status_code, 403)
            self.assertEqual(client.post('/api/voice/send', json={}).status_code, 403)
            client.get('/?token=' + server.ACCESS_TOKEN)
            response = client.post('/api/voice/presets', json={'label': 'API描述', 'text': '测试'})
            self.assertEqual(response.status_code, 200)
            item = response.get_json()
            self.wait(lambda: self.manager.snapshot()['items'][0]['status'] == 'ready')
            audio = client.get('/api/voice/presets/' + item['id'] + '/audio')
            self.assertEqual(audio.status_code, 200)
            self.assertEqual(audio.mimetype, 'audio/wav')
            audio.close()
            self.assertEqual(client.post('/api/voice/send', json={'id': item['id']}).status_code, 200)
            self.assertEqual(client.post('/api/voice/stop').status_code, 200)
            self.wait(lambda: not self.manager.state()['active'])
            self.assertEqual(client.delete('/api/voice/presets/' + item['id'], json={'revision': 1}).status_code, 200)

    def test_recording_save_rename_and_restart_do_not_require_tts(self):
        pcm = b'\x10\x00' * 8000
        item = self.manager.save_recording('手机录音', pcm)
        self.assertEqual(item['source'], 'recording')
        self.assertEqual(item['duration'], .5)
        self.assertFalse(self.link.events)
        with wave.open(str(self.manager.ready_path(item['id'])), 'rb') as audio:
            self.assertEqual(audio.readframes(8000), pcm)
        with patch.object(self.manager, 'get_voices', side_effect=AssertionError('TTS must not run')):
            changed = self.manager.save({**item, 'label': '修改描述'})
        self.assertEqual(changed['audio_key'], item['audio_key'])
        self.assertEqual(changed['revision'], 2)
        restored = module.VoicePresets(Path(self.directory.name), self.link)
        self.assertEqual(restored.snapshot()['items'][0]['status'], 'ready')
        self.assertEqual(restored.snapshot()['items'][0]['label'], '修改描述')
        with self.assertRaises(ValueError):
            self.manager.save_recording('冲突', pcm, item['id'], 1)

    def test_recording_validation_and_audio_replacement(self):
        for pcm in (b'', bytes(3201), bytes(1920002)):
            with self.assertRaises(ValueError):
                self.manager.save_recording('测试', pcm)
        with self.assertRaises(ValueError):
            self.manager.save_recording('', bytes(3200))
        item = self.create()
        updated = self.manager.save_recording('改为录音', bytes(6400), item['id'], item['revision'])
        self.assertEqual(updated['source'], 'recording')
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(self.manager.snapshot()['items'][0]['status'], 'ready')

    def test_recording_api_requires_auth_and_preserves_pcm(self):
        import server
        with patch.object(server, 'voice_presets', self.manager):
            client = server.app.test_client()
            self.assertEqual(client.post('/api/voice/recording?label=test', data=bytes(6400)).status_code, 403)
            client.get('/?token=' + server.ACCESS_TOKEN)
            self.assertIn('no-store', client.get('/').headers['Cache-Control'])
            self.assertEqual(client.post('/api/voice/recording?label=test', data=b'xx').status_code, 413)
            response = client.post('/api/voice/recording?label=test', data=bytes(6400), content_type='application/octet-stream')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['source'], 'recording')


if __name__ == '__main__':
    unittest.main()
