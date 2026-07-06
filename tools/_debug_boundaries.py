#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""境界検出のみを確認するデバッグスクリプト（LLM呼び出しなし）"""

import csv, re
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

JST = timezone(timedelta(hours=9))
INSTA360_OFFSET_SEC  = -439  # Insta360 RTC は GPS 実時刻より 439秒進んでいる
SILENCE_BOUNDARY_SEC = 30
GPS_GAP_BOUNDARY_MIN = 5
STOP_SPEED_KMH       = 5
STOP_BOUNDARY_MIN    = 15

SRT_FILE = PROJECT_ROOT / "cam-data/transcripts/large_v3/VID_20260703_092622_00_009.srt"
GPS_CSV  = PROJECT_ROOT / "cam-data/gps/tour_20260703.csv"

def parse_clip_start(file):
    parts = file.stem.split("_")
    ds, ts = parts[1], parts[2]
    insv = datetime(int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
                    int(ts[:2]), int(ts[2:4]), int(ts[4:6]), tzinfo=JST)
    return insv + timedelta(seconds=INSTA360_OFFSET_SEC)

def srt_ts_to_sec(s):
    h, m, rest = s.split(":")
    sec, ms = rest.split(",")
    return int(h)*3600 + int(m)*60 + int(sec) + int(ms)/1000

# SRT読み込み
clip_start = parse_clip_start(SRT_FILE)
entries = []
for block in re.split(r"\n{2,}", SRT_FILE.read_text(encoding="utf-8").strip()):
    lines = block.strip().splitlines()
    if len(lines) < 3:
        continue
    m = re.match(r"(\S+)\s*-->\s*(\S+)", lines[1])
    if not m:
        continue
    s_sec = srt_ts_to_sec(m.group(1))
    e_sec = srt_ts_to_sec(m.group(2))
    body  = " ".join(l.strip() for l in lines[2:] if l.strip())
    if body:
        entries.append({
            "start_abs": clip_start + timedelta(seconds=s_sec),
            "end_abs":   clip_start + timedelta(seconds=e_sec),
            "text":      body,
        })

t0, t1 = entries[0]["start_abs"], entries[-1]["end_abs"]
print(f"発話数: {len(entries)}  {t0.strftime('%H:%M:%S')}〜{t1.strftime('%H:%M:%S')}  ({(t1-t0).total_seconds()/60:.1f}分)")

# GPS読み込み
rows = []
with open(GPS_CSV, newline="", encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        h2, m2, s2 = map(int, r["timestamp"].split(":"))
        rows.append({
            "time":      datetime(t0.year, t0.month, t0.day, h2, m2, s2, tzinfo=JST),
            "speed_kmh": float(r["speed_kmh"]),
        })
margin = timedelta(seconds=30)
gps = [g for g in rows if t0 - margin <= g["time"] <= t1 + margin]
print(f"GPS点数: {len(gps)}  ({gps[0]['time'].strftime('%H:%M:%S')}〜{gps[-1]['time'].strftime('%H:%M:%S')})")

# ── GPS空白（エンジンOFF候補） ──
print("\n=== GPS空白（エンジンOFF候補、5分以上） ===")
print(f"クリップ時間帯のGPS: {gps[0]['time'].strftime('%H:%M:%S')}〜{gps[-1]['time'].strftime('%H:%M:%S')}")
print(f"クリップ末尾:        {t1.strftime('%H:%M:%S')}")
found = False
for i in range(1, len(gps)):
    gap = (gps[i]["time"] - gps[i-1]["time"]).total_seconds() / 60
    if gap >= GPS_GAP_BOUNDARY_MIN:
        print(f"  [内部空白] {gps[i-1]['time'].strftime('%H:%M:%S')} 〜 {gps[i]['time'].strftime('%H:%M:%S')}  空白 {gap:.1f}分")
        found = True
# クリップ途中でGPSが途絶えるケース
if gps:
    remaining = (t1 - gps[-1]["time"]).total_seconds() / 60
    if remaining >= GPS_GAP_BOUNDARY_MIN:
        print(f"  [末尾途絶] GPS終了 {gps[-1]['time'].strftime('%H:%M:%S')} → クリップ終了 {t1.strftime('%H:%M:%S')}  残{remaining:.1f}分 ★エンジンOFF推定")
        found = True
if not found:
    print("  （この時間帯にGPS空白なし → エンジン連続稼働）")

# ── 長時間停車 ──
print(f"\n=== 長時間停車（{STOP_BOUNDARY_MIN}分以上、信号待ち除外） ===")
stop_start = None
found = False
for g in gps:
    if g["speed_kmh"] <= STOP_SPEED_KMH:
        if stop_start is None:
            stop_start = g["time"]
    else:
        if stop_start is not None:
            dur = (g["time"] - stop_start).total_seconds() / 60
            if dur >= STOP_BOUNDARY_MIN:
                print(f"  {stop_start.strftime('%H:%M:%S')}  {dur:.1f}分停車")
                found = True
            stop_start = None
if not found:
    print(f"  （{STOP_BOUNDARY_MIN}分以上の停車なし）")

# ── 会話空白 ──
print(f"\n=== 会話空白（{SILENCE_BOUNDARY_SEC}秒以上） ===")
for i in range(1, len(entries)):
    gap = (entries[i]["start_abs"] - entries[i-1]["end_abs"]).total_seconds()
    if gap >= SILENCE_BOUNDARY_SEC:
        print(f"  {entries[i-1]['end_abs'].strftime('%H:%M:%S')}  空白 {gap:.0f}秒")
        print(f"    前: 「{entries[i-1]['text'][:30]}」")
        print(f"    後: 「{entries[i]['text'][:30]}」")
