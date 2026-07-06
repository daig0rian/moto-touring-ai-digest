#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""
EDR-22G 全セグメントからGPS抽出 → タイムスタンプ付きCSV + インタラクティブマップ
実行: uv run tools/02_extract_gps.py
出力: cam-data/gps/
"""

import csv, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# edr_mp4_extract は本プロジェクトの兄弟ディレクトリにある別リポジトリ（GPS抽出ロジック本体）
sys.path.insert(0, str(PROJECT_ROOT.parent / "edr_mp4_extract"))
from extract_data import extract_gps_records

EDR_DIR      = PROJECT_ROOT / "cam-data/EDR_MotoDR/Normal"
OUT_DIR      = PROJECT_ROOT / "cam-data/gps"
JST          = timezone(timedelta(hours=9))
SEG_DUR      = 30.5
EDR_OVERLAP  = 0.5   # 各セグメント先頭1GOP分が前セグメントと重複（dashcam-joiner 調査済み）

# ─── GPS抽出 ───────────────────────────────────────────────────────
def seg_start_time(mp4: Path) -> datetime:
    parts = mp4.stem.split("_")   # F / 20260703091953 / 20260703092024 / N
    return datetime.strptime(parts[2], "%Y%m%d%H%M%S").replace(tzinfo=JST)

def extract_all(edr_dir: Path) -> list[dict]:
    segments = sorted(edr_dir.glob("F_20260703*.MP4"))
    print(f"セグメント: {len(segments)} 件")
    points = []
    for seg_idx, mp4 in enumerate(segments):
        t0 = seg_start_time(mp4)
        for r in extract_gps_records(str(mp4)):
            if r["gps_status"] != "A" or r["latitude"] == 0:
                continue
            offset_sec = r["file_fraction"] * SEG_DUR
            # 2本目以降は先頭1GOP(0.5秒)が前セグメントと重複 → スキップ
            if seg_idx > 0 and offset_sec < EDR_OVERLAP:
                continue
            r["timestamp"] = t0 + timedelta(seconds=offset_sec)
            r["segment"]   = mp4.name
            points.append(r)
    points.sort(key=lambda r: r["timestamp"])
    print(f"有効GPSポイント: {len(points)} 件")
    return points

# ─── CSV出力 ───────────────────────────────────────────────────────
def save_csv(points: list[dict], path: Path):
    fields = ["timestamp", "latitude", "longitude", "speed_kmh",
              "heading_deg", "altitude_m", "segment"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in points:
            w.writerow({
                "timestamp":   p["timestamp"].strftime("%H:%M:%S"),
                "latitude":    p["latitude"],
                "longitude":   p["longitude"],
                "speed_kmh":   p["speed_kmh"],
                "heading_deg": p["heading_deg"],
                "altitude_m":  p["altitude_m"],
                "segment":     p["segment"],
            })
    print(f"CSV: {path}")

# ─── マップHTML生成 ────────────────────────────────────────────────
# Sentinels: __GPS_DATA__ __TOUR_DATE__ __T_START__ __T_END__ __N_PTS__
_MAP_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ルート — __TOUR_DATE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*,::before,::after{box-sizing:border-box;margin:0;padding:0}

:root {
  --bg:    #dde5eb;
  --panel: #ecf2f6;
  --edge:  #b4c6d2;
  --text:  #1a2830;
  --dim:   #587080;
  --accent:#e85d04;
  --glow:  rgba(232,93,4,.18);
  --mono:  ui-monospace,'SF Mono',Consolas,'Liberation Mono',monospace;
  --ui:    -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
}

body{
  background:var(--bg);
  color:var(--text);
  font-family:var(--ui);
  display:flex;
  flex-direction:column;
  align-items:center;
  min-height:100vh;
  padding:16px;
}

#shell{
  width:min(480px,100%);
  display:flex;
  flex-direction:column;
  gap:10px;
}

/* ── 時刻バー ── */
#timebar{
  display:flex;
  align-items:center;
  gap:12px;
  padding:8px 14px;
  background:var(--panel);
  border:1px solid var(--edge);
  border-radius:5px;
}

#time-val{
  font-family:var(--mono);
  font-size:15px;
  color:var(--accent);
  letter-spacing:.05em;
  min-width:7ch;
  font-variant-numeric:tabular-nums;
}

#spd-val{
  font-family:var(--mono);
  font-size:12px;
  color:var(--dim);
  min-width:9ch;
  text-align:right;
  font-variant-numeric:tabular-nums;
}

#range-wrap{
  flex:1;
  position:relative;
  height:24px;
  display:flex;
  align-items:center;
}

#track-bg{
  position:absolute;
  left:0;right:0;
  height:3px;
  background:var(--edge);
  border-radius:2px;
  pointer-events:none;
}

#track-fill{
  position:absolute;
  left:0;
  height:3px;
  background:var(--accent);
  border-radius:2px;
  pointer-events:none;
  width:0;
  will-change:width;
}

input[type=range]#slider{
  position:relative;
  z-index:2;
  width:100%;
  height:24px;
  -webkit-appearance:none;
  appearance:none;
  background:transparent;
  cursor:pointer;
  outline:none;
}

input[type=range]#slider::-webkit-slider-thumb{
  -webkit-appearance:none;
  width:13px;height:13px;
  border-radius:50%;
  background:var(--accent);
  box-shadow:0 0 0 2px var(--bg),0 0 8px var(--glow);
}

input[type=range]#slider::-moz-range-thumb{
  width:13px;height:13px;
  border-radius:50%;
  background:var(--accent);
  border:2px solid var(--bg);
  box-shadow:0 0 8px var(--glow);
}

input[type=range]#slider:focus-visible::-webkit-slider-thumb{
  box-shadow:0 0 0 2px var(--bg),0 0 0 4px var(--glow);
}

/* ── コントロールバー ── */
#ctrlbar{
  display:flex;
  justify-content:space-between;
  align-items:center;
  padding:5px 10px;
  background:var(--panel);
  border:1px solid var(--edge);
  border-radius:5px;
}


#zoom-ctrl{
  display:flex;
  border:1px solid var(--edge);
  border-radius:4px;
  overflow:hidden;
}

.z-btn{
  padding:4px 14px;
  background:transparent;
  color:var(--dim);
  border:none;
  font-family:var(--ui);
  font-size:11px;
  letter-spacing:.05em;
  cursor:pointer;
  transition:background .15s,color .15s;
}

.z-btn+.z-btn{
  border-left:1px solid var(--edge);
}

.z-btn.on{
  background:var(--accent);
  color:#fff;
}

.z-btn:not(.on):hover{
  background:var(--edge);
  color:var(--text);
}

.z-btn:focus-visible{
  outline:2px solid var(--accent);
  outline-offset:-2px;
}

/* ── 地図 ── */
#map-wrap{
  width:100%;
  aspect-ratio:1/1;
  border:1px solid var(--edge);
  border-radius:5px;
  overflow:hidden;
  transition:aspect-ratio .25s ease;
}
#map-wrap.wide{aspect-ratio:16/9}

#ar-ctrl{
  display:flex;
  border:1px solid var(--edge);
  border-radius:4px;
  overflow:hidden;
}

#map{width:100%;height:100%}

.leaflet-control-attribution{
  font-size:9px!important;
  opacity:.35;
  background:transparent!important;
  color:var(--dim)!important;
  box-shadow:none!important;
}

.leaflet-control-zoom a{
  background:var(--panel)!important;
  color:var(--text)!important;
  border-color:var(--edge)!important;
}

.leaflet-control-zoom a:hover{
  background:var(--edge)!important;
}

/* ── 住所 ── */
#addr-row{
  display:flex;
  align-items:flex-start;
  gap:8px;
}

#addr-eyebrow{
  font-size:10px;
  letter-spacing:.1em;
  text-transform:uppercase;
  color:var(--dim);
  padding-top:9px;
  white-space:nowrap;
  flex-shrink:0;
}

#address{
  flex:1;
  background:var(--panel);
  color:var(--text);
  border:1px solid var(--edge);
  border-radius:5px;
  padding:8px 10px;
  font-family:var(--ui);
  font-size:12px;
  line-height:1.6;
  resize:vertical;
  min-height:52px;
  outline:none;
  transition:border-color .15s;
}

#address:focus{border-color:var(--accent)}

#copy-btn{
  flex-shrink:0;
  padding:7px 14px;
  background:var(--edge);
  color:var(--text);
  border:1px solid var(--edge);
  border-radius:5px;
  font-family:var(--ui);
  font-size:11px;
  cursor:pointer;
  transition:background .15s,color .15s,border-color .15s;
  white-space:nowrap;
}

#copy-btn:hover{
  background:var(--accent);
  color:#fff;
  border-color:var(--accent);
}

#copy-btn:focus-visible{
  outline:2px solid var(--accent);
  outline-offset:2px;
}

/* ── フッター ── */
#footer{
  display:flex;
  justify-content:space-between;
  font-size:10px;
  color:var(--dim);
  padding:0 2px;
  font-variant-numeric:tabular-nums;
  letter-spacing:.03em;
}
</style>
</head>
<body>
<div id="shell">

  <div id="timebar">
    <span id="time-val">--:--:--</span>
    <div id="range-wrap">
      <div id="track-bg"></div>
      <div id="track-fill"></div>
      <input type="range" id="slider" min="0" max="1" value="0" aria-label="再生位置">
    </div>
    <span id="spd-val">-- km/h</span>
  </div>

  <div id="ctrlbar">
    <div style="display:flex;gap:6px;align-items:center;margin-left:auto">
      <div id="ar-ctrl" role="group" aria-label="縦横比">
        <button class="z-btn on" id="ar-11"  onclick="setAspect('1:1')">1:1</button>
        <button class="z-btn"    id="ar-169" onclick="setAspect('16:9')">16:9</button>
      </div>
      <div id="zoom-ctrl" role="group" aria-label="表示範囲">
        <button class="z-btn on"  id="z-full"   onclick="setZoom('full')">全体</button>
        <button class="z-btn"     id="z-follow" onclick="setZoom('follow')">追尾</button>
      </div>
    </div>
  </div>

  <div id="map-wrap"><div id="map"></div></div>

  <div id="addr-row">
    <span id="addr-eyebrow">所在地</span>
    <textarea id="address" placeholder="スライダーを動かすと現在地の住所を取得します"></textarea>
    <button id="copy-btn" onclick="copyAddr()">コピー</button>
  </div>

  <div id="footer">
    <span>__T_START__ 出発</span>
    <span id="footer-idx">0 / __N_PTS__ 点</span>
    <span>__T_END__ 着</span>
  </div>

</div>
<script>
(function(){

var GPS = __GPS_DATA__;
var N   = GPS.length;

var ROUTE_COLOR = '#1552b8';

var map = L.map('map', {zoomControl: true, attributionControl: true});
L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 19,
}).addTo(map);

var ll = GPS.map(function(p){ return [p.a, p.o]; });
var allBounds = L.latLngBounds(ll).pad(0.06);
map.fitBounds(allBounds);

/* ── ポリライン (futureが下、pastが上) ── */
var futureLine = L.polyline(ll, {
  color: ROUTE_COLOR, weight: 6.25, opacity: 0.55,
  lineJoin: 'round', lineCap: 'round',
}).addTo(map);

var pastLine = L.polyline([ll[0]], {
  color: ROUTE_COLOR, weight: 8.75, opacity: 0.9,
  lineJoin: 'round', lineCap: 'round',
}).addTo(map);

/* ── スタート・ゴールマーカー ── */
function pinCircle(latlng, color, tip) {
  return L.circleMarker(latlng, {
    radius: 5, color: color, fillColor: color, fillOpacity: 1, weight: 0,
  }).bindTooltip(tip);
}
pinCircle(ll[0],   '#52b788', 'スタート ' + GPS[0].t).addTo(map);
pinCircle(ll[N-1], '#e05b5b', 'ゴール '   + GPS[N-1].t).addTo(map);

/* ── 現在位置ドット ── */
var dotIcon = L.divIcon({
  className: '',
  html: '<div style="width:12px;height:12px;border-radius:50%;background:#ff8800;border:2px solid #fff;box-shadow:0 0 7px rgba(255,136,0,.7)"></div>',
  iconSize: [12, 12], iconAnchor: [6, 6],
});
var dot = L.marker(ll[0], {icon: dotIcon, zIndexOffset: 1000}).addTo(map);

/* ── 状態 ── */
var currentIdx  = 0;
var zoomMode = 'full';   // 'full' | 'follow'

/* ── 更新ロジック ── */
function update(i) {
  currentIdx = i;
  var p = GPS[i];

  pastLine.setLatLngs(ll.slice(0, i + 1));
  futureLine.setLatLngs(ll.slice(i));
  dot.setLatLng(ll[i]);

  document.getElementById('time-val').textContent = p.t;
  document.getElementById('spd-val').textContent  = p.s.toFixed(0) + ' km/h';
  document.getElementById('track-fill').style.width = (i / (N - 1) * 100).toFixed(2) + '%';
  document.getElementById('footer-idx').textContent = i + ' / ' + (N - 1) + ' 点';

  if (zoomMode === 'follow') {
    map.setView(ll[i], map.getZoom() < 15 ? 17 : map.getZoom(), {animate: false});
  }

  scheduleGeocode(p.a, p.o);
}

/* ── ズームトグル ── */
window.setZoom = function(mode) {
  zoomMode = mode;
  document.getElementById('z-full').classList.toggle('on',   mode === 'full');
  document.getElementById('z-follow').classList.toggle('on', mode === 'follow');

  if (mode === 'full') {
    map.fitBounds(allBounds, {animate: true});
  } else {
    map.setView(ll[currentIdx], 17, {animate: true});
  }
};

/* ── 逆ジオコーディング ── */
var gcTimer = null;
function scheduleGeocode(lat, lon) {
  clearTimeout(gcTimer);
  gcTimer = setTimeout(function() {
    fetch('https://nominatim.openstreetmap.org/reverse?lat=' + lat + '&lon=' + lon + '&format=json&accept-language=ja')
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(d){ if (d && d.display_name) document.getElementById('address').value = d.display_name; })
      .catch(function(){});
  }, 650);
}

document.getElementById('slider').max = N - 1;
document.getElementById('slider').addEventListener('input', function(e) {
  update(parseInt(e.target.value, 10));
});

/* ── アスペクト比トグル ── */
window.setAspect = function(ar) {
  var wrap = document.getElementById('map-wrap');
  wrap.classList.toggle('wide', ar === '16:9');
  document.getElementById('ar-11').classList.toggle('on',  ar === '1:1');
  document.getElementById('ar-169').classList.toggle('on', ar === '16:9');
  setTimeout(function(){ map.invalidateSize(); }, 270);
};

window.copyAddr = function() {
  var el = document.getElementById('address');
  if (!el.value) return;
  navigator.clipboard.writeText(el.value).then(function() {
    var btn = document.getElementById('copy-btn');
    var orig = btn.textContent;
    btn.textContent = '✓ コピー済み';
    setTimeout(function(){ btn.textContent = orig; }, 1600);
  });
};

update(0);

})();
</script>
</body>
</html>
"""


def create_map_html(points: list[dict], path: Path):
    gps_data = [
        {"t": p["timestamp"].strftime("%H:%M:%S"),
         "a": round(p["latitude"],  6),
         "o": round(p["longitude"], 6),
         "s": round(p["speed_kmh"], 1)}
        for p in points
    ]
    gps_json = json.dumps(gps_data, ensure_ascii=False, separators=(",", ":"))

    html = (_MAP_TEMPLATE
        .replace("__GPS_DATA__",  gps_json)
        .replace("__TOUR_DATE__", points[0]["timestamp"].strftime("%Y/%m/%d"))
        .replace("__T_START__",   points[0]["timestamp"].strftime("%H:%M"))
        .replace("__T_END__",     points[-1]["timestamp"].strftime("%H:%M"))
        .replace("__N_PTS__",     str(len(points) - 1)))

    path.write_text(html, encoding="utf-8")
    print(f"マップ: {path}")


# ─── メイン ────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    points = extract_all(EDR_DIR)
    if not points:
        print("有効なGPSポイントがありません")
        return

    t0, t1 = points[0]["timestamp"], points[-1]["timestamp"]
    dur = (t1 - t0).total_seconds() / 60
    print(f"時刻範囲: {t0.strftime('%H:%M:%S')} 〜 {t1.strftime('%H:%M:%S')}  ({dur:.1f}分)")

    save_csv(points, OUT_DIR / "tour_20260703.csv")
    create_map_html(points, OUT_DIR / "tour_20260703.html")
    print("\n完了。tour_20260703.html をブラウザで開いてください。")


if __name__ == "__main__":
    main()
