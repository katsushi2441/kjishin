#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kurage 地震ハザードマップ（名古屋版）— MCPサーバー（stdio・1ファイル）

Claude Code / Codex / Claude Desktop から、住所や地番を渡して
南海トラフ地震（最大クラス）の想定震度と液状化の危険度を引くための橋。

  claude mcp add kjishin -- /path/to/kjishin/.venv/bin/python /path/to/kjishin/kjishin_mcp.py

Codex は ~/.codex/config.toml に:
  [mcp_servers.kjishin]
  command = "/path/to/kjishin/.venv/bin/python"
  args = ["/path/to/kjishin/kjishin_mcp.py"]

設計（kdbagent・kaimom・klcrm と同じ約束）:
  - **製品本体の判定関数をそのまま呼ぶ薄い橋**。ここで別の判定を作らない。
    「判定対象外」と「危険度なし」の区別も、画面と同じ式で返す。
  - 読むだけ。書き込む口は無い（データは公開データの読み取り専用）。
  - AIが数字だけ抜いて誤って断言しないよう、**注意書き（notes）を必ず一緒に返す**。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

VERSION = "1.0.0"

try:
    from app.chiban import Chiban
    from app.lookup import Index
except Exception as e:  # 依存が入っていない場合にJSON-RPCを壊さない
    sys.stderr.write(f"kjishin を読み込めませんでした: {e}\n")
    raise SystemExit(1)

INDEX = Index()
CHIBAN = Chiban()
_loaded = False


def ensure() -> None:
    global _loaded
    if not _loaded:
        INDEX.load()
        CHIBAN.load()
        _loaded = True


def geocode(q: str):
    import urllib.parse
    import urllib.request
    u = ("https://msearch.gsi.go.jp/address-search/AddressSearch?"
         + urllib.parse.urlencode({"q": q}))
    req = urllib.request.Request(u, headers={"User-Agent": "kjishin-mcp/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        items = json.loads(r.read().decode("utf-8"))
    if not items:
        return None
    lon, lat = items[0]["geometry"]["coordinates"]
    return float(lat), float(lon), items[0]["properties"].get("title") or q


def as_dict(r) -> dict:
    return {
        "address": r.address, "lat": round(r.lat, 6), "lon": round(r.lon, 6),
        "status": r.status,
        "parcel": r.parcel or None, "parcel_mesh_count": r.parcel_cells or None,
        "shindo": r.shindo_label,
        "計測震度": {"min": r.shindo_min, "max": r.shindo_max},
        "shindo_meaning": r.shindo_meaning,
        "liquefaction": r.liq_label,
        "PL": r.liq_pl,
        "settlement_m": r.settle_max,
        "mesh_count": len(r.cells),
        "notes": r.notes,
        "data_vintage": r.vintage,
        "attribution": r.attribution,
    }


def j(v) -> str:
    return json.dumps(v, ensure_ascii=False, indent=2)


def err(msg: str):
    return False, json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def t_check(a: dict):
    ensure()
    q = str(a.get("address") or "").strip()
    lat, lon = a.get("lat"), a.get("lon")
    if lat is not None and lon is not None:
        r = INDEX.check(float(lat), float(lon), "")
        return True, j({"ok": True, **as_dict(r)})
    if not q:
        return err("address（住所か地番）か、lat と lon が要ります")
    hit = CHIBAN.find(q)
    if hit:
        geom, label, ban = hit
        r = INDEX.check_parcel(geom, ban, label)
        return True, j({"ok": True, "matched_by": "地番", **as_dict(r)})
    found = geocode(q)
    if not found:
        return err(f"住所が見つかりません: {q}")
    la, lo, title = found
    parcel = CHIBAN.at_point(la, lo)
    if parcel:
        geom, label, ban = parcel
        r = INDEX.check_parcel(geom, ban, title)
        return True, j({"ok": True, "matched_by": "住所→筆", **as_dict(r)})
    r = INDEX.check(la, lo, title)
    return True, j({"ok": True, "matched_by": "住所（代表点）", **as_dict(r)})


def t_parcel(a: dict):
    ensure()
    q = str(a.get("chiban") or "").strip()
    if not q:
        return err("chiban（例: 中区栄三丁目1-1）が要ります")
    hit = CHIBAN.find(q)
    if not hit:
        return err(f"その地番は地番参考図に見つかりません: {q}"
                   "（町丁名は漢数字です。例: 中区栄三丁目）")
    geom, label, ban = hit
    minx, miny, maxx, maxy = geom.bounds
    return True, j({"ok": True, "aza": label, "chiban": ban,
                    "bounds": {"minlon": minx, "minlat": miny, "maxlon": maxx, "maxlat": maxy},
                    "note": "地番参考図は求積および権利関係等の確認の根拠にはなりません"
                            "（名古屋市の利用上の注意）。令和8年1月1日時点。"})


def t_status(a: dict):
    ensure()
    return True, j({"ok": True,
                    "mesh_count": INDEX.count, "mesh_vintage": INDEX.vintage,
                    "parcel_count": CHIBAN.count, "parcel_vintage": CHIBAN.vintage,
                    "coverage": "名古屋市内のみ。市外は status='outside' を返す",
                    "attribution": [INDEX.attribution, CHIBAN.attribution]})


TOOLS = [
    {"name": "kjishin_check",
     "description": "名古屋市内の住所または地番について、南海トラフ地震（最大クラス）で想定される震度と液状化の危険度を返す。"
                    "地番が特定できた場合は代表点ではなく土地の区画（筆）の形で判定し、筆にかかるメッシュの幅で答える。"
                    "**液状化の「判定対象外」は「液状化しない」ではない**ので、そのまま伝えること。"
                    "返ってくる notes は必ず利用者に示すこと（予測であって確定ではない、という断りが入っている）。",
     "inputSchema": {"type": "object", "properties": {
         "address": {"type": "string", "description": "住所または地番（例: 中区栄三丁目1-1、名古屋市港区港明1丁目）"},
         "lat": {"type": "number", "description": "緯度（住所の代わりに座標で指定する場合）"},
         "lon": {"type": "number", "description": "経度"}},
         "required": []}},
    {"name": "kjishin_parcel",
     "description": "地番から土地の区画（筆）の位置と範囲を返す。名古屋市の地番参考図にもとづく。"
                    "町丁名は漢数字（中区栄三丁目）で入っている。求積や権利関係の根拠にはならない。",
     "inputSchema": {"type": "object", "properties": {
         "chiban": {"type": "string", "description": "区名＋町丁目＋地番（例: 中区栄三丁目1-1）"}},
         "required": ["chiban"]}},
    {"name": "kjishin_status",
     "description": "収録しているデータの件数・データ時点・出典・対象範囲を返す。"
                    "判定結果を引用するときの根拠として使う。",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
]


def out(m) -> None:
    sys.stdout.write(json.dumps(m, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method") or ""
        params = req.get("params") or {}
        if rid is None and method.startswith("notifications/"):
            continue
        if method == "initialize":
            out({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "kjishin", "version": VERSION},
                "instructions":
                    "名古屋市の地震ハザード（南海トラフ最大クラスの想定震度と液状化）を住所・地番で引く窓口です。"
                    "対象は名古屋市内のみで、市外は outside を返します。"
                    "液状化の『判定対象外』は『液状化しない』という意味ではありません（判定の対象に入っていない）。"
                    "結果は予測であって確定ではないので、notes をそのまま添えて回答してください。"}})
        elif method == "ping":
            out({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            out({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name") or ""
            a = params.get("arguments") or {}
            try:
                if name == "kjishin_check":
                    ok, text = t_check(a)
                elif name == "kjishin_parcel":
                    ok, text = t_parcel(a)
                elif name == "kjishin_status":
                    ok, text = t_status(a)
                else:
                    ok, text = err(f"使えないツールです: {name}")
            except Exception as e:
                ok, text = err(str(e))
            out({"jsonrpc": "2.0", "id": rid,
                 "result": {"content": [{"type": "text", "text": text}], "isError": not ok}})
        elif rid is not None:
            out({"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    main()
