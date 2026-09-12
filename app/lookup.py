# -*- coding: utf-8 -*-
"""住所・座標・地番 → 想定震度と液状化の判定（名古屋市）。

設計の芯（Kurageの防災GISと同じ約束）:
  1. 「市外」と「判定対象外」と「危険度なし」を必ず区別する。
     とくに液状化は、負のPL＝判定の対象に入っていないだけで「液状化しない」ではない。
  2. 結果には必ず根拠（想定の出典・データ時点・メッシュコード）を添える。
  3. これは**予測**であって、実際にその震度になると決まっているわけではない。
  4. 地番が分かる場合は、代表点ではなく**筆の形**で判定し、
     筆にかかるメッシュの幅（最小〜最大）で返す。敷地の中で差が出るのが普通だからだ。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.codes import (LIQ_OUT_OF_SCOPE, LIQ_OUT_OF_SCOPE_NOTE, SHINDO_MEANING,
                       liq_rank, shindo_kaikyu)

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kjishin.sqlite"


@dataclass
class Cell:
    code: str
    i_s: float
    acc: float
    vel: float
    pl: float
    settle: float


@dataclass
class Result:
    lat: float
    lon: float
    address: str = ""
    status: str = "outside"        # inside / outside（市域外）
    cells: list[Cell] = field(default_factory=list)
    parcel: str = ""               # 地番で引いたときの地番
    parcel_cells: int = 0          # 筆にかかったメッシュ数
    notes: list[str] = field(default_factory=list)
    vintage: str = ""
    attribution: str = ""

    # ---- 画面に出す形 ----
    @property
    def shindo_min(self) -> float | None:
        return min((c.i_s for c in self.cells), default=None)

    @property
    def shindo_max(self) -> float | None:
        return max((c.i_s for c in self.cells), default=None)

    @property
    def shindo_label(self) -> str:
        if not self.cells:
            return ""
        lo, hi = shindo_kaikyu(self.shindo_min), shindo_kaikyu(self.shindo_max)
        return lo if lo == hi else f"{lo}〜{hi}"

    @property
    def shindo_meaning(self) -> str:
        return SHINDO_MEANING.get(shindo_kaikyu(self.shindo_max or 0), "")

    @property
    def liq_label(self) -> str:
        """筆にかかるメッシュのうち、**いちばん危険な側**を代表にする。
        敷地の一部でも液状化の危険が高いなら、それを伝えないと意味がない。"""
        if not self.cells:
            return ""
        valid = [c.pl for c in self.cells if c.pl >= 0]
        if not valid:
            return LIQ_OUT_OF_SCOPE
        name, _ = liq_rank(max(valid))
        if len(valid) < len(self.cells):
            return f"{name}（一部は判定対象外）"
        return name

    @property
    def liq_pl(self) -> float | None:
        valid = [c.pl for c in self.cells if c.pl >= 0]
        return max(valid) if valid else None

    @property
    def settle_max(self) -> float | None:
        valid = [c.settle for c in self.cells if c.settle >= 0]
        return max(valid) if valid else None


class Index:
    def __init__(self, db: Path = DB):
        self.db = db
        self._conn: sqlite3.Connection | None = None
        self.vintage = ""
        self.attribution = ""
        self.count = 0

    def load(self) -> None:
        conn = sqlite3.connect(self.db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        meta = {r["k"]: r["v"] for r in conn.execute("SELECT k, v FROM meta")}
        self.vintage = meta.get("vintage", "")
        self.attribution = meta.get("attribution", "")
        self.count = conn.execute("SELECT COUNT(*) FROM mesh").fetchone()[0]

    def _cells_in(self, minx, miny, maxx, maxy) -> list[Cell]:
        """矩形に重なるメッシュを R*Tree で引く。"""
        cur = self._conn.execute(
            """SELECT m.code, m.i_s, m.acc, m.vel, m.pl, m.settle
               FROM mesh_rtree t JOIN rtree_map r ON r.id = t.id JOIN mesh m ON m.code = r.code
               WHERE t.maxx >= ? AND t.minx <= ? AND t.maxy >= ? AND t.miny <= ?""",
            (minx, maxx, miny, maxy))
        return [Cell(r["code"], r["i_s"], r["acc"], r["vel"], r["pl"], r["settle"]) for r in cur]

    def check(self, lat: float, lon: float, address: str = "") -> Result:
        if self._conn is None:
            self.load()
        out = Result(lat=lat, lon=lon, address=address,
                     vintage=self.vintage, attribution=self.attribution)
        out.cells = self._cells_in(lon, lat, lon, lat)
        if not out.cells:
            out.status = "outside"
            out.notes.append(
                "この地点は名古屋市の地震ハザードマップの範囲に入っていません。"
                "このデータは名古屋市が市域について作成したもので、市外は収録していません。"
                "お住まいの市町村の地震ハザードマップをご確認ください。")
            return out
        out.status = "inside"
        self._annotate(out)
        return out

    def check_parcel(self, geom, chiban: str, address: str = "") -> Result:
        """地番の筆ポリゴンで判定する。筆にかかるメッシュ全部を見る。"""
        if self._conn is None:
            self.load()
        minx, miny, maxx, maxy = geom.bounds
        out = Result(lat=(miny + maxy) / 2, lon=(minx + maxx) / 2, address=address,
                     vintage=self.vintage, attribution=self.attribution,
                     parcel=chiban, status="inside")
        # 外接矩形で絞ってから、筆の形と実際に重なるものだけ残す
        from shapely.geometry import box
        cand = self._cells_in(minx, miny, maxx, maxy)
        keep = []
        for c in cand:
            row = self._conn.execute(
                "SELECT minx,miny,maxx,maxy FROM mesh WHERE code=?", (c.code,)).fetchone()
            if geom.intersects(box(row["minx"], row["miny"], row["maxx"], row["maxy"])):
                keep.append(c)
        out.cells = keep or cand
        out.parcel_cells = len(out.cells)
        if not out.cells:
            out.status = "outside"
            out.notes.append("この地番は名古屋市の地震ハザードマップの範囲に入っていません。")
            return out
        self._annotate(out)
        out.notes.insert(0,
            f"地番の区画（筆）にかかる{out.parcel_cells}メッシュを調べた結果です。"
            "敷地の中でも場所によって想定が変わるため、いちばん厳しい側を代表として表示しています。")
        return out

    def _annotate(self, out: Result) -> None:
        if all(c.pl < 0 for c in out.cells):
            out.notes.append(LIQ_OUT_OF_SCOPE_NOTE)
        out.notes.append(
            "これは名古屋市が公表した南海トラフ地震の被害想定（最大クラス）にもとづく**予測**です。"
            "実際にこの震度になると決まっているわけではありません。"
            "地盤は数十メートル単位で変わるため、建築や地盤改良の判断には現地の地盤調査が要ります。")
