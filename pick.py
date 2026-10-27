import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import slot as S

RECHECK_DAYS = 7.0
FRESH_SHARE = 0.5
FIRST_SHARE = 0.5
FAIL_DROP = 3


def norm(v):
    return "".join(str(v).split())


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def rows(doc):
    """[[kw, vol, score], ...] -> [{'k','v','s'}]."""
    out = []
    for r in (doc or {}).get("k") or []:
        if isinstance(r, list) and r and isinstance(r[0], str) and r[0].strip():
            out.append({"k": r[0], "v": r[1] if len(r) > 1 else None, "s": float((r[2] if len(r) > 2 else 0) or 0)})
    return out


def _insert(rows_, extra):
    out = list(rows_)
    for e in sorted(extra, key=lambda x: -x["s"]):
        at = next((i for i, r in enumerate(out) if r["s"] < e["s"]), len(out))
        out.insert(at, e)
    return out


def select(queue, measured, when, recheck_days=RECHECK_DAYS, limit=S.A_RUN, first=None, fails=None,
           fresh_share=FRESH_SHARE, first_share=FIRST_SHARE):
    pri, taken = [], set()
    if first:
        in_queue = {norm(e["k"]): e for e in queue}
        top = int(limit * float(first_share))
        moved = []
        for e in first:
            k = norm(e["k"])
            if not k or k in taken:
                continue
            dropped = int((fails or {}).get(k) or 0) >= FAIL_DROP
            if (dropped or measured.get(k)) and k not in in_queue:
                in_queue[k] = e
                moved.append(e)
            if dropped or len(pri) >= top or measured.get(k):
                continue
            taken.add(k)
            pri.append(in_queue.get(k) or e)
        if moved:
            queue = _insert(queue, moved)
        if pri:
            rest = [e for e in queue if norm(e["k"]) not in taken]
            chosen, skipped = select(rest, measured, when, recheck_days, limit - len(pri), fresh_share=fresh_share)
            return pri + chosen, skipped
    cut = when - timedelta(days=float(recheck_days))
    fresh, due, skipped = [], [], 0
    for i, e in enumerate(queue):
        last = measured.get(norm(e["k"]))
        if not last:
            fresh.append((i, e))
        elif datetime.fromisoformat(last) > cut:
            skipped += 1
        else:
            due.append((i, e))
    n_fresh = min(len(fresh), int(round(limit * fresh_share)))
    n_due = min(len(due), limit - n_fresh)
    n_fresh = min(len(fresh), limit - n_due)
    chosen = sorted(fresh[:n_fresh] + due[:n_due], key=lambda x: x[0])
    return [e for _i, e in chosen], skipped


def read_jsonl(path):
    out = []
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    v = json.loads(line)
                except ValueError:
                    continue
                if isinstance(v, dict):
                    out.append(v)
    return out


def record(measured, recs, queue_doc, when, recheck_days=RECHECK_DAYS, gate=None, fails=None, first_keys=None):
    ok = [r for r in recs if r.get("성공")]
    blocks = sum(1 for r in recs if r.get("막힘"))
    circuit = any(r.get("연속차단") for r in recs)
    for r in ok:
        measured[norm(r["키워드"])] = r.get("때") or when.isoformat(timespec="seconds")
    if fails is not None:
        if first_keys is not None:
            for k in [k for k in fails if k not in first_keys]:
                del fails[k]
        for r in recs:
            k = norm(r.get("키워드", ""))
            if not k or (first_keys is not None and k not in first_keys):
                continue
            if r.get("성공"):
                fails.pop(k, None)
            elif not r.get("막힘"):
                fails[k] = int(fails.get(k) or 0) + 1
    vol = {norm(e["k"]): int(e["v"] or 0) for e in rows(queue_doc)}
    total = (queue_doc or {}).get("v") or 0
    cut = when - timedelta(days=float(recheck_days))
    live = {k for k, t in measured.items() if datetime.fromisoformat(t) > cut}

    def pct(x):
        return round(100.0 * x / total, 2) if total else None

    return {"t": when.isoformat(timespec="seconds"), "why": (gate or {}).get("why", ""),
            "tried": len(recs), "ok": len(ok), "failed": len(recs) - len(ok), "blocks": blocks, "stopped": circuit,
            "pct_run": pct(sum(vol.get(norm(r["키워드"]), 0) for r in ok)),
            "pct_window": pct(sum(vol.get(k, 0) for k in live)),
            "never": sum(1 for k in vol if k not in measured),
            "dropped": sum(1 for v in fails.values() if int(v or 0) >= FAIL_DROP) if fails is not None else None,
            "cap": (gate or {}).get("cap")}


def _out(path, **kv):
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(f"{k}={v}\n" for k, v in kv.items()))


def _now(t):
    return datetime.fromisoformat(t) if t else datetime.now(timezone.utc)


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--queue", required=True)
    g.add_argument("--first", default="")
    g.add_argument("--state-dir", required=True)
    g.add_argument("--recheck-days", type=float, default=RECHECK_DAYS)
    g.add_argument("--out", required=True)
    g.add_argument("--info", default="")
    g.add_argument("--github-output", default="")
    g.add_argument("--now", default="")
    b = sub.add_parser("gate-b")
    b.add_argument("--words", required=True, help="JSON list")
    b.add_argument("--state-dir", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--info", default="")
    b.add_argument("--github-output", default="")
    r = sub.add_parser("record")
    r.add_argument("--queue", default="")
    r.add_argument("--first", default="")
    r.add_argument("--state-dir", required=True)
    r.add_argument("--scan", required=True)
    r.add_argument("--recheck-days", type=float, default=RECHECK_DAYS)
    r.add_argument("--summary", required=True)
    r.add_argument("--gate", default="")
    r.add_argument("--outcome", default="")
    r.add_argument("--now", default="")
    a = p.parse_args(argv)
    state = Path(a.state_dir)
    state.mkdir(parents=True, exist_ok=True)

    if a.cmd in ("gate", "gate-b"):
        prev = S.load_prev(state)
        if a.cmd == "gate":
            when = _now(a.now)
            n, why = S.cap("a", os.environ, when, prev, S.A_RUN)
            measured = load(state / "m.json", {})
            fails = load(state / "ff.json", {})
            chosen, skipped = (select(rows(load(a.queue, {})), measured if isinstance(measured, dict) else {}, when,
                                      a.recheck_days, n, first=rows(load(a.first, {})) if a.first else None,
                                      fails=fails if isinstance(fails, dict) else {})
                               if n else ([], 0))
            words = [e["k"] for e in chosen]
        else:
            n, why = S.cap("b", os.environ, datetime.now(timezone.utc), prev, S.B_RUN)
            try:
                got = json.loads(a.words)
            except ValueError:
                got = []
            seen, words = set(), []
            for w in got if isinstance(got, list) else []:
                k = norm(w) if isinstance(w, str) else ""
                if k and k not in seen:
                    seen.add(k)
                    words.append(w.strip())
            words, skipped = words[:n], max(0, len(words) - n)
        go = bool(words)
        if n and not words:
            why += " - nothing to do"
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        if a.info:
            Path(a.info).write_text(json.dumps({"go": go, "why": why, "cap": len(words)}, ensure_ascii=False), encoding="utf-8")
        if go:
            S.save_prev(state, "open")
        print(f"{'go' if go else 'skip'}: {why} / {len(words)} picked / {skipped} skipped")
        _out(a.github_output, go="true" if go else "false", limit=len(words))
        return 0

    when = _now(a.now)
    recs = read_jsonl(a.scan)
    measured = load(state / "m.json", {})
    measured = measured if isinstance(measured, dict) else {}
    fails = load(state / "ff.json", {})
    fails = fails if isinstance(fails, dict) else {}
    first_keys = {norm(e["k"]) for e in rows(load(a.first, {}))} if a.first else set()
    summary = record(measured, recs, load(a.queue, {}) if a.queue else {}, when, a.recheck_days,
                     load(a.gate, {}) if a.gate else {}, fails, first_keys)
    (state / "m.json").write_text(json.dumps(measured, ensure_ascii=False), encoding="utf-8")
    (state / "ff.json").write_text(json.dumps(fails, ensure_ascii=False), encoding="utf-8")
    with (state / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    Path(a.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(a.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.outcome == "success" or (a.outcome == "failure" and summary["stopped"]):
        S.save_prev(state, "done", summary["stopped"])
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
