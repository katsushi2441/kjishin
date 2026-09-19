#!/usr/bin/env python3
"""区ごとの想定震度・液状化の統計を作る（区ページの中身）。

**なぜ要るか**: この道具は住所を1件ずつ調べる入口しか持っていなかった。
一方 kflood は区ページを持っていて「名古屋市北区 ハザードマップ 洪水」で
9〜13位に入り、90日で表示375を得ている（2026-09-19 実測）。同じ粒度を用意する。

**区の割り当ては推測しない。** メッシュ自体は区を持っていないので、
地番参考図（parcel.aza_name の先頭が区名）の筆の中心点が入るメッシュに、
その区を数える。筆は98万件あるので、区の広さに応じた重みがそのまま入る。
複数の区にまたがるメッシュは、多数を占めた区に寄せる（件数も残す）。

  cd /home/kojima/work/kjishin && /usr/bin/python3 scripts/build_ward_stats.py
"""
from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESH_DB = ROOT / "data" / "kjishin.sqlite"
CHIBAN_DB = ROOT / "data" / "chiban.sqlite"
OUT = ROOT / "data" / "ward_stats.json"

# kflood・kfacilities・krefuge の区ページと同じ綴りにそろえる
SLUG = {"千種区": "chikusa", "東区": "higashi", "北区": "kita", "西区": "nishi",
        "中村区": "nakamura", "中区": "naka", "昭和区": "showa", "瑞穂区": "mizuho",
        "熱田区": "atsuta", "中川区": "nakagawa", "港区": "minato", "南区": "minami",
        "守山区": "moriyama", "緑区": "midori", "名東区": "meito", "天白区": "tempaku"}

# 計測震度から震度階級へ（app/main.py と同じ切り方）
def shindo(i: float) -> str:
    if i >= 6.5:
        return "震度7"
    if i >= 6.0:
        return "震度6強"
    if i >= 5.5:
        return "震度6弱"
    if i >= 5.0:
        return "震度5強"
    if i >= 4.5:
        return "震度5弱"
    return "震度4以下"


def main() -> None:
    mc = sqlite3.connect(MESH_DB)
    mc.row_factory = sqlite3.Row
    cc = sqlite3.connect(CHIBAN_DB)

    # 区ごとに「その区の筆の中心点」を集め、入るメッシュを引く
    agg: dict[str, dict] = {}
    for ward, slug in SLUG.items():
        rows = cc.execute(
            "SELECT minx, miny, maxx, maxy FROM parcel WHERE aza_name LIKE ?",
            (ward + "%",)).fetchall()
        if not rows:
            print(f"  {ward}: 筆が見つかりません")
            continue
        seen: set[str] = set()
        shindos = collections.Counter()
        pls, settles, i_s = [], [], []
        liq_target = 0
        for minx, miny, maxx, maxy in rows:
            x, y = (minx + maxx) / 2, (miny + maxy) / 2
            r = mc.execute(
                """SELECT m.code, m.i_s, m.pl, m.settle FROM mesh_rtree t
                   JOIN mesh m ON m.rowid = t.id
                   WHERE t.minx <= ? AND t.maxx >= ? AND t.miny <= ? AND t.maxy >= ?
                   LIMIT 1""", (x, x, y, y)).fetchone()
            if not r or r["code"] in seen:
                continue
            seen.add(r["code"])
            i_s.append(r["i_s"])
            shindos[shindo(r["i_s"])] += 1
            # **負のPLが「判定対象外」**（-1.0 と -6.6 の違いは分からないので区別しない）。
            # 0（危険度なし）と混ぜない。NULL ではないので is not None では拾えない。
            if r["pl"] is not None and r["pl"] >= 0:
                liq_target += 1
                pls.append(r["pl"])
                if r["settle"] is not None and r["settle"] >= 0:
                    settles.append(r["settle"])
        n = len(seen)
        if not n:
            print(f"  {ward}: メッシュに当たりませんでした")
            continue
        agg[slug] = {
            "name": ward, "slug": slug, "meshes": n, "parcels": len(rows),
            "shindo": dict(shindos.most_common()),
            "shindo_max": shindo(max(i_s)), "shindo_mode": shindos.most_common(1)[0][0],
            # **「判定対象外」は「液状化しない」ではない。** 母数を分けて持つ
            "liq_judged": liq_target, "liq_unjudged": n - liq_target,
            "pl_max": round(max(pls), 1) if pls else None,
            "pl_avg": round(sum(pls) / len(pls), 1) if pls else None,
            "settle_max_m": round(max(settles), 2) if settles else None,
        }
        print(f"  {ward}: メッシュ{n:,} 最大{agg[slug]['shindo_max']} "
              f"最多{agg[slug]['shindo_mode']} PL最大{agg[slug]['pl_max']} "
              f"判定対象外{agg[slug]['liq_unjudged']:,}")
    mc.close()
    cc.close()
    OUT.write_text(json.dumps({"wards": agg}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"書き出しました: {OUT}（{len(agg)}区）")


if __name__ == "__main__":
    main()
