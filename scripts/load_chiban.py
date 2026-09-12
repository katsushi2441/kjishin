#!/usr/bin/env python3
"""名古屋市オープンデータ「地番参考図」を SQLite に取り込む（筆ポリゴン）。

  .venv/bin/python scripts/load_chiban.py

これを入れると、住所の代表点ではなく**筆の形**で判定できるようになる。
Kurageの防災GISはどれも「代表点なので地番で確認してください」と断ってきたが、
名古屋市内に限ってはその断りを外せる。

**利用上の注意（配布物の「利用上の注意.pdf」より。画面にも必ず出す）**
  - 地番参考図は、**求積および権利関係等の確認の根拠となるものではない**
  - 道路は、道路幅員・境界位置・終始端位置等の形状を示すものではない
  - **令和8年1月1日時点**。以降の分合筆は法務局へ
  - 無地番地は N または内部管理番号（例: 9876543-A）で表示されることがある
  - 名古屋市は利用により生じた損失・損害について責任を負わない

DBは大きい（筆が90万件規模）ので /mnt/data に置き、data/ からsymlinkする。
"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import shapefile
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parent.parent
RAW = Path("/mnt/data/kjishin/chiban")
DB_REAL = Path("/mnt/data/kjishin/db/chiban.sqlite")
DB_LINK = ROOT / "data" / "chiban.sqlite"

ZIPS = {
    "sakae": "https://data.bodik.jp/dataset/efcfabb1-1e3b-4020-8342-50916d342303/resource/"
             "4368e923-0409-43a3-ad8d-07ac59af73d4/download/20260101_chibansankouzu_sakaeshizeizimusyo.zip",
    "honzin": "https://data.bodik.jp/dataset/efcfabb1-1e3b-4020-8342-50916d342303/resource/"
              "dbf14b0c-43b6-442b-9c6b-b550b87b126d/download/20260101_chibansankouzu_honzinshizeizimusyo.zip",
    "kanayama": "https://data.bodik.jp/dataset/efcfabb1-1e3b-4020-8342-50916d342303/resource/"
                "41b52ba6-c4e1-41a5-b04e-367eceaed77d/download/20260101_chibansankouzu_kanayamashizeizimusyo.zip",
}
TYOUTYOU_CSV = ("https://data.bodik.jp/dataset/efcfabb1-1e3b-4020-8342-50916d342303/resource/"
                "f8de0226-52b5-44d9-a4fa-0d52b031cbc9/download/20260101_chibansankouzu_tyoutyouko-do.csv")
VINTAGE = "令和8年1月1日時点"
# 地番参考図には .prj が入っておらず、座標は**平面直角座標系（メートル）**。
# 愛知県は第VII系。EPSG:6675 → EPSG:6668(JGD2011 緯度経度) に直してから入れる。
# 実測で確認: 栄三丁目の筆が (−23870.9, −92945.8) → 35.16192N / 136.90463E＝名古屋中心部。
# 第6系だと京都、第8系だと静岡に飛ぶので、第VII系で間違いない。
_TO_LL = Transformer.from_crs("EPSG:6675", "EPSG:6668", always_xy=False)


def to_lonlat_arr(xs, ys):
    """ファイルの (x=東西, y=南北) の配列 → (経度, 緯度)。EPSG:667x は (北, 東) の順。"""
    lat, lon = _TO_LL.transform(ys, xs)
    return lon, lat
ATTRIBUTION = "出典: 名古屋市オープンデータ「地番参考図」（CC BY 4.0）を加工して作成"


def extract(tag: str, url: str) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    z = RAW / f"{tag}.zip"
    if not (z.exists() and z.stat().st_size):
        # すでに別名で落としてあればそれを使う
        for cand in RAW.glob("*.zip"):
            if tag in cand.name:
                z = cand
                break
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "kjishin/1.0"})
            with urllib.request.urlopen(req, timeout=900) as r, open(str(z) + ".part", "wb") as f:
                while True:
                    b = r.read(1 << 22)
                    if not b:
                        break
                    f.write(b)
            os.replace(str(z) + ".part", z)
    ext = RAW / "ext"
    ext.mkdir(exist_ok=True)
    if not (ext / f"{tag}_tochi_poly.shp").exists():
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                try:
                    nn = n.encode("cp437").decode("cp932")
                except Exception:
                    nn = n
                base = nn.split("/")[-1].lower()
                if base.startswith("tochi_poly."):
                    with zf.open(n) as src, open(ext / f"{tag}_{base}", "wb") as w:
                        while True:
                            b = src.read(1 << 22)
                            if not b:
                                break
                            w.write(b)
    return ext / f"{tag}_tochi_poly"


def load_tyoutyou() -> dict[str, str]:
    """町丁コード → 区名を含む町丁名。CSVは UTF-16・各行が丸ごと引用符で囲まれている。"""
    p = RAW / "tyoutyou.csv"
    if not p.exists():
        req = urllib.request.Request(TYOUTYOU_CSV, headers={"User-Agent": "kjishin/1.0"})
        with urllib.request.urlopen(req, timeout=300) as r:
            p.write_bytes(r.read())
    t = p.read_bytes().decode("utf-16")
    out: dict[str, str] = {}
    for row in csv.reader(io.StringIO(t)):
        if not row:
            continue
        parts = [c.strip().replace("　", "") for c in row[0].split(",")]
        if len(parts) >= 3 and parts[0].isdigit():
            out[parts[0]] = parts[2]
    return out


def main() -> int:
    DB_REAL.parent.mkdir(parents=True, exist_ok=True)
    names = load_tyoutyou()
    print(f"町丁コード: {len(names):,} 件")

    conn = sqlite3.connect(DB_REAL)
    conn.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        DROP TABLE IF EXISTS parcel;
        CREATE TABLE parcel (
          id INTEGER PRIMARY KEY,
          azacd TEXT NOT NULL,      -- 町丁コード
          chiban TEXT NOT NULL,     -- 地番（44, 44-3 など）
          aza_name TEXT,            -- 区名を含む町丁名
          minx REAL, miny REAL, maxx REAL, maxy REAL,
          wkt TEXT NOT NULL);
        DROP TABLE IF EXISTS parcel_rtree;
        CREATE VIRTUAL TABLE parcel_rtree USING rtree(id, minx, maxx, miny, maxy);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)

    pid = 0
    for tag, url in ZIPS.items():
        base = extract(tag, url)
        r = shapefile.Reader(str(base), encoding="cp932")
        # 項目名の大文字小文字がファイルごとに違う（sakaeはAZACD、honzinはazacd）。両対応にする
        fields = [f[0] for f in r.fields[1:]]
        col = {f.lower(): f for f in fields}
        f_aza, f_ban = col.get("azacd"), col.get("txtcd")
        if not (f_aza and f_ban):
            print(f"  !! {tag}: azacd/txtcd が無い（項目: {fields}）。飛ばす")
            continue
        print(f"{tag}: {len(r):,} 筆を取り込み中…（項目 {f_aza}/{f_ban}）")
        batch, rbatch = [], []
        for sr in r.iterShapeRecords():
            rec, shp = sr.record, sr.shape
            if not shp.points:
                continue
            aza, ban = rec[f_aza], rec[f_ban]
            if not aza or not ban:
                continue
            g = shape(shp.__geo_interface__)
            if g.is_empty:
                continue
            g = shp_transform(lambda xs, ys, z=None: to_lonlat_arr(xs, ys), g)
            if g.is_empty:
                continue
            pid += 1
            minx, miny, maxx, maxy = g.bounds
            batch.append((pid, aza, ban, names.get(aza, ""), minx, miny, maxx, maxy, g.wkt))
            rbatch.append((pid, minx, maxx, miny, maxy))
            if len(batch) >= 20000:
                conn.executemany("INSERT INTO parcel VALUES(?,?,?,?,?,?,?,?,?)", batch)
                conn.executemany("INSERT INTO parcel_rtree VALUES(?,?,?,?,?)", rbatch)
                conn.commit(); batch, rbatch = [], []
        if batch:
            conn.executemany("INSERT INTO parcel VALUES(?,?,?,?,?,?,?,?,?)", batch)
            conn.executemany("INSERT INTO parcel_rtree VALUES(?,?,?,?,?)", rbatch)
            conn.commit()
        print(f"  累計 {pid:,} 筆")

    conn.execute("CREATE INDEX IF NOT EXISTS parcel_key ON parcel(azacd, chiban)")
    conn.execute("CREATE INDEX IF NOT EXISTS parcel_name ON parcel(aza_name)")
    for k, v in (("vintage", VINTAGE), ("attribution", ATTRIBUTION),
                 ("loaded_at", time.strftime("%Y-%m-%d %H:%M:%S"))):
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM parcel").fetchone()[0]
    conn.close()

    DB_LINK.parent.mkdir(parents=True, exist_ok=True)
    if DB_LINK.is_symlink() or DB_LINK.exists():
        DB_LINK.unlink()
    DB_LINK.symlink_to(DB_REAL)
    print(f"\n合計 {n:,} 筆／DB {DB_REAL} （{DB_REAL.stat().st_size/1e9:.2f} GB）")
    print(f"symlink: {DB_LINK} → {DB_REAL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
