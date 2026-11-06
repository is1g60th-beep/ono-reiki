#!/usr/bin/env python3
"""大野町例規集を巡回し、例規一覧・本文・変更履歴を data/ に書き出す。

- 体系目次 (reiki_taikei/r_taikei_*.html) をたどって例規の一覧を集める
- 各例規の本文 (reiki_honbun/*.html) を取得してテキスト化する
- 前回の data/reiki.json と比べ、追加・改正・廃止を data/changes.json に記録する

標準ライブラリだけで動く。GitHub Actions から週1回実行する想定。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BASE = "https://www.town-ono.jp/reiki_int/"
START = BASE + "reiki_taikei/r_taikei_01_01.html"
UA = "ono-reiki-search/1.0 (+https://github.com/is1g60th-beep/ono-reiki)"
WAIT = 0.6            # 町のサーバーに負担をかけないよう、1リクエストごとに待つ秒数
MAX_TAIKEI = 400
MAX_CHANGES = 300
JST = dt.timezone(dt.timedelta(hours=9))

ERA = r"(?:明治|大正|昭和|平成|令和)(?:元|\d+)年\d+月\d+日"
RE_HONBUN = re.compile(r"reiki_honbun/([A-Za-z0-9]+)\.html")
RE_TAIKEI = re.compile(r"r_taikei_[0-9_]+\.html")
RE_KIND = re.compile(r"(条例|規則|告示|訓令|規程|要綱|種別なし|第\d+号)")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(url: str, tries: int = 3) -> str | None:
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                cs = r.headers.get_content_charset()
            time.sleep(WAIT)
            if not cs:
                m = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', raw[:3000])
                cs = m.group(1).decode() if m else None
            for enc in [cs, "utf-8", "cp932", "euc-jp"]:
                if not enc:
                    continue
                enc = {"shift_jis": "cp932", "sjis": "cp932", "x-sjis": "cp932"}.get(enc.lower(), enc)
                try:
                    return raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            log(f"  HTTP {e.code} {url} (retry {n + 1})")
        except Exception as e:  # noqa: BLE001
            log(f"  {type(e).__name__} {url} (retry {n + 1})")
        time.sleep(3 * (n + 1))
    return None


def clean(s: str) -> str:
    return re.sub(r"[ \t　\xa0]+", " ", s).strip()


class TaikeiParser(HTMLParser):
    """体系目次ページから、例規の行と他の目次ページへのリンクを取り出す。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.rows: list[dict] = []
        self.title = ""
        self._in_title = False
        self._row: dict | None = None
        self._cell: list[str] | None = None
        self._a: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "tr":
            self._row = {"cells": [], "links": []}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
            self._a = {"href": a["href"], "text": []}

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._a is not None:
            if self._row is not None:
                self._row["links"].append((self._a["href"], clean("".join(self._a["text"]))))
            self._a = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row["cells"].append(clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._cell is not None:
            self._cell.append(data)
        if self._a is not None:
            self._a["text"].append(data)


class TextParser(HTMLParser):
    """本文ページを、段落ごとに改行したテキストにする。"""

    SKIP = {"script", "style", "noscript", "head", "nav", "header", "footer", "button", "select", "form"}
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "dt", "dd", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.frames: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("frame", "iframe"):
            src = dict(attrs).get("src")
            if src:
                self.frames.append(src)
        if tag in self.BLOCK or tag == "td":
            self.out.append("\n" if tag != "td" else " ")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.out.append("\n")
        elif tag in ("frame", "iframe"):
            src = dict(attrs).get("src")
            if src:
                self.frames.append(src)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)

    def text(self) -> str:
        lines = [clean(x) for x in "".join(self.out).split("\n")]
        return "\n".join(x for x in lines if x)


def crawl_index(prev_cat: dict[str, str]) -> list[dict]:
    seen, queue, items, order = set(), deque([START]), {}, []
    while queue and len(seen) < MAX_TAIKEI:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        html = fetch(url)
        if html is None:
            log(f"  skip {url}")
            continue
        p = TaikeiParser()
        p.feed(html)
        for href in p.links:
            if RE_TAIKEI.search(href):
                nxt = urljoin(url, href).split("#")[0]
                if nxt not in seen:
                    queue.append(nxt)
        if len(seen) == 1:
            log("  先頭ページのリンク例: " + ", ".join(p.links[:40]))
        page_rows = []
        for row in p.rows:
            hit = next(((h, t) for h, t in row["links"] if RE_HONBUN.search(h)), None)
            if not hit:
                continue
            rid = RE_HONBUN.search(hit[0]).group(1)
            name = hit[1] or next((c for c in row["cells"] if c), "")
            others = [c.replace("◆", "").strip() for c in row["cells"] if c and c != hit[1]]
            date = next((re.search(ERA, c).group(0) for c in others if re.search(ERA, c)), "")
            no = next((c for c in reversed(others) if RE_KIND.search(c) and not re.search(ERA, c)), "")
            page_rows.append({"id": rid, "name": name, "date": date, "no": no})
        if not page_rows:
            continue
        # 分類（編/章）は前回の値を引き継ぐ。新しい例規は同じページの既知の例規に合わせる
        known = Counter(prev_cat[r["id"]] for r in page_rows if r["id"] in prev_cat)
        fallback = known.most_common(1)[0][0] if known else clean(re.sub(r"[|｜].*$", "", p.title)) or "未分類"
        for r in page_rows:
            if r["id"] in items:
                continue
            r["cat"] = prev_cat.get(r["id"], fallback)
            items[r["id"]] = r
            order.append(r["id"])
        log(f"  {url.rsplit('/', 1)[-1]}: {len(page_rows)}件")
    log(f"目次 {len(seen)}ページ, 例規 {len(items)}件")
    return [items[i] for i in order]


def fetch_body(rid: str) -> str | None:
    url = f"{BASE}reiki_honbun/{rid}.html"
    html = fetch(url)
    if html is None:
        return None
    p = TextParser()
    p.feed(html)
    text = p.text()
    if len(text) < 80 and p.frames:  # フレーム構成なら中身のページを読む
        parts = []
        for src in p.frames:
            sub = fetch(urljoin(url, src))
            if sub:
                q = TextParser()
                q.feed(sub)
                parts.append(q.text())
        text = "\n".join(t for t in parts if t)
    return text


def strip_boilerplate(bodies: dict[str, str]) -> dict[str, str]:
    """ほぼすべてのページに出てくる行（ナビゲーションなど）を取り除く。"""
    if len(bodies) < 20:
        return bodies
    freq = Counter()
    for t in bodies.values():
        freq.update(set(t.split("\n")))
    common = {line for line, n in freq.items() if n >= len(bodies) * 0.5}
    if common:
        log(f"共通行を除去: {sorted(common)[:15]}")
    return {k: "\n".join(x for x in t.split("\n") if x not in common) for k, t in bodies.items()}


def digest(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()[:16]


def load(name: str, default):
    p = DATA / name
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default


def dump(name: str, obj, compact=False):
    DATA.mkdir(exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":") if compact else None, indent=None if compact else 1)
    (DATA / name).write_text(txt + "\n", encoding="utf-8")


def main() -> int:
    prev = load("reiki.json", {"items": []})
    prev_items = {r["id"]: r for r in prev.get("items", [])}
    prev_full = load("fulltext.json", {})
    changes = load("changes.json", [])
    prev_cat = {k: v["cat"] for k, v in prev_items.items() if v.get("cat")}

    items = crawl_index(prev_cat)
    if len(items) < max(50, len(prev_items) * 0.7):
        log(f"取得件数が少なすぎます（{len(items)}件 / 前回 {len(prev_items)}件）。サイトの不調か構成変更の可能性があるため、更新を中止します。")
        return 1

    bodies, failed = {}, []
    for n, r in enumerate(items, 1):
        t = fetch_body(r["id"])
        if t:
            bodies[r["id"]] = t
        else:
            failed.append(r["id"])
        if n % 50 == 0:
            log(f"  本文 {n}/{len(items)}")
    bodies = strip_boilerplate(bodies)
    for rid in failed:  # 取れなかった本文は前回の値を使う
        if rid in prev_full:
            bodies[rid] = prev_full[rid]
    log(f"本文 {len(bodies)}件取得, 失敗 {len(failed)}件")

    today = dt.datetime.now(JST).date().isoformat()
    baseline = not any(v.get("hash") for v in prev_items.values())
    new_changes = []
    for r in items:
        if r["id"] in bodies:
            r["hash"] = digest(bodies[r["id"]])
        elif r["id"] in prev_items:
            r["hash"] = prev_items[r["id"]].get("hash", "")
        old = prev_items.get(r["id"])
        if baseline:
            continue
        if old is None:
            new_changes.append({"date": today, "type": "追加", "id": r["id"], "name": r["name"], "no": r["no"]})
        elif r.get("hash") and old.get("hash") and r["hash"] != old["hash"]:
            new_changes.append({"date": today, "type": "改正", "id": r["id"], "name": r["name"], "no": r["no"]})
    if not baseline:
        ids = {r["id"] for r in items}
        for rid, old in prev_items.items():
            if rid not in ids:
                new_changes.append({"date": today, "type": "廃止・削除", "id": rid, "name": old["name"], "no": old.get("no", "")})

    if new_changes:
        log(f"変更 {len(new_changes)}件: " + ", ".join(f"{c['type']} {c['name']}" for c in new_changes[:20]))
    changes = (new_changes + changes)[:MAX_CHANGES]

    dump("reiki.json", {
        "checked": today,
        "baseline": prev.get("baseline") or (today if baseline else None),
        "source": START,
        "count": len(items),
        "items": items,
    })
    dump("fulltext.json", {r["id"]: bodies.get(r["id"], "") for r in items}, compact=True)
    dump("changes.json", changes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
