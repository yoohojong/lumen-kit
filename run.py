import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import slot as S

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

MAX_PER_RUN = 600   # 한 번 상한의 바깥 울타리 — 실제 상한은 slot.hard_cap(a 450 · b 600) · 10-28 450에 걸려 600 회차가 멈춘 일
STOP_AFTER_BLOCKS = 5
CAFE_POST = re.compile(r"cafe\.naver\.com/(?:[A-Za-z0-9_-]+/\d+|ca-fe/cafes/\d+/articles/\d+)")
BLOCK_PREFIXES = ("rate limited", "network error", "unexpected status 403")


class NotOnRunner(RuntimeError):
    pass


def require_runner():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise NotOnRunner("runner only")


class UnreadablePage(Exception):
    pass


def make_reader(P, BeautifulSoup):
    def owner(url):
        key = P._owner_key(url) if url else ()
        return key[1] if len(key) == 2 else ""

    def read(html):
        soup = BeautifulSoup(html or "", "lxml")
        boxes = soup.select(".desktop_mode.api_subject_bx, .fds-default-mode.api_subject_bx")
        if not boxes:
            raise UnreadablePage("no boxes")
        rows, ab, box_no = [], 0, 0
        for box in boxes:
            names = P._collect_source_names(box)
            h2 = box.find("h2")
            if h2 is None:
                if not any("cafe.naver.com" in a.get("href", "") for a in box.find_all("a", href=True)):
                    continue
                url = P._extract_main_link(box)
                if not url:
                    continue
                ab += 1
                kind = P._classify_item_url(url)
                name = names.get(P._owner_key(url), "") or (next(iter(names.values())) if len(names) == 1 else "")
                rows.append({"칸": "AB", "칸번호": "AB", "칸이름": "", "카드": ab, "종류": kind, "주소": url,
                             "주인": owner(url) if kind == "cafe" else "",
                             "카페이름": name if kind == "cafe" else "", "제목": P._title_for_url(box, url)})
                continue
            h2_text = h2.get_text(strip=True)
            if any(s in h2_text for s in P._POPULAR_SKIP_PATTERNS):
                continue
            if any(s in h2_text for s in P._NON_SLOT_BLOCK_PATTERNS):
                continue
            area = "인기글" if "인기글" in h2_text else "스마트블록"
            if area == "스마트블록" and any(s in h2_text for s in P._SMART_BLOCK_SKIP_PATTERNS):
                continue
            box_no += 1
            cards, _dom_ok = P._items_by_card(box)
            for c, urls in enumerate(cards, start=1):
                for url in urls:
                    kind = P._classify_item_url(url)
                    rows.append({"칸": area, "칸번호": box_no, "칸이름": h2_text, "카드": c, "종류": kind,
                                 "주소": url, "주인": owner(url) if kind == "cafe" else "",
                                 "카페이름": names.get(P._owner_key(url), "") if kind == "cafe" else "",
                                 "제목": P._title_for_url(box, url)})
        return rows
    return read


def scan_one(keyword, fetch, read):
    html = fetch(keyword)
    rows = read(html)
    for order, r in enumerate(rows, start=1):
        r["차례"] = order
    return html, rows


def already_done(out_path):
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("성공"):
                done.add(rec.get("키워드"))
    return done


def run(keywords, fetch, read, out_path, is_circuit_open=None, *, limit=MAX_PER_RUN, is_block=None, resume=True,
        brand=None):
    limit = max(0, min(int(limit), MAX_PER_RUN))
    done = failed = blocks = 0
    why = "end"
    skip = already_done(out_path) if resume else set()
    todo = [k for k in keywords if k not in skip]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        for kw in todo[:limit]:
            rec = {"키워드": kw, "때": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            try:
                html, rows = scan_one(kw, fetch, read)
                blocks = 0
                rec.update({"성공": True, "화면길이": len(html), "칸": rows})
                if brand is not None:
                    try:
                        rec["브랜드칸"] = brand(html)
                    except Exception as e:
                        rec["브랜드칸"] = {"못읽음": type(e).__name__}
                if not any(r["종류"] == "cafe" for r in rows) and CAFE_POST.search(html or ""):
                    rec["카페글못읽음"] = True
                done += 1
            except NotOnRunner:
                raise
            except Exception as e:
                if is_circuit_open and is_circuit_open(e):
                    why = "blocked"
                    f.write(json.dumps({**rec, "성공": False, "까닭": why, "막힘": True, "연속차단": True},
                                       ensure_ascii=False) + "\n")
                    failed += 1
                    break
                rec.update({"성공": False, "까닭": type(e).__name__ + ": " + str(e)[:120]})
                if is_block and is_block(e):
                    rec["막힘"] = True
                    blocks += 1
                    if blocks >= STOP_AFTER_BLOCKS:
                        why = "blocked"
                        rec["연속차단"] = True
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        failed += 1
                        break
                failed += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
    if len(todo) > limit and why == "end":
        why = f"cap {limit}"
    return done, failed, why


def is_block(e, CrawlerError):
    return isinstance(e, CrawlerError) and str(e).startswith(BLOCK_PREFIXES)


def make_fetch(crawler, CrawlerError):
    def fetch(keyword):
        require_runner()
        try:
            return crawler.fetch_search(keyword)
        except CrawlerError as e:
            if str(e).startswith("unexpected status 403") and getattr(crawler, "slowdown", None):
                crawler.slowdown.on_block_detected()
            raise
    return fetch


def steady_slowdown(SlowdownController, base):
    sd = SlowdownController(base=base, max_=60.0)
    sd.ACCELERATION_FLOOR = 1.0
    return sd


def exit_code(done, failed, why):
    total = done + failed
    return 0 if done and why == "end" and failed * 10 < total else 1


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--targets", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--base", type=float, default=5.0)
    p.add_argument("--max", type=int, default=MAX_PER_RUN)
    a = p.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print("runner only", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib.net import Crawler, CrawlerError, SlowdownController, CircuitBreakerOpen
    from lib import page as P
    from lib import bc
    from bs4 import BeautifulSoup

    require_runner()
    limit = S.hard_cap(os.environ, a.max)
    if not limit:
        print("cap 0", file=sys.stderr)
        return 2
    crawler = Crawler(slowdown=steady_slowdown(SlowdownController, a.base))
    crawler.warmup()
    keywords = list(dict.fromkeys(json.loads(Path(a.targets).read_text(encoding="utf-8"))))
    done, failed, why = run(keywords, make_fetch(crawler, CrawlerError), make_reader(P, BeautifulSoup), Path(a.out),
                            lambda e: isinstance(e, CircuitBreakerOpen), limit=limit,
                            is_block=lambda e: is_block(e, CrawlerError), brand=bc.read_brand_ads)
    print(f"{len(keywords)} / ok {done} / failed {failed} / {why}")
    return exit_code(done, failed, why)


if __name__ == "__main__":
    raise SystemExit(main())
