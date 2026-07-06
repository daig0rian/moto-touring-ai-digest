#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""
シーン選定結果をもとに4分割映像を生成する

入力: cam-data/segments/scenes_YYYYMMDD.json (selected: true のシーン)
出力: cam-data/output/digest_YYYYMMDD.mp4

レイアウト（1920×1080 出力）:
  ┌──────────────┬──────────────┐
  │  EDR 前方    │  EDR 後方    │
  │  (前カメラ)  │  (後カメラ)  │
  ├──────────────┼──────────────┤
  │ Insta360 前方│ Insta360 後方│
  │  (魚眼補正)  │  (魚眼補正)  │
  └──────────────┴──────────────┘

音声: EDR音声 + Insta360音声（B+COM）を amix でミックス

実行: uv run tools/04_edit_scenes.py
"""

import json, re, shutil, subprocess, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ─── 設定 ──────────────────────────────────────────────────────────
TOUR_DATE       = "20260703"
SCENES_JSON     = PROJECT_ROOT / "cam-data/segments" / f"scenes_{TOUR_DATE}.json"
EDR_DIR         = PROJECT_ROOT / "cam-data/EDR_MotoDR/Normal"
INSV_DIR        = PROJECT_ROOT / "cam-data/DCIM/Camera01"
OUT_DIR         = PROJECT_ROOT / "cam-data/output"
SRT_DIR         = PROJECT_ROOT / "cam-data/transcripts/large_v3"
MAP_FRAMES_BASE = PROJECT_ROOT / "cam-data/map_frames"

FFMPEG          = shutil.which("ffmpeg") or str(Path.home() / "scoop/shims/ffmpeg.exe")

INSTA360_OFFSET_SEC = -439   # Insta360 RTC は GPS 実時刻より 439秒進んでいる: 実時刻 = RTC − 439
EDR_OVERLAP_SEC  = 0.5       # 各セグメント先頭1GOP分が前セグメントと重複（dashcam-joiner 調査済み）
SCENE_MARGIN_SEC = 3         # シーン前後に追加するマージン（秒）
AUDIO_FADE_SEC   = 1.0       # 音声フェードイン/アウト時間（秒）
USE_NVENC        = True      # True: h264_nvenc (GPU・高速) / False: libx264 (CPU・高品質)
OUT_WIDTH       = 1920
OUT_HEIGHT      = 1080
CELL_W          = OUT_WIDTH  // 2   # 960
CELL_H          = OUT_HEIGHT // 2   # 540

# Insta360 魚眼補正パラメータ（v360 フィルタ）
FISHEYE_IN_FOV  = 170       # 魚眼の入力 FOV（度）
RECT_H_FOV      = 120       # 補正後の水平 FOV（度）

JST = timezone(timedelta(hours=9))

# ─── ラウドネス設定 ────────────────────────────────────────────────
# EBU R128 / LUFS 基準。ツーリングごとに好みで調整。
LUFS_TARGET_EDR  = -35.0   # EDR 環境音（バイク走行音・風切り音）: 背景として小さめ
LUFS_TARGET_INSV = -18.0   # Insta360（B+COM インカム会話）: セリフとして明瞭に
LUFS_MEASURE_CAP = 60.0    # 測定に使う最大秒数（統計的に十分・速度とのトレードオフ）

# ─── ラウドネス測定 ────────────────────────────────────────────────
def measure_lufs(path: Path, ss: float, duration: float) -> float | None:
    """
    指定区間の integrated loudness (LUFS) を返す。
    loudnorm の print_format=json を使用し、stderr の JSON から input_i を読む。
    測定失敗・無音時は None。
    """
    cmd = [
        FFMPEG,
        "-ss", f"{ss:.3f}",
        "-t",  f"{min(duration, LUFS_MEASURE_CAP):.3f}",
        "-i",  str(path),
        "-af", "loudnorm=I=-23:LRA=11:TP=-1.5:print_format=json",
        "-f",  "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    m = re.search(r'"input_i"\s*:\s*"([^"]+)"', r.stderr)
    if not m:
        return None
    try:
        val = float(m.group(1))
        return val if val > -90.0 else None   # -inf / 無音は None 扱い
    except ValueError:
        return None

def compute_gain_db(measured: float | None, target: float, label: str) -> float:
    """
    measured → target LUFS に必要なゲイン (dB) を返す。
    ±40 dB にクランプ。測定失敗時は 0.0 dB（変更なし）。
    """
    if measured is None:
        print(f"    {label}: 測定失敗 → 0.0 dB (変更なし)")
        return 0.0
    gain = target - measured
    gain = max(-40.0, min(40.0, gain))
    print(f"    {label}: {measured:.1f} LUFS → 目標 {target:.1f} LUFS  (補正 {gain:+.1f} dB)")
    return gain

# ─── 時刻ユーティリティ ────────────────────────────────────────────
def timestr_to_sec(t: str) -> int:
    h, m, s = map(int, t.split(":"))
    return h * 3600 + m * 60 + s

def sec_to_timestr(s: int) -> str:
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def parse_edr_seg_time(filename: str) -> datetime:
    """F_20260703092024_... → datetime（セグメント開始時刻）"""
    stem  = Path(filename).stem         # F_20260703091953_20260703092024_N
    parts = stem.split("_")             # ['F','20260703091953','20260703092024','N']
    return datetime.strptime(parts[2], "%Y%m%d%H%M%S").replace(tzinfo=JST)

def parse_insv_clip_start(filename: str) -> datetime:
    """VID_20260703_092622_00_009.insv → Insta360 時刻（オフセット前）"""
    stem  = Path(filename).stem         # VID_20260703_092622_00_009
    parts = stem.split("_")             # ['VID','20260703','092622','00','009']
    ds, ts = parts[1], parts[2]
    return datetime(int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
                    int(ts[:2]), int(ts[2:4]), int(ts[4:6]), tzinfo=JST)

# ─── EDR ファイル検索 ─────────────────────────────────────────────
EDR_SEG_DURATION = 30.5   # セグメント1本の長さ（秒）

def find_edr_segments(prefix: str, scene_start_sec: int, scene_end_sec: int,
                      date: str) -> list[tuple[Path, float, float]]:
    """
    指定時間帯をカバーする EDR セグメントを返す。
    戻り値: [(ファイルパス, セグメント内の切り出し開始秒, 切り出し終了秒), ...]
    prefix: 'F' または 'R'
    scene_start_sec / scene_end_sec: 当日の絶対秒（EDR 基準時刻）
    """
    files = sorted(EDR_DIR.glob(f"{prefix}_{date}*.MP4"),
                   key=lambda f: parse_edr_seg_time(f.name))
    result = []
    for f in files:
        seg_t   = parse_edr_seg_time(f.name)
        seg_s   = seg_t.hour * 3600 + seg_t.minute * 60 + seg_t.second
        seg_e   = seg_s + EDR_SEG_DURATION

        # シーン時間帯とオーバーラップするか
        if seg_e <= scene_start_sec or seg_s >= scene_end_sec:
            continue

        cut_in  = max(scene_start_sec - seg_s, 0)
        cut_out = min(scene_end_sec   - seg_s, EDR_SEG_DURATION)
        result.append((f, cut_in, cut_out))
    return result

# ─── Insta360 ファイル検索 ────────────────────────────────────────
def find_insv_file(lens: str, scene_start_sec: int, scene_end_sec: int,
                   date: str) -> tuple[Path | None, float, float]:
    """
    lens: '00'（前方）または '10'（後方）
    scene_start_sec / scene_end_sec: EDR 基準の絶対秒
    Insta360 は 430秒遅れているため、ファイル時刻は (scene_sec - 430) で探す
    """
    insv_start_sec = scene_start_sec - INSTA360_OFFSET_SEC
    insv_end_sec   = scene_end_sec   - INSTA360_OFFSET_SEC

    year  = int(date[:4])
    month = int(date[4:6])
    day   = int(date[6:8])

    files = sorted(INSV_DIR.glob(f"VID_{date}_*_{lens}_*.insv"),
                   key=lambda f: parse_insv_clip_start(f.name))

    best: tuple[Path | None, float, float] = (None, 0.0, 0.0)
    for f in files:
        clip_dt  = parse_insv_clip_start(f.name)
        clip_s   = clip_dt.hour * 3600 + clip_dt.minute * 60 + clip_dt.second

        # クリップの長さを ffprobe で取得（キャッシュなし・都度実行）
        r = subprocess.run(
            [FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "quiet",
             "-print_format", "json", "-show_format", str(f)],
            capture_output=True, text=True
        )
        dur = float(json.loads(r.stdout)["format"]["duration"])
        clip_e = clip_s + dur

        if clip_e <= insv_start_sec or clip_s >= insv_end_sec:
            continue

        cut_in  = max(insv_start_sec - clip_s, 0)
        cut_out = min(insv_end_sec   - clip_s, dur)
        best = (f, cut_in, cut_out)
        break   # Insta360 のクリップは長め（シーンが1ファイルに収まる想定）

    return best

# ─── 地図フレーム concat リスト生成 ──────────────────────────────
def build_map_concat(in_sec: int, out_sec: int, date: str, tmp_dir: Path) -> Path | None:
    """
    [in_sec, out_sec) の各秒に対応する地図フレーム PNG を ffconcat リストとして書き出す。
    GPS フレームが存在しない秒は直前の有効フレームで補完（forward-fill）。
    1フレームも見つからない場合は None を返す（→ color=black フォールバック）。
    """
    map_dir = MAP_FRAMES_BASE / date
    if not map_dir.exists():
        return None

    # forward-fill: 最初に見つかった有効フレームを last_path に記録し、
    # フレームが欠けている秒は直前フレームを使い回す
    first_valid: Path | None = None
    for sec in range(in_sec, out_sec):
        p = map_dir / f"{sec:05d}.png"
        if p.exists():
            first_valid = p
            break
    if first_valid is None:
        return None

    lines = ["ffconcat version 1.0"]
    last_path = first_valid
    for sec in range(in_sec, out_sec):
        p = map_dir / f"{sec:05d}.png"
        if p.exists():
            last_path = p
        lines.append(f"file '{last_path.as_posix()}'")
        lines.append("duration 1")

    concat_path = tmp_dir / "map_concat.txt"
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat_path


# ─── 1シーン分の4分割動画を生成 ──────────────────────────────────
def render_scene(scene: dict, out_path: Path, tmp_dir: Path) -> bool:
    """
    4分割映像を生成する。
    失敗時は False を返す（スキップして処理続行）。

    シーク方針:
      - 単体ファイル (EDR 1セグメント / Insta360): -ss {cut_in} -i {file}
      - 複数 EDR セグメント: concat リストに inpoint/outpoint を書き込み
        -f concat -safe 0 -i {list} で渡す（-ss なし）
    どちらも FFmpeg への入力ストリームは PTS=0 から始まるため、
    filter_complex 内でのトリムは不要。-t {dur} で出力長を制御する。
    """
    in_sec  = max(0, timestr_to_sec(scene["in_time"]) - SCENE_MARGIN_SEC)
    out_sec = timestr_to_sec(scene["out_time"]) + SCENE_MARGIN_SEC
    dur     = out_sec - in_sec
    if dur <= 0:
        print(f"  スキップ: {scene['in_time']}〜{scene['out_time']} (duration=0)")
        return False

    date = TOUR_DATE

    # ── EDR セグメント収集 ────────────────────────────────────────
    edr_f_segs = find_edr_segments("F", in_sec, out_sec, date)
    edr_r_segs = find_edr_segments("R", in_sec, out_sec, date)

    def build_edr_input(segs: list[tuple[Path, float, float]],
                        name: str) -> list[str]:
        """
        EDR セグメント群に対応する FFmpeg インプットオプションを返す。
        単体: [-ss, ci, -i, file]
        複数: concat リスト (inpoint/outpoint 付き) を [-f, concat, -safe, 0, -i, list]
        """
        if len(segs) == 1:
            f, ci, _ = segs[0]
            return ["-ss", f"{ci:.3f}", "-i", str(f)]

        list_path = tmp_dir / f"{name}_concat.txt"
        with open(list_path, "w", encoding="utf-8") as fp:
            for i, (f, ci, co) in enumerate(segs):
                fp.write(f"file '{f.as_posix()}'\n")
                if i == 0:
                    if ci > 0:
                        fp.write(f"inpoint {ci:.3f}\n")
                else:
                    # 先頭1GOP(0.5秒)は前セグメントとの重複 → スキップ
                    inpt = max(ci, EDR_OVERLAP_SEC)
                    fp.write(f"inpoint {inpt:.3f}\n")
                if i == len(segs) - 1:
                    fp.write(f"outpoint {co:.3f}\n")
        return ["-f", "concat", "-safe", "0", "-i", str(list_path)]

    # ── Insta360 ファイル検索 ─────────────────────────────────────
    insv_10_src, insv_10_ci, _ = find_insv_file("10", in_sec, out_sec, date)

    if not edr_f_segs and not insv_10_src:
        print(f"  スキップ: EDR・Insta360ともに見つかりません ({scene['in_time']}〜)")
        return False

    # ── EDR 収録範囲の補正 ────────────────────────────────────────
    # edr_pre_gap:  シーン先頭の EDR 未収録期間（EDR が遅れて起動した場合）
    # edr_post_gap: シーン末尾の EDR 未収録期間（エンジン OFF 後も Insta360 が回っている場合）
    edr_pre_gap = 0.0
    if not edr_f_segs:
        edr_pre_gap = float(dur)
        print(f"  EDR録画なし: Insta360のみで再生 ({dur:.0f}秒)")
    else:
        edr_seg_t = parse_edr_seg_time(edr_f_segs[0][0].name)
        edr_seg_s = edr_seg_t.hour * 3600 + edr_seg_t.minute * 60 + edr_seg_t.second
        if edr_f_segs[0][1] == 0 and edr_seg_s > in_sec:
            edr_pre_gap = float(edr_seg_s - in_sec)
            print(f"  EDR開始補正: 先頭{edr_pre_gap:.0f}秒はEDR未収録 → Insta360のみで再生、"
                  f"EDR合流 {sec_to_timestr(edr_seg_s)}")

    edr_post_gap = 0.0
    if edr_f_segs:
        last_f, _, last_co = edr_f_segs[-1]
        edr_last_seg_t = parse_edr_seg_time(last_f.name)
        edr_last_seg_s = edr_last_seg_t.hour * 3600 + edr_last_seg_t.minute * 60 + edr_last_seg_t.second
        edr_last_end = edr_last_seg_s + last_co
        if edr_last_end < out_sec:
            edr_post_gap = float(out_sec - edr_last_end)
            print(f"  EDR終了補正: 末尾{edr_post_gap:.0f}秒はEDR未収録 → EDRパネルをブラックアウト")

    tpad_parts: list[str] = []
    if edr_pre_gap > 0:
        tpad_parts.append(f"start_duration={edr_pre_gap:.3f}")
    if edr_post_gap > 0:
        tpad_parts.append(f"stop_duration={edr_post_gap:.3f}")

    # ── ffmpeg コマンド構築 ───────────────────────────────────────
    inputs = []
    filter_parts = []
    edr_audio_label:  str | None = None   # EDR 音声ストリームのラベル
    insv_audio_label: str | None = None   # Insta360 音声ストリームのラベル
    vid_idx = 0

    def next_idx() -> int:
        nonlocal vid_idx
        i = vid_idx
        vid_idx += 1
        return i

    # EDR 前方（EDR 録画がない間は黒パネル + adelay で合流）
    if edr_f_segs:
        inputs.extend(build_edr_input(edr_f_segs, "edr_f"))
        edr_f_idx = next_idx()
        sp_f = (
            f"[{edr_f_idx}:v]scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,"
            f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2"
        )
        if tpad_parts:
            filter_parts.append(sp_f + f",tpad={':'.join(tpad_parts)}[vf]")
        else:
            filter_parts.append(sp_f + "[vf]")
        if edr_pre_gap > 0:
            gap_ms = int(edr_pre_gap * 1000)
            filter_parts.append(f"[{edr_f_idx}:a]adelay={gap_ms}:all=1[a_edr_raw]")
            edr_audio_label = "[a_edr_raw]"
        else:
            edr_audio_label = f"[{edr_f_idx}:a]"
    else:
        filter_parts.append(f"color=black:{CELL_W}x{CELL_H}:d={dur}[vf]")

    # EDR 後方
    if edr_r_segs:
        inputs.extend(build_edr_input(edr_r_segs, "edr_r"))
        edr_r_idx = next_idx()
        sp_r = (
            f"[{edr_r_idx}:v]scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,"
            f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2"
        )
        if tpad_parts:
            filter_parts.append(sp_r + f",tpad={':'.join(tpad_parts)}[vr]")
        else:
            filter_parts.append(sp_r + "[vr]")
    else:
        filter_parts.append(f"color=black:{CELL_W}x{CELL_H}:d={dur}[vr]")

    # Insta360 _10_（後方レンズ）→ 左下パネル [v0]
    if insv_10_src:
        inputs.extend(["-ss", f"{insv_10_ci:.3f}", "-i", str(insv_10_src)])
        i10_idx = next_idx()
        filter_parts.append(
            f"[{i10_idx}:v]"
            f"v360=input=fisheye:output=rectilinear"
            f":ih_fov={FISHEYE_IN_FOV}:iv_fov={FISHEYE_IN_FOV}"
            f":h_fov={RECT_H_FOV}:v_fov={int(RECT_H_FOV * CELL_H / CELL_W)},"
            f"scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,"
            f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2[v0]"
        )
        insv_audio_label = f"[{i10_idx}:a]"
    else:
        filter_parts.append(f"color=black:{CELL_W}x{CELL_H}:d={dur}[v0]")

    # 右下パネル [v1]: GPS マップフレーム（なければ black）
    map_concat = build_map_concat(in_sec, out_sec, date, tmp_dir)
    if map_concat:
        inputs.extend(["-f", "concat", "-safe", "0", "-i", str(map_concat)])
        map_idx = next_idx()
        filter_parts.append(
            f"[{map_idx}:v]"
            f"scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,"
            f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2,"
            f"fps=30[v1]"
        )
    else:
        filter_parts.append(f"color=black:{CELL_W}x{CELL_H}:d={dur}[v1]")

    # xstack で4分割合成
    filter_parts.append(
        f"[vf][vr][v0][v1]xstack=inputs=4:"
        f"layout=0_0|{CELL_W}_0|0_{CELL_H}|{CELL_W}_{CELL_H}[vout]"
    )

    # ─ ラウドネス測定 ────────────────────────────────────────────────
    print(f"  ラウドネス測定中...")
    meas_dur = min(float(dur), LUFS_MEASURE_CAP)

    edr_gain_db = compute_gain_db(
        measure_lufs(edr_f_segs[0][0], edr_f_segs[0][1], meas_dur) if edr_f_segs else None,
        LUFS_TARGET_EDR, "EDR音声"
    )
    insv_gain_db = compute_gain_db(
        measure_lufs(insv_10_src, insv_10_ci, meas_dur) if insv_10_src else None,
        LUFS_TARGET_INSV, "Insta360音声"
    )

    # ─ volume 補正 → amix ─────────────────────────────────────────
    norm_labels: list[str] = []

    if edr_audio_label:
        filter_parts.append(f"{edr_audio_label}volume={edr_gain_db:.2f}dB[a_edr_n]")
        norm_labels.append("[a_edr_n]")

    if insv_audio_label:
        filter_parts.append(f"{insv_audio_label}volume={insv_gain_db:.2f}dB[a_ins_n]")
        norm_labels.append("[a_ins_n]")

    n_audio = len(norm_labels)
    if n_audio > 1:
        filter_parts.append(
            "".join(norm_labels) + f"amix=inputs={n_audio}:normalize=0[amixed]"
        )
    elif n_audio == 1:
        filter_parts.append(f"{norm_labels[0]}anull[amixed]")
    else:
        filter_parts.append(f"aevalsrc=0:d={dur}[amixed]")

    fade_out_start = max(dur - AUDIO_FADE_SEC, 0.0)
    filter_parts.append(
        f"[amixed]afade=t=in:st=0:d={AUDIO_FADE_SEC:.1f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={AUDIO_FADE_SEC:.1f}[aout]"
    )

    filter_complex = ";".join(filter_parts)

    cmd = (
        [FFMPEG, "-y", "-loglevel", "error", "-stats"]
        + inputs
        + ["-filter_complex", filter_complex,
           "-map", "[vout]", "-map", "[aout]",
           "-t", str(dur),
           *(
               ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
               if USE_NVENC else
               ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
           ),
           "-c:a", "aac", "-b:a", "192k",
           str(out_path)]
    )

    print(f"  FFmpeg 実行中...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  FFmpeg エラー (returncode={result.returncode})")
        return False

    return True

# ─── 字幕生成・合成 ───────────────────────────────────────────────
def srt_time_to_sec(t: str) -> float:
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

def sec_to_srt_time(sec: float) -> str:
    ms  = int((sec % 1) * 1000)
    s   = int(sec) % 60
    m   = (int(sec) // 60) % 60
    h   = int(sec) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def parse_srt(srt_path: Path) -> list[dict]:
    entries = []
    for block in re.split(r"\n{2,}", srt_path.read_text(encoding="utf-8").strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r"(\S+)\s*-->\s*(\S+)", lines[1])
        if not m:
            continue
        text = "\n".join(lines[2:]).strip()
        if text:
            entries.append({
                "start": srt_time_to_sec(m.group(1)),
                "end":   srt_time_to_sec(m.group(2)),
                "text":  text,
            })
    return entries

def build_output_srt(rendered_scenes: list[dict]) -> str:
    """
    レンダリング済みシーンリストから出力動画用の SRT を生成する。
    各シーンの Insta360 クリップ相対時刻を出力動画の絶対時刻に変換。
    """
    entries = []
    output_offset = 0.0

    for scene in rendered_scenes:
        in_sec  = max(0, timestr_to_sec(scene["in_time"]) - SCENE_MARGIN_SEC)
        out_sec = timestr_to_sec(scene["out_time"]) + SCENE_MARGIN_SEC
        dur     = float(out_sec - in_sec)

        insv_src, insv_ci, _ = find_insv_file("00", in_sec, out_sec, TOUR_DATE)
        if insv_src:
            srt_path = SRT_DIR / f"{insv_src.stem}.srt"
            if srt_path.exists():
                for e in parse_srt(srt_path):
                    # insv_ci〜insv_ci+dur がこのシーンのクリップ内範囲
                    if e["end"] < insv_ci or e["start"] > insv_ci + dur:
                        continue
                    out_s = output_offset + (e["start"] - insv_ci)
                    out_e = output_offset + (e["end"]   - insv_ci)
                    # シーン境界にクランプ
                    out_s = max(output_offset, min(output_offset + dur, out_s))
                    out_e = max(output_offset, min(output_offset + dur, out_e))
                    if out_e > out_s + 0.05:
                        entries.append({"start": out_s, "end": out_e, "text": e["text"]})

        output_offset += dur

    lines = []
    for i, e in enumerate(entries, 1):
        lines.append(str(i))
        lines.append(f"{sec_to_srt_time(e['start'])} --> {sec_to_srt_time(e['end'])}")
        lines.append(e["text"])
        lines.append("")
    return "\n".join(lines)

def mux_subtitles(video_path: Path, srt_path: Path, out_path: Path) -> bool:
    cmd = [
        FFMPEG, "-y", "-loglevel", "error", "-stats",
        "-i", str(video_path),
        "-i", str(srt_path),
        "-c", "copy",
        "-c:s", "mov_text",
        "-metadata:s:s:0", "language=jpn",
        str(out_path),
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"  字幕合成エラー (returncode={result.returncode})")
        return False
    return True

# ─── 全シーン結合 ─────────────────────────────────────────────────
def concat_scenes(scene_files: list[Path], out_path: Path, tmp_dir: Path) -> bool:
    list_path = tmp_dir / "final_concat.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in scene_files:
            f.write(f"file '{p.as_posix()}'\n")

    cmd = [
        FFMPEG, "-y", "-loglevel", "error", "-stats",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy",
        str(out_path)
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"結合エラー (returncode={result.returncode})")
        return False
    return True

# ─── メイン ────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # シーン読み込み
    data   = json.loads(SCENES_JSON.read_text(encoding="utf-8"))
    scenes = [s for s in data["scenes"] if s.get("selected")]
    scenes.sort(key=lambda s: timestr_to_sec(s["in_time"]))

    print(f"選定シーン: {len(scenes)} 件")
    for s in scenes:
        dur = timestr_to_sec(s["out_time"]) - timestr_to_sec(s["in_time"])
        print(f"  [{s['scene_num']:02d}] {s['in_time']}〜{s['out_time']}"
              f" ({dur//60}分{dur%60}秒)  {s.get('location','')}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir     = Path(tmp)
        scene_files     = []
        rendered_scenes = []   # SRT生成用：レンダリング成功シーンのみ

        for i, scene in enumerate(scenes, 1):
            scene_out = tmp_dir / f"scene_{i:03d}.mp4"
            print(f"\n[{i}/{len(scenes)}] シーン {scene['scene_num']}  "
                  f"{scene['in_time']}〜{scene['out_time']}")
            ok = render_scene(scene, scene_out, tmp_dir)
            if ok:
                scene_files.append(scene_out)
                rendered_scenes.append(scene)
                print(f"  → {scene_out.name}")
            else:
                print(f"  → スキップ")

        if not scene_files:
            print("\nエラー: 生成できたシーンがありません")
            return

        final_path = OUT_DIR / f"digest_{TOUR_DATE}.mp4"

        # ── 字幕SRT生成 ────────────────────────────────────────────
        srt_content = build_output_srt(rendered_scenes)
        srt_path    = OUT_DIR / f"digest_{TOUR_DATE}.srt"
        srt_path.write_text(srt_content, encoding="utf-8")
        n_subs = srt_content.count("\n\n")
        print(f"\n字幕: {n_subs} エントリ → {srt_path.name}")

        # ── シーン結合 ─────────────────────────────────────────────
        print(f"\n{len(scene_files)} シーンを結合中...")
        if n_subs > 0:
            raw_path = OUT_DIR / f"digest_{TOUR_DATE}_nosub.mp4"
            ok = concat_scenes(scene_files, raw_path, tmp_dir)
            if not ok:
                print("\n結合失敗")
                return
            print("字幕トラックを合成中...")
            ok = mux_subtitles(raw_path, srt_path, final_path)
            raw_path.unlink(missing_ok=True)
        else:
            ok = concat_scenes(scene_files, final_path, tmp_dir)

        if ok:
            size_mb = final_path.stat().st_size / 1024 / 1024
            print(f"\n完成: {final_path}  ({size_mb:.1f} MB)")
        else:
            print("\n結合失敗")

if __name__ == "__main__":
    main()
