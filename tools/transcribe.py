#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "faster-whisper",
# ]
# ///
"""
Insta360 X3 音声文字起こし
実行: uv run tools/transcribe.py
出力: cam-data/transcripts/ に SRT + TXT
"""

import re, shutil, subprocess, tempfile, time
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ─── 設定 ──────────────────────────────────────────────────────────
INSV_DIR    = PROJECT_ROOT / "cam-data/DCIM/Camera01"
FFMPEG      = shutil.which("ffmpeg") or str(Path.home() / "scoop/shims/ffmpeg.exe")

# 対象クリップ（30秒以上のもの）
MIN_DUR_SEC = 30

# ラウドネス正規化（Whisper精度向上のため）
LUFS_TARGET      = -18.0   # インカム会話音声の目標 LUFS
LUFS_MEASURE_CAP = 60.0    # 測定に使う最大秒数（先頭60秒で代表）

# Whisper設定
WHISPER_MODEL   = "large-v3"   # 汎用最高品質（インカム音声はこちらが優秀）
WHISPER_LANG    = "ja"
WHISPER_DEVICE  = "cuda"       # RTX 5060 GPU
WHISPER_COMPUTE = "float16"    # GPU用

_MODEL_SLUG = WHISPER_MODEL.replace("/", "_").replace("-", "_")
OUT_DIR     = PROJECT_ROOT / "cam-data/transcripts" / _MODEL_SLUG

# ─── ユーティリティ ────────────────────────────────────────────────
def fmt_time(seconds: float) -> str:
    """SRT形式のタイムスタンプ"""
    td = timedelta(seconds=seconds)
    h = int(td.total_seconds() // 3600)
    m = int((td.total_seconds() % 3600) // 60)
    s = td.total_seconds() % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

def get_duration(path: Path) -> float:
    import json
    r = subprocess.run(
        [FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "quiet",
         "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True
    )
    return float(json.loads(r.stdout)["format"]["duration"])

def measure_lufs(path: Path) -> float | None:
    """先頭 LUFS_MEASURE_CAP 秒の integrated loudness を返す。失敗時は None。"""
    cmd = [
        FFMPEG,
        "-t", f"{LUFS_MEASURE_CAP:.1f}",
        "-i", str(path),
        "-vn",
        "-af", "loudnorm=I=-23:LRA=11:TP=-1.5:print_format=json",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    m = re.search(r'"input_i"\s*:\s*"([^"]+)"', r.stderr)
    if not m:
        return None
    try:
        val = float(m.group(1))
        return val if val > -90.0 else None
    except ValueError:
        return None

def extract_audio(insv_path: Path, out_wav: Path, gain_db: float = 0.0):
    """insv から 16kHz モノラル WAV を抽出（gain_db でラウドネス補正）"""
    cmd = [FFMPEG, "-y", "-i", str(insv_path),
           "-vn", "-ar", "16000", "-ac", "1"]
    if abs(gain_db) > 0.1:
        cmd += ["-af", f"volume={gain_db:.2f}dB"]
    cmd += ["-c:a", "pcm_s16le", str(out_wav)]
    subprocess.run(cmd, capture_output=True, check=True)

def transcribe_clip(insv_path: Path, model):
    """1クリップを文字起こしして (segments, info) を返す"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        wav = Path(t.name)
    try:
        print(f"  ラウドネス測定中...")
        measured = measure_lufs(insv_path)
        if measured is not None:
            gain_db = max(-40.0, min(40.0, LUFS_TARGET - measured))
            print(f"    {measured:.1f} LUFS → 目標 {LUFS_TARGET:.1f} LUFS  (補正 {gain_db:+.1f} dB)")
        else:
            gain_db = 0.0
            print(f"    測定失敗 → 補正なし")
        print(f"  音声抽出中...")
        extract_audio(insv_path, wav, gain_db)
        print(f"  Whisper 推論中...")
        segments, info = model.transcribe(
            str(wav),
            language=WHISPER_LANG,
            beam_size=5,
            vad_filter=True,        # 無音区間をスキップ
            vad_parameters={"min_silence_duration_ms": 500},
        )
        # generator を list 化（ここで実際の推論が走る）
        result = []
        for seg in segments:
            result.append(seg)
            print(f"    [{seg.start:6.1f}s] {seg.text.strip()}", flush=True)
        return result, info
    finally:
        wav.unlink(missing_ok=True)

def save_srt(segments, out_path: Path):
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{fmt_time(seg.start)} --> {fmt_time(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")

def save_txt(segments, out_path: Path):
    lines = []
    for seg in segments:
        m = int(seg.start // 60)
        s = int(seg.start % 60)
        lines.append(f"[{m:02d}:{s:02d}] {seg.text.strip()}")
    out_path.write_text("\n".join(lines), encoding="utf-8")

# ─── メイン ────────────────────────────────────────────────────────
def main():
    from faster_whisper import WhisperModel

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 対象クリップを収集
    clips = []
    for insv in sorted(INSV_DIR.glob("VID_20260703_*_00_*.insv")):
        dur = get_duration(insv)
        if dur >= MIN_DUR_SEC:
            clips.append((insv, dur))

    print(f"対象クリップ: {len(clips)} 件")
    for insv, dur in clips:
        m, s = int(dur // 60), int(dur % 60)
        print(f"  {insv.name}  {m}分{s}秒")

    print(f"モデルロード中: {WHISPER_MODEL} ({WHISPER_DEVICE}/{WHISPER_COMPUTE})")
    print("（初回はダウンロードが入ります）")
    try:
        model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    except Exception as e:
        print(f"  GPU失敗: {e}")
        print("  CPUモードで再試行...")
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print("  モデル準備完了")

    for insv, dur in clips:
        stem = insv.stem  # VID_20260703_092622_00_009
        print(f"\n{'='*50}")
        print(f"処理: {insv.name}  ({int(dur//60)}分{int(dur%60)}秒)")
        t0 = time.time()

        segments, info = transcribe_clip(insv, model)

        elapsed = time.time() - t0
        print(f"  完了: {len(segments)} セグメント  ({elapsed:.0f}秒)")

        srt_path = OUT_DIR / f"{stem}.srt"
        txt_path = OUT_DIR / f"{stem}.txt"
        save_srt(segments, srt_path)
        save_txt(segments, txt_path)
        print(f"  → {srt_path.name}")
        print(f"  → {txt_path.name}")


    print(f"\n完了。出力先: {OUT_DIR}")

if __name__ == "__main__":
    main()
