#!/usr/bin/env python3
"""名古屋市オープンデータ「地震ハザードマップ」（震度・液状化）を SQLite に取り込む。

  .venv/bin/python scripts/load_nagoya.py

なぜ shapely を使わないか:
  この shapefile の地物は**緯度経度に平行な長方形メッシュ**で、四隅が属性
  （X1,Y1,X2,Y2）にそのまま入っている。ポリゴンを読む必要がなく、
  矩形の内外判定で足りる。12.6万メッシュを R*Tree に載せれば1件0.1ms以下。

利用条件:
  CC BY 4.0（出典表示のみ）。出典は名古屋市。
  想定は平成26年2月公表の南海トラフ地震（最大クラス）。
  **予測であって、実際にその震度になると決まっているわけではない**ので、画面にも必ず書く。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import shapefile  # pyshp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RAW = Path(os.environ.get("KJISHIN_RAW_DIR", "/mnt/data/kjishin/raw"))
DB = ROOT / "data" / "kjishin.sqlite"
UA = {"User-Agent": "kjishin/1.0 (kurage.exbridge.jp)"}
VINTAGE = "令和7年8月時点（想定は平成26年2月公表の南海トラフ地震・最大クラス）"
ATTRIBUTION = "出典: 名古屋市オープンデータ「地震ハザードマップ」（CC BY 4.0）を加工して作成"

SOURCES = {
    "shindo": "https://data.bodik.jp/dataset/e3e3e05a-95de-4897-8633-4887595c7562/resource/"
              "0d377aba-4b3d-4801-8b7a-8bfc33bcf18c/download/earthquake_hazard_map-seismic_intensity.zip",
    "ekijoka": "https://data.bodik.jp/dataset/e3e3e05a-95de-4897-8633-4887595c7562/resource/"
               "f0aaa74e-e43e-4335-b290-6253edd88234/download/earthquake_hazard_map-liquefaction.zip",
}


def fetch(tag: str, url: str) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    z = RAW / f"{tag}.zip"
    if not (z.exists() and z.stat().st_size):
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=300) as r, open(str(z) + ".part", "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        os.replace(str(z) + ".part", z)
    ext = RAW / "ext"
    ext.mkdir(exist_ok=True)
    if not (ext / f"{tag}.shp").exists():
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                e = n.rsplit(".", 1)[-1]
                with zf.open(n) as src, open(ext / f"{tag}.{e}", "wb") as w:
                    w.write(src.read())
    return ext / tag


def schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS mesh;
        CREATE TABLE mesh (
          code TEXT PRIMARY KEY,
          minx REAL, miny REAL, maxx REAL, maxy REAL,
          i_s REAL,        -- 計測震度
          acc REAL,        -- 最大加速度(gal)
          vel REAL,        -- 最大速度(kine)
          pl REAL,         -- 液状化指数PL。負は判定対象外
          settle REAL      -- 沈下量(m)。負は判定対象外
        );
        -- 矩形メッシュなので空間索引は R*Tree で足りる（shapelyもPostGISも要らない）
        DROP TABLE IF EXISTS mesh_rtree;
        CREATE VIRTUAL TABLE mesh_rtree USING rtree(id, minx, maxx, miny, maxy);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """
    )


def main() -> int:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    schema(conn)

    print("震度を読み込み中…")
    base = fetch("shindo", SOURCES["shindo"])
    rows: dict[str, list] = {}
    r = shapefile.Reader(str(base), encoding="cp932")
    for rec in r.iterRecords():
        rows[rec["MESH"]] = [rec["X1"], rec["Y1"], rec["X2"], rec["Y2"],
                             rec["I_S"], rec["ACC_S"], rec["VEL_S"], None, None]
    print(f"  {len(rows):,} メッシュ")

    print("液状化を読み込み中…")
    base = fetch("ekijoka", SOURCES["ekijoka"])
    r2 = shapefile.Reader(str(base), encoding="cp932")
    miss = 0
    for rec in r2.iterRecords():
        row = rows.get(rec["MESH"])
        if row is None:
            miss += 1
            continue
        row[7], row[8] = rec["PL"], rec["S"]
    print(f"  震度側に無いメッシュ: {miss:,}")

    conn.executemany(
        "INSERT INTO mesh(code,minx,miny,maxx,maxy,i_s,acc,vel,pl,settle) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(code, *vals) for code, vals in rows.items()])
    conn.executemany(
        "INSERT INTO mesh_rtree(id,minx,maxx,miny,maxy) VALUES(?,?,?,?,?)",
        [(i, v[0], v[2], v[1], v[3]) for i, (code, v) in enumerate(rows.items(), start=1)])
    # rtree の id と mesh の対応を持つ
    conn.execute("DROP TABLE IF EXISTS rtree_map")
    conn.execute("CREATE TABLE rtree_map (id INTEGER PRIMARY KEY, code TEXT)")
    conn.executemany("INSERT INTO rtree_map(id,code) VALUES(?,?)",
                     [(i, code) for i, (code, _) in enumerate(rows.items(), start=1)])
    for k, v in (("vintage", VINTAGE), ("attribution", ATTRIBUTION),
                 ("loaded_at", time.strftime("%Y-%m-%d %H:%M:%S"))):
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
    conn.commit()

    n = conn.execute("SELECT COUNT(*) FROM mesh").fetchone()[0]
    neg = conn.execute("SELECT COUNT(*) FROM mesh WHERE pl < 0").fetchone()[0]
    mn, mx = conn.execute("SELECT MIN(i_s), MAX(i_s) FROM mesh").fetchone()
    conn.close()
    print(f"\n合計 {n:,} メッシュ／計測震度 {mn:.2f}〜{mx:.2f}／液状化の判定対象外 {neg:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
