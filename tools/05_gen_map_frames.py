#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "playwright",
# ]
# ///
"""
GPS マップフレームシーケンス生成

Leaflet HTML マップを Playwright でヘッドレス操作し、
1fps の静止画フレームを生成する。

実行:
  uv run tools/05_gen_map_frames.py                            # ツーリング全体
  uv run tools/05_gen_map_frames.py --start 10:30:00 --end 11:00:00  # 範囲指定
  uv run tools/05_gen_map_frames.py 20260703 --start 10:30:00

出力:
  cam-data/map_frames/TOUR_DATE/{SSSSS}.png  (480×270 px、秒通し番号)
  例: 秒=33693 (09:21:33) → 33693.png

Phase 3 で FFmpeg overlay する際の時刻→ファイル名変換:
  sec_of_day = HH*3600 + MM*60 + SS
  filename   = f"{sec_of_day:05d}.png"
"""

import argparse, csv, sys, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ─── 設定 ──────────────────────────────────────────────────────────
TOUR_DATE  = "20260703"
GPS_DIR    = PROJECT_ROOT / "cam-data/gps"
OUT_BASE   = PROJECT_ROOT / "cam-data/map_frames"

MAP_VIEW_W = 480
VIEWPORT_H = 600   # UI 全体が収まる余裕

TILE_WAIT_TIMEOUT_MS = 3000  # 新規タイル読み込みの最大待機時間
TILE_POLL_MS         = 16    # 完了チェック間隔（~1 rAF フレーム）

# MutationObserver で leaflet-tile img の追加を監視し
# load/error 完了まで window._pendingTiles でカウントする。
# L.Evented 内部に触らないため context 問題が起きない。
_TILE_HOOK_JS = """
    window._pendingTiles = 0;
    new MutationObserver(function(mutations) {
        mutations.forEach(function(m) {
            m.addedNodes.forEach(function(node) {
                if (node.tagName === 'IMG' &&
                        node.classList.contains('leaflet-tile')) {
                    window._pendingTiles++;
                    node.addEventListener('load',
                        function() {
                            window._pendingTiles = Math.max(0, window._pendingTiles - 1);
                        }, { once: true });
                    node.addEventListener('error',
                        function() {
                            window._pendingTiles = Math.max(0, window._pendingTiles - 1);
                        }, { once: true });
                }
            });
        });
    }).observe(document.getElementById('map'), { childList: true, subtree: true });
"""

# ─── ユーティリティ ────────────────────────────────────────────────
def timestr_to_sec(t: str) -> int:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)

def sec_to_timestr(sec: int) -> str:
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def load_gps(csv_path: Path) -> dict[int, int]:
    """GPS CSV から {秒通し番号: 配列インデックス} を返す"""
    mapping: dict[int, int] = {}
    with csv_path.open(encoding="utf-8-sig") as f:
        for idx, row in enumerate(csv.DictReader(f)):
            mapping[timestr_to_sec(row["timestamp"])] = idx
    return mapping

# ─── メイン ────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="GPS マップフレーム生成")
    parser.add_argument("tour_date", nargs="?", default=TOUR_DATE,
                        help="ツーリング日付 YYYYMMDD (default: %(default)s)")
    parser.add_argument("--start", default=None, metavar="HH:MM:SS",
                        help="生成開始時刻（省略時: GPSデータ先頭）")
    parser.add_argument("--end",   default=None, metavar="HH:MM:SS",
                        help="生成終了時刻（省略時: GPSデータ末尾）")
    args = parser.parse_args()

    date      = args.tour_date
    csv_path  = GPS_DIR  / f"tour_{date}.csv"
    html_path = GPS_DIR  / f"tour_{date}.html"
    out_dir   = OUT_BASE / date

    if not csv_path.exists():
        sys.exit(f"GPS CSV not found: {csv_path}")
    if not html_path.exists():
        sys.exit(f"Map HTML not found: {html_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    gps_map   = load_gps(csv_path)
    all_secs  = sorted(gps_map)
    start_sec = timestr_to_sec(args.start) if args.start else all_secs[0]
    end_sec   = timestr_to_sec(args.end)   if args.end   else all_secs[-1]

    target_secs = [s for s in all_secs if start_sec <= s <= end_sec]
    pending     = [s for s in target_secs
                   if not (out_dir / f"{s:05d}.png").exists()]

    print(f"範囲: {sec_to_timestr(start_sec)} 〜 {sec_to_timestr(end_sec)}")
    print(f"GPS ポイント数: {len(target_secs)}  未生成: {len(pending)}")
    if not pending:
        print("すべて生成済み。")
        return

    from playwright.sync_api import sync_playwright

    url = html_path.as_uri()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page(
            viewport={"width": MAP_VIEW_W, "height": VIEWPORT_H}
        )

        print(f"マップ読み込み中: {url}")
        page.goto(url, wait_until="networkidle", timeout=30_000)

        page.evaluate("window.scheduleGeocode = function(){}")
        page.evaluate("setAspect('16:9')")
        page.evaluate("setZoom('follow')")
        page.wait_for_timeout(600)  # aspect-ratio transition + invalidateSize

        # タイルロード追跡フックを注入（初期化後に注入して既存カウントを排除）
        page.evaluate(_TILE_HOOK_JS)

        map_el = page.locator("#map-wrap")
        t0 = time.time()

        for n, sec in enumerate(pending):
            # スライダーを更新して MutationObserver にタイル追加を検知させる。
            # MutationObserver は非同期（microtask）なので evaluate 後に
            # 1ms 待ってから _pendingTiles を読む。
            page.evaluate(f"""
                var sl = document.getElementById('slider');
                sl.value = {gps_map[sec]};
                sl.dispatchEvent(new Event('input'));
            """)
            page.wait_for_timeout(1)  # MutationObserver microtask を flush
            new_tiles = page.evaluate("window._pendingTiles")

            # 新規タイルがあるフレームだけ完了を待つ（キャッシュ済みは無待機）
            if new_tiles > 0:
                try:
                    page.wait_for_function(
                        "window._pendingTiles <= 0",
                        timeout=TILE_WAIT_TIMEOUT_MS,
                        polling=TILE_POLL_MS,
                    )
                except Exception:
                    pass  # タイムアウト → グレータイルのままスクショ

            map_el.screenshot(path=str(out_dir / f"{sec:05d}.png"))

            if (n + 1) % 120 == 0 or n == len(pending) - 1:
                elapsed  = time.time() - t0
                fps_rate = (n + 1) / elapsed
                remain   = (len(pending) - n - 1) / fps_rate if fps_rate > 0 else 0
                print(f"  [{n+1:4d}/{len(pending)}] {sec_to_timestr(sec)}"
                      f"  {fps_rate:.1f} fps  残り約{int(remain)}秒")

        browser.close()

    print(f"\n完了。{len(pending)} フレーム → {out_dir}")
    print(f"ファイル命名: sec_of_day → {{sec:05d}}.png")
    print(f"  例) 09:21:33 = {timestr_to_sec('09:21:33'):05d}.png")


if __name__ == "__main__":
    main()
