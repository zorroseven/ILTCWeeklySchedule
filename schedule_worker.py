"""
ILTC WEEKLY SCHEDULE WORKER

The half of the app that drives other windows (uiautomation/comtypes),
kept in its own process for the same reason as UPSELL SCRIPT INFLOWW's
worker: pywebview and comtypes in one PyInstaller build deadlock.

Writes "LOG <text>" lines while it works and finishes with one
"RESULT <json>" line.

Usage:
  worker --accounts             list every account on the scheduling page
  worker --weeks <job.json>     read the week grid for {"accounts": [...]}
  worker --sync <job.json>      make Infloww match one account's week
"""
import sys
import json
import time
import datetime
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from schedule_shared import (desired_shifts, plan_sync, crosses_midnight, describe_days,
                             week_start_from_labels, ensure_infloww_running, DAY_NAMES,
                             split_for_clock_change, norm_name, carried_in,
                             DEFAULT_TIMEZONE)

MAX_ACTIONS = 60


def log(text):
    print("LOG " + text, flush=True)


def finish(result):
    print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)


def run_accounts():
    import site_core
    doc, problem = site_core.open_site()
    if problem:
        return {"error": problem}
    return {"accounts": site_core.list_accounts(doc)}


def run_weeks(job):
    import site_core
    doc, problem = site_core.open_site()
    if problem:
        return {"error": problem}
    weeks, errors = {}, {}
    for name in job["accounts"]:
        try:
            weeks[name] = site_core.read_week(doc, name)
            log(f"read {name}")
        except Exception as e:
            errors[name] = str(e)
            log(f"could not read {name}: {e}")
    return {"weeks": weeks, "errors": errors}


def run_pick(job):
    """Account list + the weeks to do, in one pass over the site: by
    job["numbers"] = [first, last] (as the site numbers them today) or
    job["names"]."""
    import site_core
    doc, problem = site_core.open_site()
    if problem:
        return {"error": problem}
    accounts, picked, weeks, errors = site_core.read_accounts_and_weeks(
        doc, names=job.get("names"), numbers=job.get("numbers"), log=log)
    return {"accounts": accounts, "picked": picked, "weeks": weeks, "errors": errors}


def run_sync_batch(job):
    """Every account's sync in this one process: Infloww is found and
    readied once, its zone checked once (on the first account) and put
    back once at the end. Prints "START <i>" before and "ACCOUNT <json>"
    after each account, for the app to show as it goes."""
    import infloww_core as inf
    jobs = job["jobs"]
    zone_was = None
    for i, one in enumerate(jobs):
        print(f"START {i}", flush=True)
        one = dict(one, check_zone=zone_was is None)
        r = run_sync(one)
        if r.get("zone_was"):
            zone_was = r["zone_was"]
        print("ACCOUNT " + json.dumps(r, ensure_ascii=False), flush=True)
    timezone = job.get("timezone") or DEFAULT_TIMEZONE
    if zone_was and not zone_was.startswith(f"({timezone})"):
        try:
            window = inf.get_window()
            inf.wait_until_readable(window)
            inf.ensure_timezone(window, zone_was)
            inf.open_shift_schedule(window)
            log(f"switched Infloww's time zone back to {zone_was}")
        except Exception as e:
            log(f"could not switch Infloww's time zone back to {zone_was}: {e}")
    return {"ok": True}


def _fmt(s):
    return f"{DAY_NAMES[s['day']]} {s['employee']} {s['start']}-{s['end']}"


def _fmt_group(g):
    return f"{g['employee']} {g['slot']} {g['start']}-{g['end']} on {describe_days(g['days'])}"


def delete_wrong(inf, window, deletes, desired, result):
    """Delete every wrong chatter+time with ONE "This and all future
    shifts" on its first wrong day: that clears the rest of the week AND the
    weeks after (old repeats were left overlapping there before). Returns
    the (person, start, end, from_day) cleared, so days that were right but
    went with it can be created again."""
    groups = {}
    for x in sorted(deletes, key=lambda x: (x["day"], x["start"])):
        groups.setdefault((norm_name(x["employee"]), x["start"], x["end"]), []).append(x)
    cleared = []
    for key, items in groups.items():
        x = items[0]
        note = f" and all future shifts ({len(items)} day(s) this week)"
        try:
            inf.delete_shift(window, x, only_this=False)
            log("deleted " + _fmt(x) + note)
        except inf.ShiftGone:
            log(f"{_fmt(x)} was already gone")
        except Exception as e:
            result["failed"].append(f"delete {_fmt(x)}: {e}")
            log(f"FAILED to delete {_fmt(x)}: {e}")
            continue
        for y in items:
            result["done"].append("deleted " + _fmt(y))
        cleared.append((*key, x["day"]))
    return cleared


def _gone(x, cleared):
    k = (norm_name(x["employee"]), x["start"], x["end"])
    return any(k == c[:3] and x["day"] >= c[3] for c in cleared)


def _next_sunday(inf, window, creator, code, sunday, spill, crosses, slots, checked, result):
    """The site's Saturday Main, which in EU time starts on next week's
    Sunday: page one week on, fix just that Sunday's Main-time shifts."""
    nxt = sunday + datetime.timedelta(days=7)
    inf.go_to_week(window, nxt)
    inf.forget_cells(window)
    try:
        inf._day_cells(window, True)
    except Exception:
        pass
    # Every early-morning shift on that Sunday is this week's Saturday Main:
    # at 02:00-10:00 it is checked against the site, at any other (old)
    # time it is wrong and goes - left alone it sat next to the new one.
    there = [x for x in inf.read_shifts(window, creator)
             if x["day"] == 0 and (carried_in(x, slots) or x["start"] < "06:00")]
    creates, deletes = plan_sync(spill, there)
    cleared = delete_wrong(inf, window, deletes, spill, result)
    if cleared:
        creates, _ = plan_sync(spill, [x for x in there if not _gone(x, cleared)])
    for g in creates:
        g = dict(g, repeat=False)
        try:
            _create_with_activation(inf, window, code, creator, g, nxt, crosses, checked)
            result["done"].append("created " + _fmt_group(g) + " (next Sunday)")
            log("created " + _fmt_group(g) + f" on {nxt.isoformat()} (the site's Saturday Main)")
        except Exception as e:
            result["failed"].append(f"create {_fmt_group(g)} (next Sunday): {e}")
            log(f"FAILED to create {_fmt_group(g)} on {nxt.isoformat()}: {e}")
    if not creates and not deletes:
        log(f"next Sunday ({nxt.isoformat()}) already has the right Main")


def _split_overnight_saturday(groups, crosses):
    """An overnight repeat (18:00-02:00) must end on the Sunday after to
    keep its Saturday night - and Infloww then also adds that next Sunday
    (seen: Neki on Sun 22). So the Saturday goes as its own one-day shift
    and the repeat ends on Saturday."""
    out = []
    for g in groups:
        if crosses.get(g["slot"]) and 6 in g["days"] and len(g["days"]) > 1 and g.get("repeat", True):
            out.append(dict(g, days=[d for d in g["days"] if d != 6], until_saturday=True))
            out.append(dict(g, days=[6], repeat=False))
        else:
            out.append(g)
    return out


def _make(inf, window, code, creator, g, sunday, crosses):
    """Fast path from the creator's row; the full form only for the days
    the PC's clock changes (those need hour-by-label picking)."""
    if g.get("clock_change"):
        return inf.create_shift(window, code, creator, g, sunday, crosses[g["slot"]])
    return inf.create_shift_fast(window, g, sunday, crosses[g["slot"]])


def _back_to_week(inf, window, code, sunday):
    inf.open_shift_schedule(window)
    inf.select_creator(window, code)
    inf.go_to_week(window, sunday)
    inf.forget_cells(window)
    try:
        inf._day_cells(window, True)     # found once; deletes and creates reuse it
    except Exception:
        pass


def _create_with_activation(inf, window, code, creator, g, sunday, crosses, checked):
    """Create one shift. If Infloww refuses it in a way a deactivated
    employee would cause, look the employee up in Manage employees,
    activate them if they are deactivated, come back to this account's
    week and try once more."""
    try:
        return _make(inf, window, code, creator, g, sunday, crosses)
    except inf.EmployeeProblem as e:
        who = e.employee
        if who in checked:
            if checked[who] == "not found":
                raise inf.InflowwError(f"{e} ({who} is not in Manage employees)") from None
            raise
        log(f"{who}: {e} - checking them in Manage employees")
        try:
            checked[who] = inf.ensure_employee_active(window, who)
        except Exception as check_error:
            checked[who] = "check failed"
            _back_to_week(inf, window, code, sunday)
            raise inf.InflowwError(f"{e} (and checking {who} in Manage employees failed: "
                                   f"{check_error})") from None
        log(f"{who}: {checked[who]} in Manage employees")
        _back_to_week(inf, window, code, sunday)
        if checked[who] == "not found":
            raise inf.InflowwError(f"{e} ({who} is not in Manage employees)") from None
        if checked[who] != "activated":
            raise
        return _make(inf, window, code, creator, g, sunday, crosses)


def run_sync(job):
    import infloww_core as inf

    week, slots, code = job["week"], job["slots"], job["code"]
    dry = bool(job.get("dry_run"))
    sunday = (datetime.date.fromisoformat(job["week_start"]) if job.get("week_start")
              else week_start_from_labels(week["days"]))
    if sunday is None:
        return {"error": f"could not tell which week {week['days'][:1]} is"}
    desired = desired_shifts(week, slots)
    crosses = {s["name"]: crosses_midnight(s) for s in slots}
    result = {"account": job["account"], "week_start": sunday.isoformat(), "creator": None,
              "creates": [], "deletes": [], "done": [], "failed": [], "remaining": None,
              "dry_run": dry, "error": None}
    try:
        return _sync(inf, result, code, sunday, desired, crosses, dry,
                     job.get("timezone") or DEFAULT_TIMEZONE, job.get("check_zone", True), slots)
    except Exception as e:
        # Keep what was already done - the log of a half-finished account
        # is exactly what is needed to check it by hand.
        log(traceback.format_exc().replace("\n", " | "))
        result["error"] = f"{type(e).__name__}: {e}"
        return result


def _sync(inf, result, code, sunday, desired, crosses, dry, timezone, check_zone=True, slots=()):
    ok, message = ensure_infloww_running()
    if not ok:
        result["error"] = message
        return result
    if message:
        log(message)
        time.sleep(20)

    window = inf.get_window()
    inf.wait_until_readable(window)
    # The zone is checked (and switched) on the first account of a run
    # only; the app puts the user's own zone back once the run is over.
    if check_zone:
        was = inf.ensure_timezone(window, timezone)
        result["zone_was"] = was
        if not was.endswith("(top bar)") and was != inf.FIXED_ZONES.get(timezone, was):
            log(f"switched Infloww's time zone from {was} to {timezone} while working")
    return _sync_in_zone(inf, window, result, code, sunday, desired, crosses, dry, slots)


def run_zone(job):
    """Put Infloww's Regular time zone back to job["zone"] (the user's own
    view zone) after a run."""
    import infloww_core as inf
    window = inf.get_window()
    inf.wait_until_readable(window)
    inf.ensure_timezone(window, job["zone"])
    inf.open_shift_schedule(window)
    return {"ok": True}


def _sync_in_zone(inf, window, result, code, sunday, desired, crosses, dry, slots=()):
    inf.open_shift_schedule(window)
    creator = inf.select_creator(window, code)
    result["creator"] = creator
    log(f"creator: {creator}")
    # "DSTY" may have picked "DSTY Main - ...": use that full code from
    # here on (Create form, coming back from Manage employees).
    code = creator.split(" - ", 1)[0].strip()
    inf.go_to_week(window, sunday)
    inf.forget_cells(window)
    try:
        inf._day_cells(window, True)     # found once; deletes and creates reuse it
    except Exception:
        pass

    # With a day offset (Main at 02:00 EU = the EST evening before), this
    # week's Sunday Main belongs to last week - left alone - and the site's
    # Saturday Main lands on next week's Sunday (day 7), done after.
    spill = [dict(d, day=0) for d in desired if d["day"] > 6]
    desired = [d for d in desired if d["day"] <= 6]
    raw = inf.read_shifts(window, creator)
    # Sunday early morning = last week's Saturday Main. At the right time
    # it is left alone; at a wrong (old) time it is deleted AND the same
    # chatter is put back at the right time - deleting it alone left the
    # 15th without a Main on six accounts (2026-11-15).
    carry_slot = next((sl for sl in slots if int(sl.get("day_offset", 0) or 0) > 0), None)
    carry_ok = [x for x in raw if carried_in(x, slots)]
    carry_old = [x for x in raw if carry_slot and x["day"] == 0 and not carried_in(x, slots)
                 and x["start"] < "06:00"]
    existing = [x for x in raw if x not in carry_ok and x not in carry_old]
    creates, deletes = plan_sync(desired, existing)
    deletes = carry_old + deletes
    if carry_old and not carry_ok:
        x = carry_old[0]
        creates = [{"employee": x["employee"], "slot": carry_slot["name"], "start": carry_slot["start"],
                    "end": carry_slot["end"], "days": [0], "repeat": False}] + creates
    result["creates"] = [_fmt_group(g) for g in creates]
    result["deletes"] = [_fmt(x) for x in deletes]
    log(f"Infloww has {len(existing)} shift(s); {len(creates)} to create, {len(deletes)} to delete"
        + (f"; next Sunday: {', '.join(d['employee'] for d in spill)}" if spill else ""))
    if dry:
        return result

    if not creates and not deletes and not spill:
        log("all good - everything is already done as it should be")
        result["remaining"] = {"creates": [], "deletes": []}
        return result

    # Delete what is wrong FIRST, then create what is missing - once, no
    # read-back. Shifts that are already right are not touched. Deleting
    # first keeps old and new cards from sitting side by side, where a
    # hover can land on the wrong one (seen 2026-11-12: a new bogibatina
    # went instead of the old one). Each delete is "only this shift": the
    # repeats end with the week anyway.
    checked = {}          # employee -> what Manage employees said
    # Delete, then refresh once and delete again whatever is still wrong:
    # the same chatter+time can sit in two separate old repeats, and one
    # "all future" only clears one of them (seen on Zoey: slavkoP1 Thu/Sat).
    # The fresh read is also what the creates are planned from - "all
    # future" can take a later day that was right.
    for _pass in range(3):
        if not deletes:
            break
        delete_wrong(inf, window, deletes, desired, result)
        inf.refresh(window)
        raw = inf.read_shifts(window, creator)
        existing = [x for x in raw if not carried_in(x, slots)
                    and not (carry_slot and x["day"] == 0 and x["start"] < "06:00")]
        sunday_fix = [g for g in creates if g["days"] == [0] and g.get("repeat") is False
                      and carry_slot and g["start"] == carry_slot["start"]]
        creates, deletes = plan_sync(desired, existing)
        if sunday_fix and not any(carried_in(x, slots) for x in raw):
            creates = sunday_fix + creates
        if deletes:
            log(f"{len(deletes)} still wrong after deleting - another pass")
    for g in _split_overnight_saturday(split_for_clock_change(creates, sunday, crosses), crosses):
        try:
            _create_with_activation(inf, window, code, creator, g, sunday, crosses, checked)
            result["done"].append("created " + _fmt_group(g))
            log("created " + _fmt_group(g))
        except Exception as e:
            result["failed"].append(f"create {_fmt_group(g)}: {e}")
            log(f"FAILED to create {_fmt_group(g)}: {e}")

    if spill:
        _next_sunday(inf, window, creator, code, sunday, spill, crosses, slots, checked, result)

    failed = [f.partition(": ")[0] for f in result["failed"]]
    result["remaining"] = {
        # Every create that failed, the next-Sunday one included.
        "creates": [f[len("create "):] for f in failed if f.startswith("create ")],
        "deletes": [_fmt(x) for x in deletes if "delete " + _fmt(x) in failed],
    }
    return result


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    try:
        if mode == "--accounts":
            return run_accounts()
        job = json.load(open(argv[2], encoding="utf-8")) if len(argv) > 2 else None
        if mode == "--weeks" and job:
            return run_weeks(job)
        if mode == "--sync" and job:
            return run_sync(job)
        if mode == "--zone" and job:
            return run_zone(job)
        if mode == "--pick" and job:
            return run_pick(job)
        if mode == "--sync-batch" and job:
            return run_sync_batch(job)
        return {"error": "usage: --accounts | --weeks <job.json> | --sync <job.json>"}
    except Exception as e:
        log(traceback.format_exc().replace("\n", " | "))
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    finish(main(sys.argv))
