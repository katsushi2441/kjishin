# -*- coding: utf-8 -*-
"""地番参考図（名古屋市）。住所文字列や座標から、土地の区画（筆）を引く。

なぜ要るか:
  Kurageの防災GISはどれも「住所は町丁目の代表点だから、地番で確認してください」と
  断ってきた。名古屋市は地番参考図をCC BY 4.0で公開しているので、市内に限っては
  **筆の形**で判定できる。代表点1点ではなく、敷地にかかる範囲で答えられる。

**利用上の注意（配布物の「利用上の注意.pdf」。画面にも必ず出す）**
  - 求積および権利関係等の確認の根拠にはならない
  - 道路は道路幅員・境界位置等の形状を示すものではない
  - 令和8年1月1日時点。以降の分合筆は法務局へ
  - 名古屋市は利用により生じた損失・損害について責任を負わない
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from shapely import wkt
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "chiban.sqlite"

#: 「中区栄3丁目1-1」「千種区青柳町1丁目15番」などから 町丁名 と 地番 を切り出す
_PAT = re.compile(
    r"^\s*(?:名古屋市)?\s*"
    r"(?P<aza>.+?[区].*?)"                       # 区名から町丁目まで
    r"\s*(?P<ban>\d+(?:[-－‐―ー]\d+)*)\s*(?:番地?|番)?\s*$")

_ZEN = str.maketrans("０１２３４５６７８９－‐―ー", "0123456789----")

#: 町丁名は漢数字で入っている（「中区栄三丁目」）。利用者は「栄3丁目」と書くので直す。
_KANSUJI = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九",
            10: "十", 11: "十一", 12: "十二", 13: "十三", 14: "十四", 15: "十五",
            16: "十六", 17: "十七", 18: "十八", 19: "十九", 20: "二十"}


def _to_kansuji_chome(aza: str) -> str:
    """「栄3丁目」→「栄三丁目」。すでに漢数字ならそのまま。"""
    def rep(m):
        n = int(m.group(1))
        return _KANSUJI.get(n, m.group(1)) + "丁目"
    return re.sub(r"(\d+)\s*丁目", rep, aza)


class Chiban:
    def __init__(self, db: Path = DB):
        self.db = db
        self._conn: sqlite3.Connection | None = None
        self.count = 0
        self.vintage = ""
        self.attribution = ""

    @property
    def available(self) -> bool:
        return self._conn is not None and self.count > 0

    def load(self) -> None:
        if not self.db.exists():
            return
        conn = sqlite3.connect(self.db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            self.count = conn.execute("SELECT COUNT(*) FROM parcel").fetchone()[0]
        except sqlite3.Error:
            conn.close()
            return
        meta = {r["k"]: r["v"] for r in conn.execute("SELECT k, v FROM meta")}
        self.vintage = meta.get("vintage", "")
        self.attribution = meta.get("attribution", "")
        self._conn = conn

    # ---- 地番で引く ----
    def find(self, q: str):
        """「中区栄3丁目1-1」→ (筆のポリゴン, 町丁名, 地番)。引けなければ None。"""
        if not self.available:
            return None
        m = _PAT.match(q.translate(_ZEN))
        if not m:
            return None
        aza = m.group("aza").strip().replace(" ", "").replace("　", "")
        aza = _to_kansuji_chome(aza)
        ban = m.group("ban")
        rows = self._conn.execute(
            "SELECT aza_name, chiban, wkt FROM parcel WHERE aza_name = ? AND chiban = ? LIMIT 1",
            (aza, ban)).fetchall()
        if not rows:
            # 「1-1」を「1」として持っている場合や、丁目の表記ゆれを許す
            rows = self._conn.execute(
                "SELECT aza_name, chiban, wkt FROM parcel "
                "WHERE aza_name LIKE ? AND chiban = ? LIMIT 1",
                (aza.replace("丁目", "%丁目") + "%", ban)).fetchall()
        if not rows:
            return None
        r = rows[0]
        return wkt.loads(r["wkt"]), r["aza_name"], r["chiban"]

    # ---- 座標から筆を引く ----
    def at_point(self, lat: float, lon: float):
        """その座標が乗っている筆。無ければ None。"""
        if not self.available:
            return None
        cur = self._conn.execute(
            """SELECT p.aza_name, p.chiban, p.wkt FROM parcel_rtree t
               JOIN parcel p ON p.id = t.id
               WHERE t.maxx >= ? AND t.minx <= ? AND t.maxy >= ? AND t.miny <= ?
               LIMIT 40""", (lon, lon, lat, lat))
        pt = Point(lon, lat)
        for r in cur:
            g = wkt.loads(r["wkt"])
            if g.covers(pt):
                return g, r["aza_name"], r["chiban"]
        return None
