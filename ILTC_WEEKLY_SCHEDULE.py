"""
ILTC WEEKLY SCHEDULE

Pick accounts, and for each one it reads who works Grave / Afternoon /
Main every day of the week off the ILTC scheduling site (open in Chrome),
then makes Infloww's Shift schedule match: creates the missing shifts as
weekly repeats and deletes any shift that is not on the site.

Preview runs the whole read-and-compare without changing anything.

Same two-process layout as UPSELL SCRIPT INFLOWW: this GUI process never
imports uiautomation/comtypes; all of that lives in schedule_worker.py,
run as a subprocess.
"""
import os
import sys
import json
import time
import queue
import ctypes
import tempfile
import threading
import datetime
import traceback
import subprocess
import webview

from schedule_shared import (APP_DIR, load_config, save_config, DEFAULT_SLOTS, parse_clock, is_timezone,
                             week_start_from_labels, desired_shifts)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
LOG_FILE = os.path.join(APP_DIR, "schedule_gui.log")
# What the window's log panel shows, kept so it is still there after a
# restart (one JSON [text, class] per line); "Clear log" empties it.
PANEL_LOG = os.path.join(APP_DIR, "schedule_panel_log.jsonl")
PANEL_KEEP = 3000
LOG_MAX_BYTES = 1_000_000
WINDOW_TITLE = "ILTC WEEKLY SCHEDULE"
WEBVIEW_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or APP_DIR, "ILTC WEEKLY SCHEDULE", "webview")
_WORKER_NAME = "ILTC WEEKLY SCHEDULE WORKER.exe"
CREATE_NO_WINDOW = 0x08000000

ACCOUNTS_TIMEOUT = 90
WEEKS_TIMEOUT_EACH = 25
SYNC_TIMEOUT = 900        # one account: up to ~60 creates/deletes
POLL_INTERVAL = 0.2

_log_lock = threading.Lock()


def log_to_file(msg):
    try:
        with _log_lock:
            try:
                if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
            except OSError:
                pass
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"{stamp}  {msg}\n")
    except Exception:
        pass


threading.excepthook = lambda a: log_to_file(
    "UNCAUGHT in thread:\n" + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))


def worker_command(*args):
    if getattr(sys, "frozen", False):
        for candidate in (os.path.join(APP_DIR, "worker", _WORKER_NAME),
                          os.path.join(APP_DIR, _WORKER_NAME)):
            if os.path.exists(candidate):
                return [candidate, *args]
        return [os.path.join(APP_DIR, "worker", _WORKER_NAME), *args]
    return [sys.executable, os.path.join(APP_DIR, "schedule_worker.py"), *args]


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def alert_finished():
    """Flash the taskbar button until looked at, and beep - the run keeps
    Chrome and Infloww in front, so this window is buried."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                            ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)
            user32.FlashWindowEx(ctypes.byref(info))
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


class _Stopped(Exception):
    pass


class _Timeout(Exception):
    pass


with open(os.path.join(RESOURCE_DIR, "gui.html"), "r", encoding="utf-8") as _f:
    GUI_HTML = _f.read()


def _nothing(name, reason):
    return {"name": name, "state": "nothing", "done": [], "missing": [["not started", reason]]}


def _outcome(name, r, dry_run):
    """One account's result sorted into what went through and what did
    not: {name, state: done|partial|nothing, done: [...], missing:
    [[what, why]]}."""
    if dry_run:
        if r.get("error"):
            return _nothing(name, r["error"])
        return {"name": name, "state": "done", "done": [], "missing": []}
    why = {}
    for f in r.get("failed", []):
        what, _, reason = f.partition(": ")      # times have no ": " in them
        why[what] = reason
    create_failed = any(f.startswith("create ") for f in r.get("failed", []))
    done = list(r.get("done", []))
    remaining = r.get("remaining")
    if remaining is None:
        # It stopped part-way: whatever was planned and not done is missing.
        remaining = {"creates": [c for c in r.get("creates", []) if "created " + c not in done],
                     "deletes": [d for d in r.get("deletes", []) if "deleted " + d not in done]}
    missing = []
    for c in remaining.get("creates", []):
        missing.append([f"not created: {c}", why.get("create " + c, "")])
    for d in remaining.get("deletes", []):
        reason = why.get("delete " + d) or (
            "left in place because a shift above could not be created" if create_failed else "")
        missing.append([f"not deleted: {d}", reason])
    if r.get("error"):
        missing.append(["stopped part-way" if done else "not started", r["error"]])
    state = "done" if not missing else ("partial" if done else "nothing")
    return {"name": name, "state": state, "done": done, "missing": missing}


def _state_text(o):
    if o["state"] == "partial":
        return f"partly done ({len(o['done'])} change(s) went through, {len(o['missing'])} did not)"
    return "nothing went through"


class Api:
    def __init__(self):
        self._window = None
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._proc = None

    def set_window(self, window):
        self._window = window

    # --- called from the page ---

    def get_state(self):
        return load_config()

    def get_panel_log(self):
        """The log panel's lines from before, newest PANEL_KEEP."""
        try:
            with open(PANEL_LOG, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        if len(lines) > PANEL_KEEP:
            lines = lines[-PANEL_KEEP:]
            try:
                with open(PANEL_LOG, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            except OSError:
                pass
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def clear_panel_log(self):
        try:
            open(PANEL_LOG, "w", encoding="utf-8").close()
        except OSError:
            pass
        return True

    def save_codes(self, codes):
        cfg = load_config()
        cfg["codes"] = {str(k): str(v).strip().upper() for k, v in (codes or {}).items()}
        return save_config(cfg)

    def save_selected(self, names):
        cfg = load_config()
        cfg["selected"] = [str(n) for n in (names or [])]
        return save_config(cfg)

    def save_slots(self, slots):
        cfg = load_config()
        clean = []
        for given, default in zip(slots or [], DEFAULT_SLOTS):
            start, end = parse_clock(given.get("start")), parse_clock(given.get("end"))
            old = next((x for x in cfg["slots"] if x.get("name") == default["name"]), default)
            clean.append({"name": default["name"], "start": start or default["start"],
                          "end": end or default["end"],
                          "day_offset": old.get("day_offset", default.get("day_offset", 0))})
        if len(clean) == len(DEFAULT_SLOTS):
            cfg["slots"] = clean
        return save_config(cfg)

    def save_timezone(self, value):
        cfg = load_config()
        if is_timezone(value):
            cfg["timezone"] = value
        return save_config(cfg)

    def reset_slots(self):
        cfg = load_config()
        cfg["slots"] = [dict(s) for s in DEFAULT_SLOTS]
        return save_config(cfg)

    def load_accounts(self):
        self._start_job(self._load_accounts)

    def start_numbers(self, first, last, dry_run):
        self._start_job(self._run_numbers, first, last, bool(dry_run))

    def start(self, names, dry_run):
        self._start_job(self._run, [str(n) for n in names], bool(dry_run))

    def stop(self):
        self._stop.set()
        self._kill("stop requested")

    # --- plumbing ---

    def _start_job(self, fn, *args):
        self._stop.clear()
        threading.Thread(target=self._guarded, args=(fn, *args), daemon=True).start()

    def _js(self, script):
        if self._window is None:
            return
        try:
            self._window.evaluate_js(script)
        except Exception as e:
            log_to_file(f"evaluate_js failed: {e!r} :: {script[:200]}")

    def _log(self, text, cls=None):
        log_to_file(text)
        try:
            with _log_lock, open(PANEL_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps([text, cls], ensure_ascii=False) + "\n")
        except Exception:
            pass
        self._js(f"appendLog({json.dumps(text)}, {json.dumps(cls)})")

    def _footer(self, text):
        self._js(f"setFooter({json.dumps(text)})")

    def _guarded(self, fn, *args):
        if not self._busy.acquire(blocking=False):
            self._log("Already working - wait for it to finish, or hit Stop.", "l-err")
            return
        self._js("setRunning(true)")
        try:
            fn(*args)
        except _Stopped:
            self._log("Stopped.", "l-warn")
            self._footer("Stopped.")
        except Exception as e:
            log_to_file("JOB FAILED:\n" + traceback.format_exc())
            self._log(f"Something went wrong: {e} (details in schedule_gui.log)", "l-err")
        finally:
            self._busy.release()
            self._js("setRunning(false)")

    def _kill(self, why):
        proc = self._proc
        if proc is not None and proc.poll() is None:
            log_to_file(f"killing worker {proc.pid}: {why}")
            try:
                proc.kill()
            except Exception:
                pass

    def _worker(self, args, timeout, job=None, on_line=None):
        """Run the worker, relaying its LOG lines live; returns its RESULT.
        Any other line goes to `on_line` (the batch sync's START/ACCOUNT)."""
        job_path = None
        if job is not None:
            fd, job_path = tempfile.mkstemp(prefix="iltc_job_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False)
            args = [*args, job_path]
        proc = subprocess.Popen(worker_command(*args), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", creationflags=CREATE_NO_WINDOW)
        self._proc = proc
        lines = queue.Queue()

        def pump():
            for line in proc.stdout:
                lines.put(line.rstrip("\n"))
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.time() + timeout
        result, other = None, []
        try:
            while True:
                try:
                    line = lines.get(timeout=POLL_INTERVAL)
                except queue.Empty:
                    line = ""
                if line is None:
                    break
                if line.startswith("RESULT "):
                    result = json.loads(line[7:])
                elif line.startswith("LOG "):
                    self._log("    " + line[4:], "l-dim")
                elif line and on_line is not None and line.startswith(("START ", "ACCOUNT ")):
                    on_line(line)
                elif line:
                    other.append(line)
                if self._stop.is_set():
                    self._kill("stop requested")
                    raise _Stopped()
                if time.time() > deadline:
                    self._kill(f"over {timeout}s")
                    raise _Timeout()
        finally:
            self._proc = None
            if job_path:
                try:
                    os.remove(job_path)
                except OSError:
                    pass
        if result is None:
            log_to_file("worker gave no result:\n" + "\n".join(other[-40:]))
            return {"error": "the worker stopped without a result (see schedule_gui.log)"}
        return result

    # --- jobs ---

    def _load_accounts(self):
        self._footer("Reading accounts from the scheduling site...")
        res = self._worker(["--accounts"], ACCOUNTS_TIMEOUT)
        if res.get("error"):
            self._log(res["error"], "l-err")
            self._footer(res["error"])
            return False
        cfg = load_config()
        cfg["accounts"] = res["accounts"]
        save_config(cfg)
        self._js(f"renderState({json.dumps(cfg, ensure_ascii=False)})")
        self._log(f"Loaded {len(res['accounts'])} accounts from the site.", "l-ok")
        self._footer(f"{len(res['accounts'])} accounts loaded.")
        return True

    def _run_numbers(self, first, last, dry_run):
        """Run by the site's numbers (the boss's list says "29 to 43"):
        the account list and those accounts' weeks are read in one pass,
        so the numbers mean what the site shows today."""
        try:
            first, last = sorted((int(first), int(last)))
        except (TypeError, ValueError):
            self._footer("Type two account numbers, e.g. 29 and 43.")
            return
        self._footer(f"Reading accounts {first} to {last} from the scheduling site...")
        res = self._worker(["--pick"], WEEKS_TIMEOUT_EACH * (last - first + 1) + 60,
                           {"numbers": [first, last]})
        if res.get("error"):
            self._log(res["error"], "l-err")
            self._footer(res["error"])
            return
        picked = res.get("picked") or []
        cfg = load_config()
        cfg["accounts"] = res["accounts"]
        if picked:
            cfg["selected"] = picked
        save_config(cfg)
        self._js(f"renderState({json.dumps(cfg, ensure_ascii=False)})")
        if not picked:
            self._log(f"No accounts numbered {first} to {last} on the site.", "l-err")
            self._footer(f"No accounts numbered {first} to {last}.")
            return
        self._log(f"Selected {first} to {last} ({len(picked)}): " + ", ".join(picked), "l-ok")
        self._run(picked, dry_run, prefetched=res)

    def _run(self, names, dry_run, prefetched=None):
        started = time.time()
        cfg = load_config()
        codes, slots, timezone = cfg["codes"], cfg["slots"], cfg["timezone"]
        missing_code = [n for n in names if not codes.get(n)]
        if missing_code:
            self._log("No Infloww creator code for: " + ", ".join(missing_code)
                      + " - fill in the code box next to the account.", "l-err")
            names = [n for n in names if codes.get(n)]
        if not names:
            return
        mode = "PREVIEW - nothing will be changed" if dry_run else "RUN - Infloww will be changed"
        self._log(f"{mode}. {len(names)} account(s). Infloww time zone {timezone}. Times: "
                  + ", ".join(f"{s['name']} {s['start']}-{s['end']}" for s in slots), "l-header")

        res = prefetched
        if res is None:
            self._footer("Reading the week from the scheduling site...")
            res = self._worker(["--pick"], WEEKS_TIMEOUT_EACH * len(names) + 60, {"names": names})
            if res.get("error"):
                self._log(res["error"], "l-err")
                self._footer(res["error"])
                return
        weeks, errors = res.get("weeks", {}), dict(res.get("errors", {}))

        outcomes, jobs = [], []
        for i, name in enumerate(names, 1):
            if name not in weeks:
                self._log(f"[{i}/{len(names)}] {name}: could not read the site - {errors.get(name)}", "l-err")
                outcomes.append(_nothing(name, f"could not read the site - {errors.get(name)}"))
                continue
            week = weeks[name]
            if not desired_shifts(week, slots):
                self._log(f"[{i}/{len(names)}] {name}: the site has nobody on this account this week "
                          "- skipped (not deleting everything off a blank week)", "l-warn")
                outcomes.append(_nothing(name, "the site has nobody on this account this week - skipped"))
                continue
            sunday = week_start_from_labels(week["days"])
            jobs.append({"account": name, "code": codes[name], "week": week, "slots": slots,
                         "week_start": sunday.isoformat() if sunday else None, "dry_run": dry_run,
                         "timezone": timezone, "_n": i})

        # Every account in one worker session (Infloww found and readied
        # once); it reports each account as it finishes.
        t0 = [time.time()]

        def on_line(line):
            if line.startswith("START "):
                job = jobs[int(line[6:])]
                week = job["week"]
                self._log(f"[{job['_n']}/{len(names)}] {job['account']} ({job['code']})  week of "
                          f"{job['week_start'] or week['days'][:1]}", "l-header")
                for slot in slots:
                    row = week["slots"].get(slot["name"]) or []
                    self._log(f"    {slot['name']:<9} " + " | ".join(n or "—" for n in row))
                self._footer(f"[{job['_n']}/{len(names)}] {job['account']}")
                t0[0] = time.time()
            else:
                r = json.loads(line[8:])
                self._report(r, dry_run)
                outcomes.append(_outcome(r.get("account") or "?", r, dry_run))
                self._log(f"    ({format_duration(time.time() - t0[0])})", "l-dim")

        if jobs:
            try:
                self._worker(["--sync-batch"], SYNC_TIMEOUT * len(jobs) + 120,
                             {"jobs": jobs, "timezone": timezone}, on_line=on_line)
            except _Timeout:
                self._log(f"    TIMED OUT after {format_duration(time.time() - t0[0])}", "l-err")
            reported = {o["name"] for o in outcomes}
            for job in jobs:
                if job["account"] not in reported:
                    outcomes.append(_nothing(job["account"], "the run stopped before this account"))
        order = {n: k for k, n in enumerate(names)}
        outcomes.sort(key=lambda o: order.get(o["name"], len(order)))

        took = format_duration(time.time() - started)
        done = [o for o in outcomes if o["state"] == "done"]
        partial = [o for o in outcomes if o["state"] == "partial"]
        nothing = [o for o in outcomes if o["state"] == "nothing"]
        problems = partial + nothing
        verb = "previewed" if dry_run else "done correctly"

        self._log("=" * 50, "l-dim")
        self._log(f"Finished in {took}. {len(done)} {verb}, {len(partial)} partly done, "
                  f"{len(nothing)} nothing done.", "l-ok" if not problems else "l-warn")
        if done:
            self._log(f"{verb.upper()}: " + ", ".join(o["name"] for o in done), "l-ok")
        if problems:
            self._log("DID NOT GO THROUGH:", "l-err")
            for o in problems:
                self._log(f"  {o['name']} - {_state_text(o)}", "l-err")
                for text, reason in o["missing"]:
                    self._log(f"      {text}" + (f"  ({reason})" if reason else ""), "l-warn")
        self._js(f"setRetryList({json.dumps([o['name'] for o in problems])})")

        title = ("Preview finished" if dry_run else
                 "✅ Everything went through" if not problems else "⚠ Some things didn't go through")
        rows = [[verb.capitalize(), f"{len(done)} of {len(names)}", "ok"]]
        if partial:
            rows.append(["Partly done", str(len(partial)), "warn"])
        if nothing:
            rows.append(["Nothing done", str(len(nothing)), "err"])
        rows.append(["Time taken", took, None])
        details = {
            "problems": [{"name": o["name"], "state": _state_text(o), "items": o["missing"]}
                         for o in problems],
            "done": [{"name": o["name"],
                      "items": [d[0].upper() + d[1:] for d in o["done"]]
                      or (["Previewed"] if dry_run else ["Already matched the site - nothing to change"])}
                     for o in done],
            "done_label": verb.capitalize(),
            "no_issues": not problems,
        }
        self._js(f"showDoneDialog({json.dumps(title)}, {json.dumps(rows)}, "
                 f"{json.dumps(details, ensure_ascii=False)})")
        self._footer(f"Done - {len(done)} {verb}, {len(partial)} partly, {len(nothing)} nothing done.")
        alert_finished()

    def _report(self, r, dry_run):
        if r.get("creator"):
            self._log(f"    Infloww creator: {r['creator']}", "l-dim")
        if r.get("creates") or r.get("deletes"):
            for c in r.get("creates", []):
                self._log(f"    + create {c}", "l-ok")
            for d in r.get("deletes", []):
                self._log(f"    - delete {d}", "l-warn")
        elif not r.get("error"):
            self._log("    all good - everything is done as it should be", "l-ok")
        for f in r.get("failed", []):
            self._log(f"    FAILED: {f}", "l-err")
        if r.get("error"):
            self._log(f"    ERROR: {r['error']}", "l-err")
        remaining = r.get("remaining")
        if not dry_run and remaining is not None:
            if remaining.get("creates") or remaining.get("deletes"):
                for c in remaining.get("creates", []):
                    self._log(f"    still missing: {c}", "l-warn")
                for d in remaining.get("deletes", []):
                    self._log(f"    still there: {d}", "l-warn")
            else:
                self._log("    done - every change went through", "l-ok")


if __name__ == "__main__":
    log_to_file("--- app start ---")
    api = Api()
    window = webview.create_window(WINDOW_TITLE, html=GUI_HTML, js_api=api, width=1180,
                                   height=860, min_size=(900, 640), background_color="#111318")
    api.set_window(window)
    webview.start(storage_path=WEBVIEW_DIR)
