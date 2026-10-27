import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
A_RUN, A_SMALL = 450, 30
B_RUN, B_SMALL, B_PER_DAY = 200, 30, 4
WINDOW_DEFAULT, WINDOW_MAX = 2.0, 7.0
EARLY_MINUTES = 10
PREV = "prev.json"
EVENTS_A = ("schedule", "workflow_dispatch")
SLOTS = ("15 15 * * *", "15 19 * * *", "15 23 * * *", "15 3 * * *", "15 7 * * *", "15 11 * * *")
SEED_HOURS = 24
CRON = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$")


def slot_time(cron, now):
    m = CRON.match(cron or "")
    if not m:
        return None
    minute, hour = int(m.group(1)), int(m.group(2))
    if minute > 59 or hour > 23:
        return None
    now = now.astimezone(UTC)
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return t - timedelta(days=1) if t > now + timedelta(minutes=EARLY_MINUTES) else t


def slot_key(env, now):
    s = slot_time(env.get("SLOT_CRON", ""), now)
    return s.strftime("%Y-%m-%dT%H:%MZ") if s else ""


def window_hours(env):
    try:
        v = float(env.get("SLOT_WINDOW_HOURS") or "")
    except ValueError:
        return WINDOW_DEFAULT
    return v if 0 < v <= WINDOW_MAX else WINDOW_DEFAULT


def first_try(env):
    return str(env.get("GITHUB_RUN_ATTEMPT") or "1") == "1"


def mode_a(env, now):
    ev = env.get("GITHUB_EVENT_NAME")
    if ev not in EVENTS_A:
        return "", f"event {ev or '-'}"
    if not first_try(env):
        return "", f"attempt {env.get('GITHUB_RUN_ATTEMPT')}"
    if env.get("SLOT_CRON", "") not in SLOTS:
        return "", f"bad slot '{env.get('SLOT_CRON', '')}'"
    s = slot_time(env.get("SLOT_CRON", ""), now)
    if s is None:
        return "", f"bad slot '{env.get('SLOT_CRON', '')}'"
    late = now.astimezone(UTC) - s
    if late >= timedelta(hours=window_hours(env)):
        return "", f"slot {s:%H:%M}Z late {late.total_seconds() / 3600:.1f}h"
    return "ok", f"slot {s:%Y-%m-%d %H:%M}Z"


def mode_b(env):
    ev = env.get("GITHUB_EVENT_NAME")
    if ev != "workflow_dispatch":
        return "", f"event {ev or '-'}"
    if not first_try(env):
        return "", f"attempt {env.get('GITHUB_RUN_ATTEMPT')}"
    return "ok", "dispatch"


def _when(t):
    try:
        v = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return v if v.tzinfo else None
    except ValueError:
        return None


def gh_runner(args):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", timeout=120)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def api(runner, path):
    code, out = runner(["api", path])
    if code:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _runs(runner, repo, workflow, since, events):
    j = api(runner, f"repos/{repo}/actions/workflows/{workflow}/runs?created=%3E%3D{since:%Y-%m-%dT%H:%M:%SZ}&per_page=100")
    runs = j.get("workflow_runs") if isinstance(j, dict) else None
    if not isinstance(runs, list) or not isinstance(j.get("total_count"), int) or j["total_count"] > len(runs):
        return None
    out = []
    for r in runs:
        if not isinstance(r, dict) or not str(r.get("id")).isdigit():
            return None
        t = _when(r.get("created_at"))
        if t is None:
            return None
        if r.get("event") in events:
            out.append((t, int(r["id"])))
    return out


def claim_a(env, now, runner, repo, workflow):
    """-> (slot, why, unread)."""
    ok, why = mode_a(env, now)
    if not ok:
        return "", why, False
    s = slot_time(env.get("SLOT_CRON", ""), now)
    start, end = s - timedelta(minutes=EARLY_MINUTES), s + timedelta(hours=window_hours(env))
    seen = _runs(runner, repo, workflow, start, EVENTS_A)
    me = int(env["GITHUB_RUN_ID"]) if str(env.get("GITHUB_RUN_ID") or "").isdigit() else None
    mine = next((t for t, i in seen or [] if i == me), None)
    if seen is None or mine is None:
        return "", f"{why} - run list unread", True
    first = [str(i) for t, i in seen if i != me and start <= t < end and (t, i) < (mine, me)]
    if first:
        return "", f"{why} - taken by {', '.join(first)}", False
    return slot_key(env, now), why, False


def claim_b(env, now, runner, repo, workflow):
    """-> (ok, why, unread). At most B_PER_DAY dispatch runs per KST day."""
    ok, why = mode_b(env)
    if not ok:
        return "", why, False
    day = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    seen = _runs(runner, repo, workflow, day.astimezone(UTC), ("workflow_dispatch",))
    me = int(env["GITHUB_RUN_ID"]) if str(env.get("GITHUB_RUN_ID") or "").isdigit() else None
    mine = next((t for t, i in seen or [] if i == me), None)
    if seen is None or mine is None:
        return "", f"{why} - run list unread", True
    before = sum(1 for t, i in seen if i != me and (t, i) < (mine, me))
    if before >= B_PER_DAY:
        return "", f"{why} - {before} runs today", False
    return "ok", f"{why} #{before + 1} today", False


def load_prev(state_dir):
    try:
        v = json.loads((Path(state_dir) / PREV).read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def save_prev(state_dir, result, blocked=None):
    p = Path(state_dir) / PREV
    p.parent.mkdir(parents=True, exist_ok=True)
    v = {"r": result, "t": datetime.now(UTC).isoformat(timespec="seconds"), "id": os.environ.get("GITHUB_RUN_ID", "")}
    if result == "done":
        v["blocked"] = bool(blocked)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(v), encoding="utf-8")
    os.replace(tmp, p)


def prev_bad(prev):
    if prev is None:
        return "no prev"
    if prev.get("r") != "done":
        return "prev unfinished"
    if prev.get("blocked") is not False:
        return "prev blocked"
    return ""


def _cap(want, top):
    try:
        return max(0, min(int(want), top))
    except (TypeError, ValueError):
        return top


def cap(kind, env, now, prev, want):
    """-> (n, why). kind 'a' needs SLOT_OK == slot_key, kind 'b' needs CLAIM_OK."""
    if kind == "a":
        ok, why = mode_a(env, now)
        if ok and env.get("SLOT_OK") != slot_key(env, now):
            ok, why = "", why + " - not claimed"
        big, small = A_RUN, A_SMALL
    else:
        ok, why = mode_b(env)
        if ok and env.get("CLAIM_OK") != "ok":
            ok, why = "", why + " - not claimed"
        big, small = B_RUN, B_SMALL
    if not ok:
        return 0, why
    bad = prev_bad(prev)
    top = small if bad else big
    return _cap(want, top), f"{why}{' / ' + bad if bad else ''} -> max {top}"


def hard_cap(env, want):
    ev, ref = env.get("GITHUB_EVENT_NAME"), env.get("GITHUB_WORKFLOW_REF", "")
    if not first_try(env) or ev not in EVENTS_A:
        return 0
    if "/b.yml@" in ref:
        return _cap(want, B_RUN) if ev == "workflow_dispatch" else 0
    return _cap(want, A_RUN)


def _is_time(v):
    return isinstance(v, str) and _when(v) is not None


SHAPES = {
    "m.json": lambda v: isinstance(v, dict) and all(_is_time(x) for x in v.values()),
    "ff.json": lambda v: isinstance(v, dict) and all(type(x) is int for x in v.values()),
    PREV: lambda v: isinstance(v, dict) and (v.get("r") == "open" or (v.get("r") == "done" and isinstance(v.get("blocked"), bool))),
}


def readable(p):
    p = Path(p)
    try:
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".json":
            return SHAPES.get(p.name, lambda v: isinstance(v, dict))(json.loads(text))
        if p.suffix == ".jsonl":
            return all(isinstance(json.loads(x), dict) for x in text.split("\n")[:-1] if x.strip())
        return True
    except (ValueError, OSError, UnicodeDecodeError):
        return False


def whole(folder, need):
    f = Path(folder)
    return all((f / n).exists() for n in need) and all(
        readable(p) for p in f.iterdir() if p.is_file() and p.suffix in (".json", ".jsonl"))


def restore(repo, name, dest, need, runner=None, tries=3, seed=None):
    got = _restore(repo, name, dest, need, runner, tries)
    if got == "none" and seed and Path(seed).is_dir() and whole(seed, need):
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(seed, dest)
        t = _when((load_prev(dest) or {}).get("t"))
        if t is None or datetime.now(UTC) - t > timedelta(hours=SEED_HOURS):
            (Path(dest) / PREV).unlink(missing_ok=True)
        return "seed"
    return got


def _restore(repo, name, dest, need, runner=None, tries=3):
    runner = runner or gh_runner
    dest = Path(dest)
    if dest.exists() and whole(dest, need):
        return "cache"
    a = api(runner, f"repos/{repo}/actions/artifacts?name={name}&per_page=30") or {}
    live = sorted((x for x in a.get("artifacts") or [] if not x.get("expired")),
                  key=lambda x: x.get("created_at") or "", reverse=True)
    for art in live[:tries]:
        rid = (art.get("workflow_run") or {}).get("id")
        tmp = Path(tempfile.mkdtemp(prefix="st_"))
        try:
            code, _o = runner(["run", "download", str(rid), "-R", repo, "-n", name, "-D", str(tmp)])
            if code or not whole(tmp, need):
                continue
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(tmp, dest)
            return "artifact"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if dest.exists():
        for p in dest.iterdir():
            if p.is_file() and p.suffix in (".json", ".jsonl") and not readable(p):
                p.unlink()
        if any((dest / n).exists() for n in need):
            return "partial"
    return "none"


def _out(path, **kv):
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(f"{k}={v}\n" for k, v in kv.items()))


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("kind", choices=["a", "b"])
    c.add_argument("--repo", required=True)
    c.add_argument("--workflow", required=True)
    c.add_argument("--github-output", default="")
    c.add_argument("--now", default="")
    s = sub.add_parser("restore")
    s.add_argument("--repo", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--dir", required=True)
    s.add_argument("--need", nargs="+", required=True)
    s.add_argument("--seed", default="")
    a = p.parse_args(argv)
    if a.cmd == "claim":
        now = datetime.fromisoformat(a.now) if a.now else datetime.now(UTC)
        fn = claim_a if a.kind == "a" else claim_b
        got, why, unread = fn(os.environ, now, gh_runner, a.repo, a.workflow)
        print(f"{'go' if got else 'skip'}: {why}")
        _out(a.github_output, slot=got)
        return 1 if unread else 0
    print("restore:", restore(a.repo, a.name, a.dir, a.need, seed=a.seed or None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
