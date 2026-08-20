"""一次性生成车道 5 句播报音频到 resources/announce/

产出：
  step{N}.wav   — 8kHz mono PCM16，便于试听；运行时也可转 μ-law
  step{N}.ulaw  — G.711 μ-law，预约机 TwoWayAudio 直接推流
"""
from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'resources' / 'announce'

TEXTS = {
    1: '绿通车辆，按键检测',
    2: '申请已接收，请原地等待',
    3: '申请已受理，请等待放行',
    4: '待检车辆，请通行',
    5: '检测中，请勿跟车',
}

# 合成读音修正（业务文案仍用 TEXTS）；「原地」的「地」须读 di，不用 de
TTS_SPEAK = {
    2: '申请已接收，请原底等待',
}


def linear_to_ulaw(sample: int) -> int:
    bias = 0x84
    clip = 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > clip:
        sample = clip
    sample = sample + bias
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        mask >>= 1
        exponent -= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    n = len(pcm) // 2
    out = bytearray(n)
    for i in range(n):
        s = struct.unpack_from('<h', pcm, i * 2)[0]
        out[i] = linear_to_ulaw(s)
    return bytes(out)


def stereo_to_mono(pcm: bytes) -> bytes:
    frames = len(pcm) // 4
    out = bytearray(frames * 2)
    for i in range(frames):
        l = struct.unpack_from('<h', pcm, i * 4)[0]
        r = struct.unpack_from('<h', pcm, i * 4 + 2)[0]
        struct.pack_into('<h', out, i * 2, (l + r) // 2)
    return bytes(out)


def resample(pcm: bytes, src: int, dst: int = 8000) -> bytes:
    if src == dst:
        return pcm
    ns = len(pcm) // 2
    nd = max(1, int(ns * dst / src))
    out = bytearray(nd * 2)
    for i in range(nd):
        pos = i * src / dst
        i0 = int(pos)
        i1 = min(i0 + 1, ns - 1)
        t = pos - i0
        s0 = struct.unpack_from('<h', pcm, i0 * 2)[0]
        s1 = struct.unpack_from('<h', pcm, i1 * 2)[0]
        struct.pack_into('<h', out, i * 2, int(s0 + (s1 - s0) * t))
    return bytes(out)


def tts_pcm8k(text: str) -> bytes:
    fd, wav_path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    try:
        esc = text.replace("'", "''")
        wesc = wav_path.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech\n"
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "$synth.Rate = 0\n"
            "$synth.Volume = 100\n"
            f"$synth.SetOutputToWaveFile('{wesc}')\n"
            f"$synth.Speak('{esc}')\n"
            "$synth.Dispose()\n"
            "Write-Output 'OK'\n"
        )
        p = subprocess.run(
            [
                'powershell.exe',
                '-NoProfile',
                '-NonInteractive',
                '-ExecutionPolicy', 'Bypass',
                '-Command', script,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or '').strip())
        with wave.open(wav_path, 'rb') as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if sw != 2:
            raise RuntimeError('need 16bit wav')
        pcm = frames
        if ch == 2:
            pcm = stereo_to_mono(pcm)
        elif ch != 1:
            raise RuntimeError(f'bad channels {ch}')
        return resample(pcm, rate, 8000)
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def main() -> None:
    import sys

    OUT.mkdir(parents=True, exist_ok=True)
    only = None
    if len(sys.argv) > 1:
        only = int(sys.argv[1])

    for step, text in TEXTS.items():
        if only is not None and step != only:
            continue
        speak = TTS_SPEAK.get(step, text)
        print(f'gen step{step}: {text}' + (f' (读音: {speak})' if speak != text else ''))
        pcm = tts_pcm8k(speak)
        ulaw = pcm16_to_ulaw(pcm)
        wav_out = OUT / f'step{step}.wav'
        ulaw_out = OUT / f'step{step}.ulaw'
        with wave.open(str(wav_out), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(pcm)
        ulaw_out.write_bytes(ulaw)
        # 清理旧 A-law 产物（运行时已改用 μ-law）
        old_alaw = OUT / f'step{step}.alaw'
        if old_alaw.is_file():
            old_alaw.unlink()
        print(f'  -> {wav_out.name} / {ulaw_out.name} (~{len(ulaw) / 8000:.2f}s)')
    print(f'done -> {OUT}')


if __name__ == '__main__':
    main()
