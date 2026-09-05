"""容器内自测脚本：构造带噪与干净的音频，验证接口行为是否符合预期。

用法（在容器内执行）：
    python -m app.smoke_test [--host http://127.0.0.1:8000]

也支持在宿主机执行（需要已安装 numpy / soundfile / requests）：
    python deployment/app/smoke_test.py --host http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import io
import math
import os
import sys
import tempfile
import time

import numpy as np

def _require_requests():
    try:
        import requests  # type: ignore
    except ImportError:
        print("缺少 requests，请先 pip install requests", file=sys.stderr)
        raise SystemExit(2)
    return requests


def make_speech(seconds: float = 3.0, sr: int = 16000) -> np.ndarray:
    """合成一段带停顿的"语音"：基频 + 谐波 + 音节包络"""
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    sig = np.zeros_like(t)
    # 4 个音节，音节之间有静音间隙，便于噪声检测估计噪声底
    n_syll = 4
    seg = seconds / n_syll
    for i in range(n_syll):
        start = i * seg
        mask = (t >= start + 0.05) & (t < start + seg * 0.6)
        local = t[mask] - start
        env = np.sin(np.pi * np.clip(local / (seg * 0.6), 0, 1)) ** 2
        f0 = 130 + 40 * (i % 3)
        wave = np.sin(2 * np.pi * f0 * local) + 0.5 * np.sin(2 * np.pi * 2 * f0 * local)
        wave += 0.25 * np.sin(2 * np.pi * 3 * f0 * local)
        sig[mask] += (wave * env * 0.35).astype(np.float32)
    return sig


def add_noise(sig: np.ndarray, snr_db: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(sig.shape).astype(np.float32)
    power_sig = float(np.mean(sig**2)) or 1e-9
    power_noise = power_sig / (10 ** (snr_db / 10.0))
    noise *= math.sqrt(power_noise / (float(np.mean(noise**2)) or 1e-9))
    return (sig + noise).astype(np.float32)


def write_wav(path: str, data: np.ndarray, sr: int = 16000) -> None:
    import soundfile as sf

    sf.write(path, data, sr, subtype="PCM_16")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("CV_HOST_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--skip-format-test", action="store_true", help="跳过需要 ffmpeg 的 mp3/m4a 用例")
    args = parser.parse_args()
    requests = _require_requests()
    base = args.host.rstrip("/")

    print(f"[smoke] 目标服务: {base}")
    ok = True

    try:
        resp = requests.get(f"{base}/health", timeout=10)
        print(f"[smoke] /health -> {resp.status_code} {resp.text[:120]}")
        ok &= resp.status_code == 200
    except Exception as exc:
        print(f"[smoke] /health 请求失败: {exc}")
        return 1

    for _ in range(120):
        try:
            r = requests.get(f"{base}/ready", timeout=5)
            if r.status_code == 200:
                print("[smoke] 模型就绪")
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        print("[smoke] 模型长时间未就绪，跳过后续用例")
        return 1

    sr = 16000
    tmpdir = tempfile.mkdtemp(prefix="cv-smoke-")
    noisy = add_noise(make_speech(3.0, sr), snr_db=5.0, seed=1)
    clean = make_speech(3.0, sr)  # 无噪声，预期被自动判定为无需降噪

    cases = [("noisy", noisy, True), ("clean", clean, None)]
    for name, data, expect in cases:
        wav_path = os.path.join(tmpdir, f"{name}.wav")
        write_wav(wav_path, data, sr)
        with open(wav_path, "rb") as fp:
            files = {"file": (os.path.basename(wav_path), fp, "audio/wav")}
            t0 = time.time()
            r = requests.post(f"{base}/api/v1/denoise", files=files, timeout=600)
        cost = time.time() - t0
        applied = r.headers.get("X-Denoise-Applied")
        snr = r.headers.get("X-Noise-Snr-Db")
        print(
            f"[smoke] {name}.wav -> {r.status_code} bytes={len(r.content)} "
            f"applied={applied} snr={snr} 耗时={cost:.2f}s"
        )
        if r.status_code != 200:
            print(f"[smoke]   ! 失败: {r.text[:300]}")
            ok = False
            continue
        if name == "clean" and applied == "true":
            print("[smoke]   ! 干净音频被误判为需要降噪，可调低 denoise.snr_threshold_db")
        if name == "noisy" and applied != "true":
            print("[smoke]   ! 带噪音频未被降噪，可调高 denoise.snr_threshold_db")
            ok = False
        out = os.path.join(tmpdir, f"{name}.out.wav")
        with open(out, "wb") as fp:
            fp.write(r.content)
        try:
            import soundfile as sf

            data_out, sr_out = sf.read(out)
            print(f"[smoke]   输出时长={len(data_out)/sr_out:.2f}s 采样率={sr_out}")
            ok &= sr_out == sr and abs(len(data_out) - len(data)) <= sr * 0.2
        except Exception as exc:
            print(f"[smoke]   ! 读取输出失败: {exc}")
            ok = False

    if not args.skip_format_test:
        for ext in ("mp3", "m4a"):
            src = os.path.join(tmpdir, f"noisy.{ext}")
            try:
                import subprocess

                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", os.path.join(tmpdir, "noisy.wav"),
                     "-c:a", "libmp3lame" if ext == "mp3" else "aac", "-b:a", "96k", src],
                    check=True, capture_output=True, timeout=60,
                )
            except Exception as exc:
                print(f"[smoke] 跳过 {ext} 用例（转码失败）: {exc}")
                continue
            with open(src, "rb") as fp:
                r = requests.post(
                    f"{base}/api/v1/denoise",
                    files={"file": (f"noisy.{ext}", fp, "audio/mpeg" if ext == "mp3" else "audio/mp4")},
                    timeout=600,
                )
            print(f"[smoke] noisy.{ext} -> {r.status_code} bytes={len(r.content)} applied={r.headers.get('X-Denoise-Applied')}")
            if r.status_code != 200:
                print(f"[smoke]   ! 失败: {r.text[:300]}")
                ok = False
            else:
                dst = os.path.join(tmpdir, f"noisy.out.{ext}")
                with open(dst, "wb") as fp:
                    fp.write(r.content)
                try:
                    import subprocess

                    probe = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration",
                         "-of", "default=nw=1", dst],
                        capture_output=True, timeout=30,
                    )
                    print(f"[smoke]   输出格式: {probe.stdout.decode('utf-8', 'ignore').strip()}")
                    ok &= ext in probe.stdout.decode("utf-8", "ignore")
                except Exception as exc:
                    print(f"[smoke]   ! 校验输出格式失败: {exc}")

    try:
        r = requests.get(f"{base}/api/v1/status", timeout=10)
        print(f"[smoke] /api/v1/status -> {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"[smoke]   resource={data.get('resource', {}).get('detail')}")
            print(f"[smoke]   scheduler={data.get('scheduler')}")
    except Exception as exc:
        print(f"[smoke] /api/v1/status 失败: {exc}")

    print("[smoke] 结论:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
