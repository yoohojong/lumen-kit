import argparse
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

URL = "https://search.naver.com/search.naver"
# (target gap seconds start-to-start, requests). 0.0 = no wait (bounded by response time).
STAGES = [(7.0, 60), (5.0, 80), (3.5, 100), (2.5, 120), (1.8, 150), (1.2, 200), (0.8, 250), (0.0, 5000)]
# last stage has no planned end — it runs until the first block or the deadline
JITTER = 0.15
SOFT = ("short", "no_result_boxes")   # could be the keyword, not the IP -> confirm once with a known-good keyword


def classify(status, text, final_url, block_signs):
    """'' = normal. Anything else = block (stop)."""
    if status == 429:
        return "429"
    if status == 403:
        return "403"
    if status != 200:
        return f"status_{status}"
    if any(s in text for s in block_signs):
        return "block_text"
    host = urlparse(final_url or "").netloc
    if host and host != "search.naver.com":
        return "redirect_" + host
    if len(text) < 500:
        return "short"
    if "api_subject_bx" not in text:
        return "no_result_boxes"
    return ""


def run(keywords, fetch, out_path, *, stages=STAGES, deadline_s=3000, clock=time.monotonic, sleep=time.sleep,
        rng=None, block_signs=(), save_block=None, confirm_kw=None):
    """Send requests stage by stage; stop at the first block or at the deadline. Returns summary dict."""
    rng = rng or random.Random(0)
    t0 = clock()
    last = None
    i = 0
    confirms = 0
    stop, first_block = "all stages done", None
    per = []
    lengths = []
    with open(out_path, "a", encoding="utf-8") as f:
        for si, (gap, count) in enumerate(stages, start=1):
            st = {"stage": si, "gap_target_s": gap, "planned": count, "sent": 0, "ok": 0, "gaps": [], "elapsed": [],
                  "thin": 0}
            per.append(st)
            for _ in range(count):
                if clock() - t0 >= deadline_s:
                    stop = f"deadline {deadline_s}s"
                    break
                if i >= len(keywords):
                    stop = "keywords exhausted"
                    break
                if last is not None and gap > 0:
                    want = gap * rng.uniform(1 - JITTER, 1 + JITTER)
                    wait = last + want - clock()
                    if wait > 0:
                        sleep(wait)
                start = clock()
                if last is not None:
                    st["gaps"].append(start - last)
                last = start
                i += 1
                st["sent"] += 1
                rec = {"n": i, "stage": si, "gap_target_s": gap, "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "since_start_s": round(start - t0, 2)}
                try:
                    status, text, final_url = fetch(keywords[i - 1])
                    kind = classify(status, text, final_url, block_signs)
                    rec.update({"status": status, "len": len(text)})
                except Exception as e:  # network failure counts as block (same as production is_block)
                    kind = "network_" + type(e).__name__
                    rec.update({"status": None, "len": 0, "error": str(e)[:160]})
                rec["elapsed_s"] = round(clock() - start, 3)
                st["elapsed"].append(rec["elapsed_s"])
                if kind in SOFT:
                    # empty / short page: may be the keyword itself. Re-check one known-good keyword after 10s.
                    sleep(10)
                    confirms += 1
                    try:
                        c_status, c_text, c_url = fetch(confirm_kw or keywords[0])
                        c_kind = classify(c_status, c_text, c_url, block_signs)
                    except Exception as e:
                        c_kind = "network_" + type(e).__name__
                    rec["confirm"] = c_kind or "ok"
                    last = clock()
                    if not c_kind:
                        rec["soft"] = kind      # keyword-only empty page — not a block
                        st["soft"] = st.get("soft", 0) + 1
                        kind = ""
                    else:
                        kind = kind + "+confirm_" + c_kind
                if kind:
                    if save_block:
                        save_block(rec, kind)
                    rec["block"] = kind
                    first_block = {"n": i, "stage": si, "gap_target_s": gap, "kind": kind,
                                   "since_start_s": rec["since_start_s"], "status": rec.get("status")}
                    stop = "first block"
                else:
                    st["ok"] += 1
                    if len(lengths) >= 20 and rec["len"] < 0.3 * statistics.median(lengths):
                        rec["thin"] = True       # warning only — not a block
                        st["thin"] += 1
                    lengths.append(rec["len"])
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if first_block:
                    break
            if first_block or stop.startswith(("deadline", "keywords")):
                break
    stages_out = []
    for st in per:
        g, e = st.pop("gaps"), st.pop("elapsed")
        st["gap_mean_s"] = round(statistics.mean(g), 3) if g else None
        st["gap_min_s"] = round(min(g), 3) if g else None
        st["resp_mean_s"] = round(statistics.mean(e), 3) if e else None
        stages_out.append(st)
    total_s = clock() - t0
    return {"sent": i, "confirm_sent": confirms, "ok": sum(s["ok"] for s in stages_out), "blocks": 1 if first_block else 0,
            "first_block": first_block, "stop": stop, "duration_s": round(total_s, 1),
            "per_hour_rate": round(i / total_s * 3600, 1) if total_s > 0 else None, "stages": stages_out}


def load_keywords(path, seed):
    k = json.loads(Path(path).read_text(encoding="utf-8"))["k"]
    words = list(dict.fromkeys(r[0] for r in k if r and r[0]))
    random.Random(seed).shuffle(words)
    return words


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--queue", default="data/q.json")
    p.add_argument("--deadline-min", default="50")
    a = p.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print("runner only", file=sys.stderr)
        return 2
    try:
        deadline_min = min(52.0, max(1.0, float(a.deadline_min)))
    except ValueError:
        deadline_min = 50.0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from curl_cffi import requests
    from lib.net import UA, PROFILES, HEADERS, BLOCK_SIGNS

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    profile = random.choice(PROFILES)
    session = requests.Session(impersonate=profile)
    try:
        session.get("https://www.naver.com/", headers={**HEADERS, "User-Agent": random.choice(UA)}, timeout=10)
    except Exception:
        pass

    def fetch(keyword):
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise SystemExit(2)
        r = session.get(URL, params={"query": keyword}, headers={**HEADERS, "User-Agent": random.choice(UA)},
                        timeout=15)
        last_page["text"] = r.text
        return r.status_code, r.text, str(r.url)

    last_page = {"text": ""}

    def save_block(rec, kind):
        (out / "block.html").write_text(last_page["text"][:200000], encoding="utf-8")

    seed = int(os.environ.get("GITHUB_RUN_ID") or 0)
    keywords = load_keywords(a.queue, seed)
    top = json.loads(Path(a.queue).read_text(encoding="utf-8"))["k"][0][0]   # highest search volume — always has results
    s = run(keywords, fetch, out / "p.jsonl", deadline_s=deadline_min * 60, rng=random.Random(seed),
            block_signs=BLOCK_SIGNS, save_block=save_block, confirm_kw=top)
    s.update({"run_id": os.environ.get("GITHUB_RUN_ID"), "profile": profile, "deadline_min": deadline_min,
              "keywords_available": len(keywords)})
    (out / "sum.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in s.items() if k != "stages"}, ensure_ascii=False))
    for st in s["stages"]:
        print(json.dumps(st, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
