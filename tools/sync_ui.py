#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# ///
"""
EDR-22G / Insta360 X3 時刻同期確認UI
実行: uv run tools/sync_ui.py
ブラウザ: http://localhost:8080
"""

import base64, json, os, re, shutil, subprocess, tempfile, threading, webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ─── パス設定 ──────────────────────────────────────────────────────
EDR_DIR    = PROJECT_ROOT / "cam-data/EDR_MotoDR/Normal"
INSV_BASE  = PROJECT_ROOT / "cam-data/DCIM/Camera01"
INSV_FILES = {
    "00": INSV_BASE / "VID_20260703_092622_00_009.insv",
    "10": INSV_BASE / "VID_20260703_092622_10_009.insv",
}
INSV_START = datetime(2026, 7, 3, 9, 26, 22)   # clip009 ファイル名タイムスタンプ（録画開始フレーム）
INSV_DUR   = 1642.7                             # seconds

ANCHORS = [
    datetime(2026, 7, 3, 9, 27, 0),
    datetime(2026, 7, 3, 9, 30, 0),
    datetime(2026, 7, 3, 9, 35, 0),
]

FFMPEG = shutil.which("ffmpeg") or str(Path.home() / "scoop/shims/ffmpeg.exe")

# ─── EDR セグメントインデックス ────────────────────────────────────
def _build_edr_index():
    segs = []
    for f in EDR_DIR.glob("F_20260703091953_*.MP4"):
        m = re.match(r"F_\d{14}_(\d{8})(\d{6})_N\.MP4", f.name)
        if m:
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            segs.append((dt, f))
    return sorted(segs)

EDR_SEGS = _build_edr_index()

def find_edr(target: datetime):
    """対象時刻を含む EDR セグメントとオフセット秒を返す"""
    for start, path in EDR_SEGS:
        if start <= target < start + timedelta(seconds=31):
            return path, (target - start).total_seconds()
    return None, None

# ─── フレーム抽出 ──────────────────────────────────────────────────
def extract_b64(path: Path, offset_s: float, scale: str) -> str | None:
    if path is None or not Path(path).exists():
        return None
    hh = int(offset_s // 3600)
    mm = int((offset_s % 3600) // 60)
    ss = offset_s % 60
    ts = f"{hh:02d}:{mm:02d}:{ss:06.3f}"
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as t:
        p = t.name
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-ss", ts, "-i", str(path),
             "-frames:v", "1", "-vf", f"scale={scale}", p],
            capture_output=True, timeout=20,
        )
        if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 100:
            return base64.b64encode(open(p, "rb").read()).decode()
        return None
    except Exception as e:
        print(f"  [extract error] {e}")
        return None
    finally:
        try: os.unlink(p)
        except: pass

# ─── サーバー状態 ──────────────────────────────────────────────────
state = {"edr": 0, "insta360": 0, "lens": "00"}

def compute_all_frames():
    def _one(args):
        i, anchor = args
        et = anchor + timedelta(seconds=state["edr"])
        it = anchor + timedelta(seconds=state["insta360"])
        edr_path, edr_off = find_edr(et)
        ins_off = (it - INSV_START).total_seconds()
        return {
            "row":      i,
            "anchor":   anchor.strftime("%H:%M:%S"),
            "edr_time": et.strftime("%H:%M:%S"),
            "ins_time": it.strftime("%H:%M:%S"),
            "edr_img":  extract_b64(edr_path, edr_off, "640:360") if edr_path else None,
            "ins_img":  extract_b64(INSV_FILES[state["lens"]], ins_off, "480:480")
                        if 0 <= ins_off <= INSV_DUR else None,
        }
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_one, enumerate(ANCHORS)))
    return sorted(results, key=lambda r: r["row"])


# ─── HTML ──────────────────────────────────────────────────────────
HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>時刻同期UI</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; background: #111; color: #ddd; padding: 14px; user-select: none; }
h1 { text-align: center; font-size: 15px; color: #9cf; margin-bottom: 12px; letter-spacing: .05em; }

/* ── コントロールバー ── */
#ctrl {
  display: flex; justify-content: space-between; align-items: center;
  background: #1c1c1c; border: 1px solid #2e2e2e; border-radius: 8px;
  padding: 10px 16px; margin-bottom: 12px; gap: 12px;
}
.cam-ctrl { display: flex; flex-direction: column; align-items: center; gap: 5px; }
.cam-ctrl h3 { font-size: 13px; color: #8cf; }
.btn-row { display: flex; gap: 4px; }
button {
  background: #252525; color: #ccc; border: 1px solid #3a3a3a;
  padding: 5px 9px; border-radius: 4px; cursor: pointer; font-size: 12px;
  transition: background .1s;
}
button:hover { background: #3a3a3a; }
button.p { color: #8f8; }
button.n { color: #f88; }
button.reset { color: #fa8; }
.off-disp { font-size: 11px; color: #777; }

/* ── ギャップ表示 ── */
#gap-box { text-align: center; min-width: 130px; }
#gap-box .glabel { font-size: 10px; color: #666; margin-bottom: 2px; }
#gap-num { font-size: 32px; font-weight: bold; color: #ff8; line-height: 1; }
#gap-unit { font-size: 13px; color: #999; }
#gap-hint { font-size: 10px; color: #555; margin-top: 4px; }

/* ── グリッド ── */
#grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.row-label {
  grid-column: 1 / -1;
  font-size: 11px; color: #555; border-top: 1px solid #222;
  padding-top: 8px; margin-top: 2px;
}
.cell {
  background: #181818; border: 1px solid #2c2c2c; border-radius: 6px; padding: 8px;
}
.cell-header {
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 5px;
}
.cell-name { font-size: 11px; color: #666; }
.cell-time { font-size: 14px; font-weight: bold; color: #ff8; }
.cell img { width: 100%; display: block; border-radius: 3px; }
.cell-empty { color: #744; font-size: 11px; padding: 30px; text-align: center; }

/* ── ローディング ── */
#overlay {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,.72); color: #fff; font-size: 16px;
  align-items: center; justify-content: center; z-index: 99; flex-direction: column; gap: 8px;
}
#overlay.show { display: flex; }
.spinner { width: 32px; height: 32px; border: 3px solid #444; border-top-color: #8cf;
           border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div id="overlay"><div class="spinner"></div>フレーム取得中...</div>

<h1>時刻同期UI — EDR-22G ⟷ Insta360 X3</h1>
<div id="dbg" style="font-size:11px;color:#888;text-align:center;margin-bottom:6px;min-height:16px"></div>

<div id="ctrl">
  <!-- EDR -->
  <div class="cam-ctrl">
    <h3>EDR-22G</h3>
    <div class="btn-row">
      <button class="n" onclick="adj('edr',-60)">−1分</button>
      <button class="n" onclick="adj('edr',-10)">−10秒</button>
      <button class="n" onclick="adj('edr',-1)">−1秒</button>
      <button class="p" onclick="adj('edr',1)">+1秒</button>
      <button class="p" onclick="adj('edr',10)">+10秒</button>
      <button class="p" onclick="adj('edr',60)">+1分</button>
    </div>
    <div class="off-disp" id="edr-disp">オフセット: 0秒</div>
  </div>

  <!-- ギャップ -->
  <div id="gap-box">
    <div class="glabel">Insta360 − EDR ギャップ</div>
    <div><span id="gap-num">0</span><span id="gap-unit">秒</span></div>
    <div id="gap-hint">正 = Insta360が進んでいる<br>負 = Insta360が遅れている</div>
    <button class="reset" style="margin-top:8px;font-size:11px" onclick="reset()">リセット</button>
  </div>

  <!-- Insta360 -->
  <div class="cam-ctrl">
    <h3>Insta360 X3</h3>
    <div class="btn-row">
      <button class="n" onclick="adj('insta360',-60)">−1分</button>
      <button class="n" onclick="adj('insta360',-10)">−10秒</button>
      <button class="n" onclick="adj('insta360',-1)">−1秒</button>
      <button class="p" onclick="adj('insta360',1)">+1秒</button>
      <button class="p" onclick="adj('insta360',10)">+10秒</button>
      <button class="p" onclick="adj('insta360',60)">+1分</button>
    </div>
    <div class="off-disp" id="ins-disp">オフセット: 0秒</div>
    <button id="lens-btn" onclick="toggleLens()" style="margin-top:4px;color:#8cf;font-size:11px">レンズ: 前(_00_)</button>
  </div>
</div>

<div id="grid"></div>

<script>
let offsets = {edr: 0, insta360: 0};

function fmt(s) {
  const sign = s >= 0 ? '+' : '';
  if (Math.abs(s) >= 60) {
    const m = Math.floor(Math.abs(s) / 60), sec = Math.abs(s) % 60;
    return `オフセット: ${sign}${s >= 0 ? '' : '-'}${m}分${sec > 0 ? sec + '秒' : ''}`;
  }
  return `オフセット: ${sign}${s}秒`;
}

function updateUI() {
  document.getElementById('edr-disp').textContent = fmt(offsets.edr);
  document.getElementById('ins-disp').textContent = fmt(offsets.insta360);
  const gap = offsets.insta360 - offsets.edr;
  document.getElementById('gap-num').textContent = (gap >= 0 ? '+' : '') + gap;
}

let loadGen = 0; // 古いレスポンスを破棄するための世代カウンター
let lens = '00';

function adj(cam, d) {
  offsets[cam] += d;
  updateUI();
  load();
}

function reset() {
  offsets = {edr: 0, insta360: 0};
  updateUI();
  load();
}

function toggleLens() {
  lens = lens === '00' ? '10' : '00';
  const btn = document.getElementById('lens-btn');
  btn.textContent = lens === '00' ? 'レンズ: 前(_00_)' : 'レンズ: 後(_10_)';
  btn.style.color = lens === '00' ? '#8cf' : '#fc8';
  load();
}

function dbg(msg) {
  const el = document.getElementById('dbg');
  el.textContent = msg;
}

async function load() {
  const gen = ++loadGen;
  const ov = document.getElementById('overlay');
  ov.classList.add('show');
  const payload = Object.assign({}, offsets, {lens});
  const sending = JSON.stringify(payload);
  dbg(`送信: ${sending}`);
  console.log('[load] sending', payload, 'gen=', gen);
  try {
    const res = await fetch('/frames', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: sending,
    });
    if (gen !== loadGen) { console.log('[load] discarded old gen', gen); return; }
    if (!res.ok) { dbg(`エラー: HTTP ${res.status}`); return; }
    const rows = await res.json();
    if (gen !== loadGen) return;
    console.log('[load] received', rows.map(r => ({edr: r.edr_time, ins: r.ins_time})));
    dbg(`受信: T1 edr=${rows[0]?.edr_time} ins=${rows[0]?.ins_time}`);

    const grid = document.getElementById('grid');
    grid.innerHTML = '';

    rows.forEach((row, i) => {
      const lbl = document.createElement('div');
      lbl.className = 'row-label';
      lbl.textContent = `T${i + 1}  基準アンカー ${row.anchor}`;
      grid.appendChild(lbl);

      const eCell = document.createElement('div');
      eCell.className = 'cell';
      eCell.innerHTML = `
        <div class="cell-header">
          <span class="cell-name">EDR-22G（前方）</span>
          <span class="cell-time">${row.edr_time}</span>
        </div>
        ${row.edr_img
          ? `<img src="data:image/jpeg;base64,${row.edr_img}">`
          : `<div class="cell-empty">範囲外</div>`}`;

      const iCell = document.createElement('div');
      iCell.className = 'cell';
      iCell.innerHTML = `
        <div class="cell-header">
          <span class="cell-name">Insta360 X3（360°）</span>
          <span class="cell-time">${row.ins_time}</span>
        </div>
        ${row.ins_img
          ? `<img src="data:image/jpeg;base64,${row.ins_img}">`
          : `<div class="cell-empty">範囲外</div>`}`;

      grid.appendChild(eCell);
      grid.appendChild(iCell);
    });
  } catch(e) {
    dbg(`例外: ${e}`);
    console.error('[load] error', e);
  } finally {
    if (gen === loadGen) ov.classList.remove('show');
  }
}

load();
</script>
</body>
</html>"""


# ─── HTTP ハンドラー ───────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass  # アクセスログ抑制

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        if self.path == "/frames":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                state["edr"]      = int(body.get("edr", 0))
                state["insta360"] = int(body.get("insta360", 0))
                state["lens"]     = "10" if body.get("lens") == "10" else "00"
                print(f"  [POST /frames] edr={state['edr']:+d}s  insta360={state['insta360']:+d}s  lens={state['lens']}")
                rows = compute_all_frames()
                for r in rows:
                    has_e = "ok" if r["edr_img"] else "null"
                    has_i = "ok" if r["ins_img"] else "null"
                    print(f"    T{r['row']+1}: edr={r['edr_time']} [{has_e}]  ins={r['ins_time']} [{has_i}]")
                data = json.dumps(rows).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                import traceback
                msg = traceback.format_exc()
                print(f"  [ERROR] {msg}")
                err = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)


# ─── エントリーポイント ────────────────────────────────────────────
def main():
    port = 8080
    url  = f"http://localhost:{port}"
    print(f"\n EDR/Insta360 時刻同期UI")
    print(f" ブラウザ: {url}")
    print(f" ffmpeg : {FFMPEG}")
    print(" 終了   : Ctrl+C\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        HTTPServer(("localhost", port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")

if __name__ == "__main__":
    main()
