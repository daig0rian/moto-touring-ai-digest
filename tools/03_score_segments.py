#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["anthropic"]
# ///
"""
2段階LLMパイプライン:
  Stage 2: AD (haiku, 並列) — チャプターごとに話題分割・文字起こし補正・要約
  Stage 3: Director (sonnet) — 全シーンを採点・選定

実行: uv run tools/03_score_segments.py
出力: cam-data/segments/scenes_YYYYMMDD.json
"""

import csv, json, re, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ─── 設定 ──────────────────────────────────────────────────────────
TOUR_DATE      = "20260703"
SRT_DIR        = PROJECT_ROOT / "cam-data/transcripts/large_v3"
GPS_CSV        = PROJECT_ROOT / "cam-data/gps/tour_20260703.csv"
EDR_DIR        = PROJECT_ROOT / "cam-data/EDR_MotoDR/Normal"
OUT_DIR        = PROJECT_ROOT / "cam-data/segments"

AD_MODEL       = "claude-haiku-4-5-20251001"
DIRECTOR_MODEL = "claude-sonnet-4-5"
AD_WORKERS     = 3
AD_MAX_ENTRIES = 60   # これを超えるチャプターはサブチャプターに分割してADに渡す

INSTA360_OFFSET_SEC   = -439  # Insta360 RTC は GPS 実時刻より 439秒進んでいる: 実時刻 = RTC − 439
SILENCE_BOUNDARY_SEC  = 30
EDR_GAP_BOUNDARY_MIN  = 5
STOP_SPEED_KMH        = 5
STOP_BOUNDARY_MIN     = 15
TARGET_RATIO          = 0.10

# クリップ間のギャップとみなす閾値（これ以上の無発話区間はクリップ境界）
CLIP_GAP_SEC = 3600

JST = timezone(timedelta(hours=9))

# ─── 時刻ユーティリティ ────────────────────────────────────────────
def parse_clip_start(file: Path) -> datetime:
    parts = file.stem.split("_")
    ds, ts = parts[1], parts[2]
    insv = datetime(int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
                    int(ts[:2]), int(ts[2:4]), int(ts[4:6]), tzinfo=JST)
    return insv + timedelta(seconds=INSTA360_OFFSET_SEC)

def srt_ts_to_sec(s: str) -> float:
    h, m, rest = s.split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000

def parse_gps_dt(s: str, date: tuple) -> datetime:
    h, m, sec = map(int, s.split(":"))
    y, mo, d  = date
    return datetime(y, mo, d, h, m, sec, tzinfo=JST)

def timestr_to_sec(t: str) -> int:
    h, m, s = map(int, t.split(":"))
    return h * 3600 + m * 60 + s

def duration_sec(t_in: str, t_out: str) -> int:
    return max(0, timestr_to_sec(t_out) - timestr_to_sec(t_in))

# ─── SRT 読み込み ─────────────────────────────────────────────────
def load_srt(path: Path) -> list[dict]:
    clip_start = parse_clip_start(path)
    entries    = []
    for block in re.split(r'\n{2,}', path.read_text(encoding="utf-8").strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r'(\S+)\s*-->\s*(\S+)', lines[1])
        if not m:
            continue
        s_sec = srt_ts_to_sec(m.group(1))
        e_sec = srt_ts_to_sec(m.group(2))
        body  = " ".join(l.strip() for l in lines[2:] if l.strip())
        if body:
            entries.append({
                "start_sec": s_sec, "end_sec": e_sec,
                "start_abs": clip_start + timedelta(seconds=s_sec),
                "end_abs":   clip_start + timedelta(seconds=e_sec),
                "text":      body,
            })
    return entries

def load_all_srt(srt_dir: Path, date: str) -> list[dict]:
    all_entries = []
    files = sorted(srt_dir.glob(f"VID_{date}_*_00_*.srt"))
    print(f"  SRTファイル: {len(files)} 件")
    for f in files:
        entries = load_srt(f)
        t0 = entries[0]["start_abs"].strftime("%H:%M:%S") if entries else "-"
        t1 = entries[-1]["end_abs"].strftime("%H:%M:%S")  if entries else "-"
        print(f"    {f.name}  → {len(entries)} 発話  ({t0}〜{t1})")
        all_entries.extend(entries)
    all_entries.sort(key=lambda e: e["start_abs"])
    return all_entries

def compute_total_recording_sec(entries: list[dict]) -> float:
    """クリップ間のギャップを除いた実収録時間（秒）を返す"""
    if not entries:
        return 0.0
    total        = 0.0
    cluster_start = entries[0]["start_abs"]
    prev_end      = entries[0]["end_abs"]
    for e in entries[1:]:
        gap = (e["start_abs"] - prev_end).total_seconds()
        if gap > CLIP_GAP_SEC:
            total        += (prev_end - cluster_start).total_seconds()
            cluster_start = e["start_abs"]
        prev_end = e["end_abs"]
    total += (prev_end - cluster_start).total_seconds()
    return total

# ─── GPS 読み込み ─────────────────────────────────────────────────
def load_gps(path: Path, date: tuple) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append({
                "time":      parse_gps_dt(r["timestamp"], date),
                "lat":       float(r["latitude"]),
                "lon":       float(r["longitude"]),
                "speed_kmh": float(r["speed_kmh"]),
            })
    return rows

def annotate_entries_with_gps(entries: list[dict], gps: list[dict]) -> None:
    """各発話に最近傍GPS点の速度を付与"""
    if not gps:
        return
    gi = 0
    for e in entries:
        mid = e["start_abs"] + (e["end_abs"] - e["start_abs"]) / 2
        while gi + 1 < len(gps) and gps[gi + 1]["time"] <= mid:
            gi += 1
        e["speed_kmh"] = gps[gi]["speed_kmh"]

# ─── 境界検出 ─────────────────────────────────────────────────────
def detect_edr_gaps(edr_dir: Path,
                    clip_start: datetime, clip_end: datetime) -> list[dict]:
    date_str  = clip_start.strftime("%Y%m%d")
    seg_times = []
    for f in edr_dir.glob(f"F_{date_str}*.MP4"):
        parts = f.stem.split("_")
        if len(parts) >= 3:
            try:
                t = datetime.strptime(parts[2], "%Y%m%d%H%M%S").replace(tzinfo=JST)
                seg_times.append(t)
            except ValueError:
                continue
    seg_times.sort()

    boundaries = []
    for i in range(1, len(seg_times)):
        gap_min = (seg_times[i] - seg_times[i - 1]).total_seconds() / 60
        if gap_min >= EDR_GAP_BOUNDARY_MIN:
            bt = seg_times[i - 1] + timedelta(seconds=30.5)
            if clip_start <= bt <= clip_end:
                boundaries.append({
                    "time":   bt,
                    "reason": f"EDRファイルギャップ {gap_min:.0f}分（エンジンOFF確実）",
                    "weight": 3,
                })
    return boundaries

def detect_boundaries(entries: list[dict], gps: list[dict],
                      edr_gaps: list[dict] | None = None) -> list[dict]:
    boundaries = list(edr_gaps) if edr_gaps else []

    stop_start = None
    for g in gps:
        if g["speed_kmh"] <= STOP_SPEED_KMH:
            if stop_start is None:
                stop_start = g["time"]
        else:
            if stop_start is not None:
                dur_min = (g["time"] - stop_start).total_seconds() / 60
                if dur_min >= STOP_BOUNDARY_MIN:
                    boundaries.append({
                        "time":   stop_start,
                        "reason": f"長時間停車 {dur_min:.0f}分",
                        "weight": 2,
                    })
                stop_start = None

    for i in range(1, len(entries)):
        gap = (entries[i]["start_abs"] - entries[i-1]["end_abs"]).total_seconds()
        if gap >= SILENCE_BOUNDARY_SEC:
            boundaries.append({
                "time":   entries[i-1]["end_abs"],
                "reason": f"会話空白 {gap:.0f}秒",
                "weight": 1,
            })

    boundaries.sort(key=lambda b: b["time"])
    merged = []
    for b in boundaries:
        if merged and (b["time"] - merged[-1]["time"]).total_seconds() < 60:
            if b["weight"] > merged[-1]["weight"]:
                merged[-1]["reason"] = b["reason"] + " / " + merged[-1]["reason"]
                merged[-1]["weight"] = b["weight"]
            else:
                merged[-1]["reason"] += " / " + b["reason"]
        else:
            merged.append(b)
    return merged

# ─── チャプター構築 ────────────────────────────────────────────────
def build_chapters(entries: list[dict], gps: list[dict],
                   bounds: list[dict]) -> list[dict]:
    if not entries:
        return []
    splits = (
        [{"time": entries[0]["start_abs"],  "reason": "クリップ先頭"}]
        + bounds
        + [{"time": entries[-1]["end_abs"], "reason": "クリップ末尾"}]
    )
    chapters = []
    for i in range(len(splits) - 1):
        t_start    = splits[i]["time"]
        t_end      = splits[i + 1]["time"]
        ch_entries = [e for e in entries if t_start <= e["start_abs"] < t_end]
        ch_gps     = [g for g in gps     if t_start <= g["time"]      < t_end]
        if ch_entries:   # 発話がないチャプターは除外（長大なギャップ区間を避ける）
            pts = ch_gps
            chapters.append({
                "id":         i + 1,
                "start":      t_start,
                "end":        t_end,
                "end_reason": splits[i + 1]["reason"],
                "entries":    ch_entries,
                "gps_pts":    ch_gps,
                "spd_avg":    sum(g["speed_kmh"] for g in pts) / len(pts) if pts else 0,
                "spd_max":    max(g["speed_kmh"] for g in pts) if pts else 0,
            })
    return chapters

# ─── 逆ジオコーディング ────────────────────────────────────────────
_geocache: dict[tuple, str] = {}

def reverse_geocode(lat: float, lon: float) -> str:
    key = (round(lat, 3), round(lon, 3))
    if key in _geocache:
        return _geocache[key]
    try:
        url = (f"https://nominatim.openstreetmap.org/reverse"
               f"?lat={lat}&lon={lon}&format=json&accept-language=ja")
        req = urllib.request.Request(url, headers={"User-Agent": "touring-vlog/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
        addr  = d.get("address", {})
        parts = [
            addr.get("city") or addr.get("town") or addr.get("village") or "",
            addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter") or "",
        ]
        result = " ".join(p for p in parts if p) or d.get("display_name", f"{lat:.4f},{lon:.4f}")
        _geocache[key] = result
        time.sleep(1.1)
        return result
    except Exception:
        return f"{lat:.4f},{lon:.4f}"

def geocode_scene_starts(ad_scenes: list[dict], gps: list[dict],
                         date: tuple) -> None:
    """
    ADが分割した各シーンの in_time に最も近いGPS点をジオコーディングして
    scene["location"] を設定する。
    話題が始まった場所とその話題の内容は強く関連するという前提に基づく。
    """
    if not gps:
        for s in ad_scenes:
            s["location"] = "GPS情報なし"
        return

    y, mo, d = date
    print(f"シーン開始点のジオコーディング: {len(ad_scenes)} シーン...")
    for s in ad_scenes:
        h, m, sec = map(int, s["in_time"].split(":"))
        scene_time = datetime(y, mo, d, h, m, sec, tzinfo=JST)
        nearest    = min(gps, key=lambda g: abs((g["time"] - scene_time).total_seconds()))
        gap_min    = abs((nearest["time"] - scene_time).total_seconds()) / 60
        if gap_min > 30:
            s["location"] = "GPS情報なし"
            print(f"  [{s['scene_num']:02d}] {s['in_time']}  GPS遠すぎ（最近傍 {gap_min:.0f}分）")
        else:
            s["location"] = reverse_geocode(nearest["lat"], nearest["lon"])
            print(f"  [{s['scene_num']:02d}] {s['in_time']}  {s['location'][:40]}")

# ─────────────────────────────────────────────────────────────────
# Stage 2: アシスタントディレクター (haiku, 並列)
# ─────────────────────────────────────────────────────────────────

_AD_SYSTEM = """\
あなたはバイクツーリング映像のアシスタントディレクターです。
走行中のインカム会話（Whisperで文字起こし済み）を1チャプター分受け取り、
以下のタスクを行ってください。

## タスク: 話題セグメントへの分割・要約・補正スクリプト出力
- チャプター内の話題の切れ目を意味的に検出し、セグメントに分割する
- 各セグメントを要約し、見どころ（笑い・驚き・発見・ハプニング）があれば抽出する
- in_time / out_time は会話リストにある発話の実際の時刻（HH:MM:SS）を使うこと
- 場所名が会話から読み取れる場合は location に記入し、不明な場合は null にする
- corrected_lines: セグメント内の各発話を補正してすべて収録する
  - 明らかな誤認識（ノイズ・風切り音・ハルシネーション）と判断した行は除外する
  - 部分的に補正できる場合は正しいと思われるテキストに修正する
  - 判断できない場合はそのまま収録する
  - これは後工程で字幕として使うため、全発話を漏れなく含めること
- transcript_note: 補正・除外した行がある場合にその概要を記録（なければ null）
- story_moment: 以下のいずれかに該当する場合に記録する（該当なければ null）
  - "出発"   — 最初のエンジン始動・出発直後の走り出し
  - "帰着"   — 最終目的地（出発地点）への帰着・エンジンオフ
  - "目的地到着" — 途中の目的地（観光地・食事処・休憩地点など）への到着
  - "目的地離脱" — 途中目的地からの出発
  複数の性質が混在する場合はカンマ区切りで記録する（例: "目的地到着,目的地離脱"）

## 出力形式（JSONのみ、説明文不要）
{
  "segments": [
    {
      "in_time": "HH:MM:SS",
      "out_time": "HH:MM:SS",
      "speed_avg": 数値,
      "location": "場所名（会話から読み取れる場合のみ。不明はnull）",
      "topic": "話題の要約（1〜2文）",
      "highlight": "見どころ・笑い・驚き等（なければ null）",
      "story_moment": "出発 / 帰着 / 目的地到着 / 目的地離脱 / null",
      "transcript_note": "補正・除外した行の概要（なければ null）",
      "corrected_lines": [
        {"t": "HH:MM:SS", "text": "補正後テキスト"},
        ...
      ]
    }
  ]
}\
"""

def format_ad_prompt(chapter: dict) -> str:
    dur = (chapter["end"] - chapter["start"]).total_seconds()
    header = (
        f"[チャプター {chapter['id']:02d}  "
        f"{chapter['start'].strftime('%H:%M:%S')}〜{chapter['end'].strftime('%H:%M:%S')}  "
        f"{dur/60:.1f}分]\n"
        f"走行: 平均{chapter['spd_avg']:.0f}km/h 最高{chapter['spd_max']:.0f}km/h\n\n"
    )

    if not chapter["entries"]:
        return header + "（この区間に発話なし）"

    # GPS速度マーカーを3分おきに挿入（地名なし・速度のみ）
    gps_pts      = chapter["gps_pts"]
    spd_markers  = []
    last_t       = None
    for g in gps_pts:
        if last_t is None or (g["time"] - last_t).total_seconds() >= 3 * 60:
            spd_markers.append((g["time"], g["speed_kmh"]))
            last_t = g["time"]

    lines = []
    mi    = 0
    for e in chapter["entries"]:
        while mi < len(spd_markers) and spd_markers[mi][0] <= e["start_abs"]:
            lines.append(f"  ── [{spd_markers[mi][1]:.0f}km/h] ──")
            mi += 1
        lines.append(f"  {e['start_abs'].strftime('%H:%M:%S')} {e['text']}")

    return header + "\n".join(lines)

def _ad_fallback(chapter: dict, reason: str) -> list[dict]:
    return [{
        "chapter_id":      chapter["id"],
        "in_time":         chapter["start"].strftime("%H:%M:%S"),
        "out_time":        chapter["end"].strftime("%H:%M:%S"),
        "speed_avg":       round(chapter["spd_avg"], 1),
        "location":        None,
        "topic":           "（AD解析失敗 — チャプター全体）",
        "highlight":       None,
        "story_moment":    None,
        "transcript_note": reason,
        "corrected_lines": [],
    }]

def ask_ad(chapter: dict) -> list[dict]:
    client = anthropic.Anthropic()
    resp   = client.messages.create(
        model=AD_MODEL,
        max_tokens=8192,
        system=_AD_SYSTEM,
        messages=[{"role": "user", "content": format_ad_prompt(chapter)}],
    )
    raw = resp.content[0].text.strip()
    m   = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return _ad_fallback(chapter, f"JSONなし: {raw[:80]}")
    try:
        segments = json.loads(m.group()).get("segments", [])
    except json.JSONDecodeError as e:
        return _ad_fallback(chapter, f"JSON解析失敗（出力切断の可能性）: {e}")
    for seg in segments:
        seg["chapter_id"] = chapter["id"]
    return segments

def _split_chapter(chapter: dict) -> list[dict]:
    """
    発話数が AD_MAX_ENTRIES を超えるチャプターをサブチャプターに均等分割する。
    サブチャプターには同じ chapter_id を保持し、sub_id（0始まり）を付与する。
    """
    entries = chapter["entries"]
    if len(entries) <= AD_MAX_ENTRIES:
        return [{**chapter, "sub_id": 0}]

    n_subs     = -(-len(entries) // AD_MAX_ENTRIES)   # ceiling division
    chunk_size = -(-len(entries) // n_subs)
    print(f"  チャプター {chapter['id']:02d}: {len(entries)} 発話 → "
          f"{n_subs} サブチャプター（各最大{chunk_size}発話）に分割")

    subs = []
    for i in range(n_subs):
        chunk = entries[i * chunk_size : (i + 1) * chunk_size]
        if not chunk:
            continue
        t_start = chunk[0]["start_abs"]
        t_end   = chunk[-1]["end_abs"]
        ch_gps  = [g for g in chapter["gps_pts"] if t_start <= g["time"] <= t_end]
        subs.append({
            **chapter,
            "sub_id":  i,
            "start":   t_start,
            "end":     t_end,
            "entries": chunk,
            "gps_pts": ch_gps,
            "spd_avg": (sum(g["speed_kmh"] for g in ch_gps) / len(ch_gps)
                        if ch_gps else chapter["spd_avg"]),
            "spd_max": (max(g["speed_kmh"] for g in ch_gps)
                        if ch_gps else chapter["spd_max"]),
        })
    return subs

def run_ad(chapters: list[dict]) -> list[dict]:
    sub_chapters: list[dict] = []
    for ch in chapters:
        sub_chapters.extend(_split_chapter(ch))

    results: list[tuple[int, int, list[dict]]] = []
    with ThreadPoolExecutor(max_workers=AD_WORKERS) as executor:
        futures = {executor.submit(ask_ad, ch): ch for ch in sub_chapters}
        for future in as_completed(futures):
            ch = futures[future]
            label = f"{ch['id']:02d}-{ch['sub_id']}"
            try:
                segs = future.result()
                results.append((ch["id"], ch["sub_id"], segs))
                print(f"  チャプター {label} AD完了: {len(segs)} セグメント")
            except Exception as e:
                print(f"  チャプター {label} AD失敗: {e}")

    results.sort(key=lambda x: (x[0], x[1]))
    merged = [seg for _, _, segs in results for seg in segs]
    for i, s in enumerate(merged, 1):
        s["scene_num"] = i
    return merged

# ─────────────────────────────────────────────────────────────────
# Stage 3: プロデューサー (sonnet, 1回)
# ─────────────────────────────────────────────────────────────────

_DIRECTOR_SYSTEM = """\
あなたはバイクのグループツーリング映像のディレクターです。
アシスタントディレクターが整理したシーン一覧を受け取り、
YouTubeで限定公開する約30分のダイジェスト動画のシーンを選定してください。

各シーンには要約（topic）・見どころ（highlight）と、
アシスタントディレクターが補正した実際の会話テキスト（corrected_lines）が含まれます。
会話の実際の面白さ・盛り上がりは corrected_lines を読んで判断してください。

## タスク
全シーンにスコア（1〜10）を付け、合計時間が目標に近づくよう「selected: true」を設定する。

## 採点観点（優先度順）
1. 走行の絵になり方（峠・海沿い・夜景・有名スポット・高速道路の爽快感）
2. グループ会話の盛り上がり（笑い・驚き・発見・感動・ハプニング）
3. ツーリングの構造（出発の高揚感・目的地到着・食事・帰路）
4. 速度と場所から想像できる映像的な面白さ

## 必須選定ルール（スコアに関わらず selected: true にすること）
- 旅の始まり（最初のエンジン始動・出発直後のシーン）
- 旅の終わり（帰着・エンジンオフ前後のシーン）
- 目的地への到着（目的地に着いたと読み取れるシーン）
- 目的地からの離脱（目的地を出発したと読み取れるシーン）
これらはストーリーの骨格であり、どれが欠けても旅の流れが伝わらなくなる。

## 出力形式（JSONのみ、説明文不要）
{
  "editorial_policy": "この映像全体の編集方針（2〜3文）",
  "scored_scenes": [
    {
      "scene_num": 1,
      "score": 1〜10,
      "reason": "評価理由（1文）",
      "selected": true または false
    }
  ]
}\
"""

def format_director_prompt(ad_scenes: list[dict], total_rec_sec: float) -> str:
    target_min = total_rec_sec * TARGET_RATIO / 60
    lines = [
        "## ツーリング全体概要",
        f"実収録時間: {total_rec_sec/60:.0f}分",
        f"目標選定: 約{target_min:.0f}分（全体の{TARGET_RATIO*100:.0f}%）",
        f"シーン数: {len(ad_scenes)}",
        "",
        "## シーン一覧",
    ]
    for s in ad_scenes:
        dur       = duration_sec(s["in_time"], s["out_time"])
        loc_str   = s.get("location") or "（場所不明）"
        highlight = f"\n     ★ {s['highlight']}"              if s.get("highlight")       else ""
        story     = f"\n     【必須】{s['story_moment']}"      if s.get("story_moment")    else ""
        note      = f"\n     ⚠ {s['transcript_note']}"       if s.get("transcript_note") else ""

        cl = s.get("corrected_lines") or []
        if cl:
            dialogue = "\n" + "\n".join(f"     {ln['t']} {ln['text']}" for ln in cl)
        else:
            dialogue = ""

        lines.append(
            f"\n[{s['scene_num']:02d}] {s['in_time']}〜{s['out_time']} "
            f"({dur//60}分{dur%60}秒)  {loc_str}  {s.get('speed_avg', 0):.0f}km/h"
            f"\n     {s['topic']}{story}{highlight}{note}{dialogue}"
        )
    return "\n".join(lines)

def ask_director(ad_scenes: list[dict], total_rec_sec: float) -> dict:
    client = anthropic.Anthropic()
    print(f"Director（{DIRECTOR_MODEL}）に選定依頼中...", flush=True)
    prompt = format_director_prompt(ad_scenes, total_rec_sec)
    resp   = client.messages.create(
        model=DIRECTOR_MODEL,
        max_tokens=4096,
        system=_DIRECTOR_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    m   = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        raise ValueError(f"Director JSONが見つかりません:\n{raw}")
    return json.loads(m.group())

# ─── メイン ────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── データ読み込み ─────────────────────────────────────────────
    print("SRT 読み込み...")
    entries = load_all_srt(SRT_DIR, TOUR_DATE)
    t0, t1  = entries[0]["start_abs"], entries[-1]["end_abs"]
    total_rec_sec = compute_total_recording_sec(entries)
    print(f"  合計: {len(entries)} 発話  "
          f"({t0.strftime('%H:%M:%S')}〜{t1.strftime('%H:%M:%S')})  "
          f"実収録 {total_rec_sec/60:.1f}分")

    clip_date = (t0.year, t0.month, t0.day)
    print(f"\nGPS 読み込み: {GPS_CSV.name}")
    gps = load_gps(GPS_CSV, clip_date)
    print(f"  → {len(gps)} ポイント")
    annotate_entries_with_gps(entries, gps)

    # ── Stage 1: 構造的境界検出 ────────────────────────────────────
    print("\n[Stage 1] 自然境界を検出中...")
    edr_gaps = detect_edr_gaps(EDR_DIR, t0, t1)
    print(f"  EDRギャップ: {len(edr_gaps)} 件")
    bounds = detect_boundaries(entries, gps, edr_gaps=edr_gaps)
    print(f"  → {len(bounds)} 境界")
    for b in bounds:
        print(f"    {b['time'].strftime('%H:%M:%S')}  {b['reason']}")

    chapters = build_chapters(entries, gps, bounds)
    print(f"  → {len(chapters)} チャプター（発話あり）")
    for ch in chapters:
        print(f"    [{ch['id']:02d}] {ch['start'].strftime('%H:%M:%S')}〜"
              f"{ch['end'].strftime('%H:%M:%S')}  {len(ch['entries'])}発話  "
              f"平均{ch['spd_avg']:.0f}km/h")

    # ── Stage 2: AD 並列処理 ───────────────────────────────────────
    print(f"\n[Stage 2] AD（{AD_MODEL}）並列処理中...")
    ad_scenes = run_ad(chapters)
    print(f"  → 合計 {len(ad_scenes)} シーン")

    # AD後にシーン開始点をジオコーディング
    print()
    geocode_scene_starts(ad_scenes, gps, clip_date)

    # AD中間結果を保存
    ad_debug_path = OUT_DIR / "ad_scenes_debug.json"
    ad_debug_path.write_text(
        json.dumps(ad_scenes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  ADシーン保存: {ad_debug_path}")

    # ── Stage 3: Director ─────────────────────────────────────────
    director_prompt = format_director_prompt(ad_scenes, total_rec_sec)
    (OUT_DIR / "director_prompt_debug.txt").write_text(director_prompt, encoding="utf-8")

    print(f"\n[Stage 3] Director（{DIRECTOR_MODEL}）に選定依頼中...")
    result = ask_director(ad_scenes, total_rec_sec)

    # ── 結果マージ & 保存 ─────────────────────────────────────────
    scored_map = {s["scene_num"]: s for s in result.get("scored_scenes", [])}
    for scene in ad_scenes:
        scored = scored_map.get(scene["scene_num"], {})
        scene["score"]    = scored.get("score")
        scene["reason"]   = scored.get("reason")
        scene["selected"] = scored.get("selected", False)

    out_path = OUT_DIR / f"scenes_{TOUR_DATE}.json"
    out_path.write_text(json.dumps({
        "tour_date":        TOUR_DATE,
        "total_rec_min":    round(total_rec_sec / 60, 1),
        "ad_model":         AD_MODEL,
        "dir_model":       DIRECTOR_MODEL,
        "editorial_policy": result.get("editorial_policy", ""),
        "scenes":           ad_scenes,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── コンソール出力 ────────────────────────────────────────────
    print(f"\n── 編集方針 ──────────────────────────────────────────")
    print(result.get("editorial_policy", ""))

    scenes_sorted = sorted(ad_scenes, key=lambda s: s.get("score") or 0, reverse=True)
    selected      = [s for s in scenes_sorted if s.get("selected")]
    total_sel_sec = sum(duration_sec(s["in_time"], s["out_time"]) for s in selected)

    print(f"\n── 全シーン評価 ({len(ad_scenes)}件) ─────────────────────────")
    for s in scenes_sorted:
        mark = "★" if s.get("selected") else "  "
        dur  = duration_sec(s["in_time"], s["out_time"])
        print(f"  {mark} [{s['scene_num']:02d}] score:{s.get('score') or '-':>2}  "
              f"{s['in_time']}〜{s['out_time']} ({dur//60}分{dur%60}秒)  "
              f"{s.get('location', '')}  {s.get('reason', '')}")

    print(f"\n── 選定シーン ({len(selected)}件, 合計{total_sel_sec/60:.1f}分) ──────")
    for s in selected:
        dur = duration_sec(s["in_time"], s["out_time"])
        print(f"  ★ [{s['scene_num']:02d}] {s['in_time']}〜{s['out_time']}"
              f" ({dur//60}分{dur%60}秒)  {s.get('location', '')}  {s.get('reason', '')}")

    print(f"\n結果: {out_path}")

if __name__ == "__main__":
    main()
