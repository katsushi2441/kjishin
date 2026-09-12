# -*- coding: utf-8 -*-
"""Kurage 地震ハザードマップ（名古屋版）— 内部の略称 kjishin

住所または地番を入れると、南海トラフ地震（最大クラス）で想定される
**震度**と**液状化の危険度**を返す。名古屋市の公開データにもとづく。

活断層マップ(kfault)との違い:
  kfault は「活断層の線の直上か、何メートル離れているか」＝震源の話。
  こちらは「その住所が何震度で、液状化するか」＝結果の話。住民が知りたいのは後者。

地番参考図を積んでいる理由:
  Kurageの防災GISはどれも「住所は町丁目の代表点だから地番で確認してください」と
  断ってきた。名古屋市は地番参考図をCC BYで公開しているので、市内に限っては
  **筆の形で判定**できる。敷地の中で想定が変わることまで見せられる。

構成: FastAPI + SQLite(R*Tree) + shapely。PostGISもDockerも要らない。
"""
import json
import os
from datetime import date

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.chiban import Chiban
from app.codes import LIQ_OUT_OF_SCOPE_NOTE, liq_rank, shindo_kaikyu
from app.lookup import Index

PORT = int(os.environ.get("KJISHIN_PORT", "18353"))
SITE = os.environ.get("KJISHIN_SITE_NAME", "Kurage 地震ハザードマップ（名古屋版）")
PUBLIC_BASE = os.environ.get("KJISHIN_PUBLIC_BASE", "https://kurage.exbridge.jp/kjishin.php").rstrip("/")
GSI = "https://msearch.gsi.go.jp/address-search/AddressSearch"
UA = {"User-Agent": "kjishin/1.0 (kurage.exbridge.jp)"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(ROOT, "app", "templates"))
app = FastAPI(title=SITE)
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "app", "static")), name="static")
INDEX = Index()
CHIBAN = Chiban()

LINKS = {
    # 買い切り版の商品ページ。デモから商品へ必ず導線を張る（全製品そろえる）
    "kappstore": "https://kappstore.exbridge.jp/app.php?id=51649180bea0fd57&ref=kjishin",
    "kfault": "https://kurage.exbridge.jp/kfault.php/",
    "kflood": "https://kurage.exbridge.jp/kflood.php/",
    "khazard": "https://kurage.exbridge.jp/khazard.php/",
    "krefuge": "https://kurage.exbridge.jp/krefuge.php/",
    "kmorido": "https://kurage.exbridge.jp/kmorido.php/",
    "city": "https://www.city.nagoya.jp/bousaiportal/hazardmap/1036428.html",
}

FAQ = [
    ("この震度はどういう想定ですか",
     "名古屋市が平成26年2月に公表した、南海トラフで発生する地震の被害想定にもとづきます。"
     "「千年に一度あるいはそれよりもっと発生頻度が低いが、仮に発生すれば甚大な被害をもたらす」"
     "最大クラスの地震を想定しています。よく起きる地震の想定ではありません。"),
    ("液状化の「判定対象外」とは何ですか",
     "液状化の判定の対象に入っていない、という意味です。「液状化しない」と判定されたわけではありません。"
     "名古屋市の東部（熱田台地・東部丘陵などの台地）に多く分布します。"
     "このサイトでは「判定対象外」と「危険度なし（PL=0）」を必ず分けて表示します。"),
    ("地番で調べると何が違いますか",
     "住所で調べると町丁目の代表点1点で判定しますが、地番で調べると土地の区画（筆）の形で判定します。"
     "筆にかかるメッシュを全部調べるので、敷地の中で想定が変わる場合もわかります。"
     "名古屋市が地番参考図を公開しているため、市内に限ってできます。"),
    ("この結果は建築や売買の根拠になりますか",
     "なりません。これは予測であり、公的な証明ではありません。"
     "とくに地番参考図は、名古屋市が「求積および権利関係等の確認の根拠となるものではない」と明記しています。"
     "建築や地盤改良の判断には、現地の地盤調査が必要です。"),
    ("活断層マップとは何が違いますか",
     "活断層マップは「活断層の線の直上か、何メートル離れているか」を示します。"
     "こちらは「その場所が何震度になり、液状化するか」を示します。震源の話と、揺れの結果の話の違いです。"),
]


@app.on_event("startup")
def _startup() -> None:
    INDEX.load()
    CHIBAN.load()


def geocode(q: str):
    r = requests.get(GSI, params={"q": q}, headers=UA, timeout=10)
    r.raise_for_status()
    items = r.json()
    if not items:
        return None
    top = items[0]
    lon, lat = top["geometry"]["coordinates"]
    return float(lat), float(lon), top["properties"].get("title") or q


def jsonld_for(path: str) -> str:
    graph = [{
        "@type": "WebSite", "@id": PUBLIC_BASE + "/#website", "name": SITE,
        "url": PUBLIC_BASE + "/", "inLanguage": "ja",
        "publisher": {"@type": "Organization", "name": "株式会社エクスブリッジ", "url": "https://exbridge.jp/"},
        "potentialAction": {"@type": "SearchAction",
                            "target": {"@type": "EntryPoint", "urlTemplate": PUBLIC_BASE + "/?q={search_term_string}"},
                            "query-input": "required name=search_term_string"},
    }]
    if path in ("/", "/about"):
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in FAQ]})
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)


def root_prefix(path: str) -> str:
    segs = [s for s in path.split("/") if s]
    depth = len(segs) if path.endswith("/") else max(0, len(segs) - 1)
    return "../" * depth


def page(request: Request, name: str, **kw):
    path = request.url.path
    kw.update(site=SITE, links=LINKS, year=date.today().year, faq=FAQ,
              count=INDEX.count, vintage=INDEX.vintage, attribution=INDEX.attribution,
              chiban_count=CHIBAN.count, chiban_vintage=CHIBAN.vintage,
              chiban_attribution=CHIBAN.attribution,
              public_base=PUBLIC_BASE, canonical=PUBLIC_BASE + path,
              root=root_prefix(path), jsonld=jsonld_for(path))
    return templates.TemplateResponse(request, name, kw)


def _judge(q: str):
    """住所か地番か。まず地番として引き、だめなら住所として引く。"""
    hit = CHIBAN.find(q)
    if hit:
        geom, label, chiban = hit
        return INDEX.check_parcel(geom, chiban, label), ""
    try:
        found = geocode(q)
    except requests.RequestException:
        return None, "住所検索に接続できませんでした。時間をおいて試してください。"
    if not found:
        return None, "住所が見つかりませんでした。区名から入れ直すか、地番（例: 中区栄3丁目1-1）で試してください。"
    lat, lon, title = found
    # 住所の代表点が筆の上に乗っていれば、その筆の形で判定する（精度が上がる）
    parcel = CHIBAN.at_point(lat, lon)
    if parcel:
        geom, label, chiban = parcel
        r = INDEX.check_parcel(geom, chiban, title)
        r.notes.insert(0, f"住所の位置にある土地の区画（{label} {chiban}）で判定しました。")
        return r, ""
    return INDEX.check(lat, lon, title), ""


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = ""):
    result, error = None, ""
    if q.strip():
        result, error = _judge(q.strip())
    return page(request, "index.html", q=q, result=result, error=error)


@app.get("/api/check")
def api_check(q: str = "", lat: float = None, lon: float = None):
    if lat is not None and lon is not None:
        r = INDEX.check(lat, lon, "")
    elif q.strip():
        r, err = _judge(q.strip())
        if not r:
            return JSONResponse({"error": err}, status_code=404)
    else:
        return JSONResponse({"error": "q または lat/lon が要ります"}, status_code=400)
    return {
        "address": r.address, "lat": r.lat, "lon": r.lon, "status": r.status,
        "parcel": r.parcel, "parcel_cells": r.parcel_cells,
        "shindo": r.shindo_label, "shindo_i_min": r.shindo_min, "shindo_i_max": r.shindo_max,
        "shindo_meaning": r.shindo_meaning,
        "liquefaction": r.liq_label, "pl": r.liq_pl, "settlement_m": r.settle_max,
        "mesh_count": len(r.cells), "notes": r.notes,
        "data_vintage": r.vintage, "attribution": r.attribution,
    }


@app.get("/api/mesh.geojson")
def mesh_geojson(bbox: str = "", layer: str = "shindo", limit: int = 6000):
    """表示範囲のメッシュを GeoJSON で返す。bbox は minlon,minlat,maxlon,maxlat。

    メッシュは緯度経度に平行な矩形なので、保存してある四隅から組み立てる。
    ポリゴンを持たない分、DBが小さく引くのも速い。
    """
    try:
        minx, miny, maxx, maxy = [float(v) for v in bbox.split(",")]
    except ValueError:
        return JSONResponse({"error": "bbox は minlon,minlat,maxlon,maxlat の形で渡してください"}, status_code=400)
    if INDEX._conn is None:
        INDEX.load()
    cur = INDEX._conn.execute(
        """SELECT m.code, m.minx, m.miny, m.maxx, m.maxy, m.i_s, m.pl
           FROM mesh_rtree t JOIN rtree_map r ON r.id = t.id JOIN mesh m ON m.code = r.code
           WHERE t.maxx >= ? AND t.minx <= ? AND t.maxy >= ? AND t.miny <= ?
           LIMIT ?""", (minx, maxx, miny, maxy, int(limit)))
    feats = []
    for r in cur:
        i_s, pl = r["i_s"], r["pl"]
        # 塗り分け用の段階。液状化は負を「判定対象外」として別扱いにする（0と混ぜない）
        if layer == "ekijoka":
            cls = -1 if pl < 0 else (0 if pl == 0 else 1 if pl <= 5 else 2 if pl <= 15 else 3)
            label = liq_rank(pl)[0]
        else:
            cls = 0 if i_s < 5.5 else 1 if i_s < 6.0 else 2 if i_s < 6.5 else 3
            label = shindo_kaikyu(i_s)
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [r["minx"], r["miny"]], [r["maxx"], r["miny"]],
                [r["maxx"], r["maxy"]], [r["minx"], r["maxy"]], [r["minx"], r["miny"]]]]},
            "properties": {"cls": cls, "label": label,
                           "i_s": round(i_s, 2), "pl": (None if pl < 0 else round(pl, 1))},
        })
    return {"type": "FeatureCollection", "features": feats, "truncated": len(feats) >= limit}


@app.get("/map/", response_class=HTMLResponse)
def map_page(request: Request, lat: float = None, lon: float = None, q: str = ""):
    if q.strip() and lat is None:
        hit = CHIBAN.find(q.strip())
        if hit:
            minx, miny, maxx, maxy = hit[0].bounds
            lat, lon = (miny + maxy) / 2, (minx + maxx) / 2
        else:
            try:
                found = geocode(q.strip())
                if found:
                    lat, lon = found[0], found[1]
            except requests.RequestException:
                pass
    return page(request, "map.html", lat=lat, lon=lon, q=q[:100])


@app.get("/healthz")
def healthz():
    return {"ok": True, "mesh": INDEX.count, "parcels": CHIBAN.count, "vintage": INDEX.vintage}


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return page(request, "about.html", out_of_scope=LIQ_OUT_OF_SCOPE_NOTE)


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return f"User-agent: *\nAllow: /\n\nSitemap: {PUBLIC_BASE}/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap():
    urls = "".join(f"<url><loc>{PUBLIC_BASE}{p}</loc><changefreq>monthly</changefreq></url>"
                   for p in ("/", "/map/", "/about"))
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?>'
                            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                    media_type="application/xml")


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms():
    return f"""# {SITE}

> 住所または地番を入れると、南海トラフ地震（最大クラス）で名古屋市のその場所に想定される
> **震度**と**液状化の危険度**を返すサイト。名古屋市の公開データ（CC BY 4.0）にもとづく。

## 活断層マップとの違い（よく混同される）
- 活断層マップ: 活断層の線の直上か、何メートル離れているか。**震源**の話。
- このサイト: その場所が何震度になり、液状化するか。**揺れの結果**の話。

## 収録
- 想定震度・液状化: {INDEX.count:,} メッシュ（約50m）／{INDEX.vintage}
- 地番参考図: {CHIBAN.count:,} 筆／{CHIBAN.vintage}
- {INDEX.attribution}
- {CHIBAN.attribution}
- 対象は**名古屋市内のみ**。市外は「範囲外」と返す。

## 大事な区別
- **液状化の「判定対象外」は「液状化しない」ではない。** 判定の対象に入っていないという意味で、
  名古屋市東部の台地に多い。危険度なし（PL=0）とは必ず分けて表示する。
- 地番で調べると、代表点1点ではなく**筆の形**で判定し、筆にかかるメッシュの幅で返す。


## 買い切り版
- 商品ページ: https://kappstore.exbridge.jp/app.php?id=51649180bea0fd57
- 税込55,000円。ソースコード（MIT）・データ取り込みスクリプト・設置手順書を同梱。自社サーバーで動かせる。

## 使い方
- 住所で調べる: {PUBLIC_BASE}/?q=<住所>
- 地番で調べる: {PUBLIC_BASE}/?q=<区名+町丁目+地番>\n- 地図で見る: {PUBLIC_BASE}/map/
- API: {PUBLIC_BASE}/api/check?q=<住所>
- MCP: 同梱の kjishin_mcp.py を AIエージェントに登録すると、住所判定をツールとして呼べる

## 注意
予測であって、実際にその震度になると決まっているわけではない。公的な証明ではない。
地番参考図は名古屋市が「求積および権利関係等の確認の根拠となるものではない」と明記している。
建築や地盤改良の判断には現地の地盤調査が要る。

## 関連
- 活断層マップ: {LINKS['kfault']}
- 洪水・内水ハザードマップ: {LINKS['kflood']}
- なごやハザードマップ（名古屋市）: {LINKS['city']}

運営: 株式会社エクスブリッジ https://exbridge.jp/
"""
