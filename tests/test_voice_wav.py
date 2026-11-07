# -*- encoding: utf-8 -*-
"""语音的 WAV 重采样/裁静音：OlivOS 运行时**没有**标准库 `wave`。

2026-11-04 线上日志证据：16 小时里 41 条
`WAV 音频流优化失败，回退原始音频（可能无声或延迟）: No module named 'wave'`
——也就是每一条语音都走了降级分支：没重采样到 24000Hz、没裁掉 0.6s 头静音。
OlivOS 是打包分发的（`lib/` 里只有 DLLs 和几个第三方包，没有 wave.py），
所以插件自带最小 RIFF 解析/封装，并在这里钉住它的行为。
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_wav_tests_'))

from newGeYi.voice.client import (  # noqa: E402
    _optimize_wav_audio, _read_wav, _write_wav,
)


def _pcm16(samples: list) -> bytes:
    return struct.pack('<%dh' % len(samples), *samples)


def _tone(rate: int, seconds: float, lead_silence: float = 0.6, amp: int = 3000) -> bytes:
    """前 lead_silence 秒静音，之后方波——模拟 GPT-SoVITS 的 0.6s 迟滞。"""
    n = int(rate * seconds)
    lead = int(rate * lead_silence)
    samples = []
    for i in range(n):
        if i < lead:
            samples.append(0)
        else:
            samples.append(amp if (i // 40) % 2 else -amp)
    return _pcm16(samples)


class WavHeaderTest(unittest.TestCase):
    def test_roundtrip(self):
        frames = _pcm16([1, -1, 2, -2])
        data = _write_wav(1, 2, 16000, frames)
        nch, width, rate, back = _read_wav(data)
        self.assertEqual((nch, width, rate), (1, 2, 16000))
        self.assertEqual(back, frames)

    def test_ignores_unknown_chunks(self):
        """GPT-SoVITS / ffmpeg 常在 fmt 前后塞 LIST、fact 之类的块。"""
        frames = _pcm16([5, -5])
        base = _write_wav(1, 2, 8000, frames)
        junk = b'LIST' + struct.pack('<I', 8) + b'INFOxxxx'
        patched = base[:36] + junk + base[36:]
        patched = (patched[:4] + struct.pack('<I', len(patched) - 8) + patched[8:])
        nch, width, rate, back = _read_wav(patched)
        self.assertEqual((nch, width, rate), (1, 2, 8000))
        self.assertEqual(back, frames)

    def test_rejects_non_pcm(self):
        data = (b'RIFF' + struct.pack('<I', 40) + b'WAVE'
                + b'fmt ' + struct.pack('<IHHIIHH', 16, 3, 1, 32000, 128000, 4, 32)
                + b'data' + struct.pack('<I', 4) + b'\x00' * 4)
        with self.assertRaises(ValueError):
            _read_wav(data)


class OptimizeTest(unittest.TestCase):
    def test_resample_to_24k_and_trim_leading_silence(self):
        raw = _write_wav(1, 2, 44100, _tone(44100, 1.0))
        out = _optimize_wav_audio(raw)
        nch, width, rate, frames = _read_wav(out)
        self.assertEqual((nch, width, rate), (1, 2, 24000))
        samples = struct.unpack('<%dh' % (len(frames) // 2), frames)
        # 保留的是 30ms「呼吸头」（24k 下 720 点），之后必须立刻有声音
        self.assertLessEqual(abs(samples[0]), 600, '头部静音没被裁掉')
        self.assertTrue(any(abs(s) >= 600 for s in samples[700:1000]),
                        '30ms 呼吸头之后应该有声音了')
        self.assertLess(len(samples), int(0.7 * 24000), '整段静音都留着了')

    def test_already_24k_mono_is_kept(self):
        raw = _write_wav(1, 2, 24000, _tone(24000, 0.8))
        nch, width, rate, frames = _read_wav(_optimize_wav_audio(raw))
        self.assertEqual((nch, width, rate), (1, 2, 24000))
        self.assertTrue(frames)

    def test_stereo_is_resampled_without_losing_channels(self):
        mono = _tone(32000, 0.6)
        left = struct.unpack('<%dh' % (len(mono) // 2), mono)
        inter = []
        for v in left:
            inter.extend((v, v))
        raw = _write_wav(2, 2, 32000, _pcm16(inter))
        nch, width, rate, _frames = _read_wav(_optimize_wav_audio(raw))
        self.assertEqual((nch, width, rate), (2, 2, 24000))

    def test_garbage_falls_back_to_original(self):
        self.assertEqual(_optimize_wav_audio(b'not a wav at all'), b'not a wav at all')
        self.assertEqual(_optimize_wav_audio(b''), b'')


if __name__ == '__main__':
    unittest.main(verbosity=2)
