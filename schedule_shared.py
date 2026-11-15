"""
Code both processes need, with no uiautomation/comtypes dependency - the
GUI process imports this, and must never pull in comtypes (same reason as
in UPSELL SCRIPT INFLOWW: pywebview + comtypes in one PyInstaller build
deadlocks on startup).

Holds the config file, the account -> Infloww creator mapping, the shift
slot times, and plan_sync(), which decides what to create and delete.
"""
import os
import re
import sys
import glob
import json
import ctypes
import datetime
import subprocess

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "schedule_config.json")

INFLOWW_CLOSED = "Infloww is not open - start Infloww, then try again."
SITE_CLOSED = ("The scheduling site is not open in Chrome - open "
               "http://129.153.228.212/modules/scheduling/scheduling.html, log in, then try again.")

DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Infloww is switched to this zone (top bar > Settings) before anything
# else; changeable in the app. Shift times below are in that zone. A shift
# starts on the day the site shows it; an end at or before the start means
# the next day, so Main on Sunday runs Sun 6 PM -> Mon 2 AM. These match
# Infloww's blue presets, which the worker clicks instead of typing times.
DEFAULT_TIMEZONE = "UTC+02:00"   # EU; the site's days are EST (see DEFAULT_SLOTS)
_TZ = re.compile(r"^UTC[+-](0\d|1[0-4]):(00|30|45)$")
# EU time (UTC+02:00), the site's days being EST: Grave and Afternoon start
# on the site's day; Main (the EST evening) starts at 02:00 the NEXT day in
# EU time - "day_offset": 1. So the site's Saturday Main lands on next
# week's Sunday, and this week's Sunday 02:00 Main belongs to last week.
DEFAULT_SLOTS = [
    {"name": "Grave", "start": "10:00", "end": "18:00", "day_offset": 0},
    {"name": "Afternoon", "start": "18:00", "end": "02:00", "day_offset": 0},
    {"name": "Main", "start": "02:00", "end": "10:00", "day_offset": 1},
]

# Website account name -> Infloww creator code (the part before " - " in
# "ANT - 🍉 Antje"). Taken from the order of the upsell tool's model list,
# which matches the site's account order. Editable in the app.
DEFAULT_CODES = {
    "Amouranth": "AMO", "Athena": "ATH", "Jade": "JADE", "Kirsten": "K2S",
    "ika": "IKA", "Emma Louise": "EMMA", "Vixen": "VIX", "Bellbo": "BELL",
    "Eva Elfie": "EVA", "Morgpie": "MP", "Effy": "EFFY", "Queen Kalin": "QUE",
    "Buttercup": "BUT", "Mia Waifu": "MIA", "Maddie": "MAD", "Lela": "LELA",
    "Rita": "RIT", "Tori Page": "TOR", "Cluelo Paid": "CLU", "Zoey": "ZOEY",
    "Trippie Bri": "TRI", "Asianbunny": "AB", "Colleen": "COL", "Lindsay": "LIN",
    "Bianca": "BIA", "Savannah Skye": "SAV", "Spooky": "SPK", "Undead Em": "UND",
    "Ella": "ELLA", "Adriana Chechik": "ADR", "Lavender": "LAV", "Antje": "ANT",
    "Raelee": "RAE", "Jenbretty": "JBR", "Vladi": "VLADI", "Kalayna": "KAL",
    "Baby Yujia": "YUJ", "Destiny Fomo": "DSTY", "Blue Widdow": "BW",
}


# --- config ---------------------------------------------------------------

def _default_config():
    return {"slots": [dict(s) for s in DEFAULT_SLOTS], "codes": dict(DEFAULT_CODES),
            "timezone": DEFAULT_TIMEZONE, "accounts": [], "selected": []}


def load_config():
    cfg = _default_config()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return cfg
    if isinstance(raw.get("slots"), list) and len(raw["slots"]) == len(DEFAULT_SLOTS):
        cfg["slots"] = raw["slots"]
    if isinstance(raw.get("codes"), dict):
        cfg["codes"].update({str(k): str(v) for k, v in raw["codes"].items()})
    if is_timezone(raw.get("timezone")):
        cfg["timezone"] = raw["timezone"]
    for key in ("accounts", "selected"):
        if isinstance(raw.get(key), list):
            cfg[key] = raw[key]
    return cfg


def is_timezone(value):
    """"UTC-06:00" style, the way Infloww's top bar shows it."""
    return isinstance(value, str) and bool(_TZ.match(value))


def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)
    return cfg


# --- names and times --------------------------------------------------------

def norm_name(name):
    """Website "lonewolf" is Infloww "Lonewolf": compare case- and
    space-insensitively."""
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def is_empty_cell(name):
    return norm_name(name) in ("", "-", "—", "–", "?", "none", "open", "empty")


_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([ap]m)?\s*$", re.I)


def parse_clock(text):
    """"02:00 AM", "6:00 pm", "18:00" -> "HH:MM" (24h), or None."""
    m = _CLOCK.match(str(text or ""))
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ampm == "am" and hour == 12:
        hour = 0
    elif ampm == "pm" and hour != 12:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def clock_12h(hhmm):
    """"18:00" -> "6:00 PM", the spelling Infloww's time pickers use."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12}:{minute:02d} {suffix}"


def crosses_midnight(slot):
    return slot["end"] <= slot["start"]


# --- which week -------------------------------------------------------------

_ORDINAL = re.compile(r"(\d{1,2})(st|nd|rd|th)?\b")


def week_start_from_labels(labels, today=None):
    """The website only says "Sunday 25th"; pick the Sunday with that day
    number closest to today. Returns a date, or None."""
    m = _ORDINAL.search(labels[0] if labels else "")
    if not m:
        return None
    day = int(m.group(1))
    today = today or datetime.date.today()
    best = None
    for offset in range(-45, 46):
        d = today + datetime.timedelta(days=offset)
        if d.day == day and d.weekday() == 6:
            if best is None or abs(offset) < abs((best - today).days):
                best = d
    return best


# --- the plan ---------------------------------------------------------------

def desired_shifts(week, slots):
    """Website grid -> list of {day, employee, start, end, slot}. `day` is
    the day in Infloww's zone: the site's day plus the slot's day_offset,
    so a Saturday Main with offset 1 comes out as day 7 (next Sunday)."""
    out = []
    for slot in slots:
        row = week["slots"].get(slot["name"]) or []
        offset = int(slot.get("day_offset", 0) or 0)
        for day, name in enumerate(row[:7]):
            if is_empty_cell(name):
                continue
            out.append({"day": day + offset, "employee": name.strip(), "start": slot["start"],
                        "end": slot["end"], "slot": slot["name"]})
    return out


def carried_in(shift, slots):
    """True for a shift at the start of the week that belongs to the week
    before: on Sunday at the times of a slot with a day offset (last
    week's Saturday Main)."""
    return shift["day"] == 0 and any(int(sl.get("day_offset", 0) or 0) > 0 and
                                     (shift["start"], shift["end"]) == (sl["start"], sl["end"])
                                     for sl in slots)


def _key(s):
    return (s["day"], norm_name(s["employee"]), s["start"], s["end"])


def plan_sync(desired, existing):
    """What to change so Infloww matches the website for one creator.

    `existing` is every shift Infloww shows that week. Anything in it the
    website does not ask for is deleted, including the right person at the
    wrong time. Missing shifts are grouped per person and slot into one
    weekly-repeating shift ticking the days they work.

    Returns (creates, deletes): creates = [{employee, slot, start, end,
    days}], deletes = existing entries, earliest day first.
    """
    want = {}
    for s in desired:
        want.setdefault(_key(s), s)
    have = {}
    deletes = []
    for s in existing:
        k = _key(s)
        if k in want and k not in have:
            have[k] = s
        else:
            # Not wanted, or a duplicate of one already kept.
            deletes.append(s)
    groups = {}
    for k, s in want.items():
        if k in have:
            continue
        g = groups.setdefault((norm_name(s["employee"]), s["slot"]),
                              {"employee": s["employee"], "slot": s["slot"],
                               "start": s["start"], "end": s["end"], "days": []})
        g["days"].append(s["day"])
    creates = []
    for g in groups.values():
        g["days"].sort()
        creates.append(g)
    creates.sort(key=lambda g: (g["days"][0], g["start"], norm_name(g["employee"])))
    deletes.sort(key=lambda s: (s["day"], s["start"]))
    return creates, deletes


def is_clock_change_day(day):
    """True on the days this PC's clock jumps for daylight saving (in
    Norway: last Sunday of March and of October)."""
    start = datetime.datetime.combine(day, datetime.time(0, 0)).astimezone()
    end = datetime.datetime.combine(day, datetime.time(23, 59)).astimezone()
    return start.utcoffset() != end.utcoffset()


def split_for_clock_change(groups, sunday, crosses):
    """Infloww gets times wrong on the days the PC's clock changes: its
    hour list has "2 AM" twice (or none), and presets and later hours land
    an hour off - including every repeat of a weekly shift that starts on
    that day. So a shift touching such a day is made as a one-off
    (flagged "clock_change", set by hour label instead of preset), and the
    weekly repeat starts from the next day worked. Seen 2026-10-25."""
    out = []
    for g in groups:
        def touches(d):
            day = sunday + datetime.timedelta(days=d)
            return is_clock_change_day(day) or (
                crosses[g["slot"]] and is_clock_change_day(day + datetime.timedelta(days=1)))
        odd = [d for d in g["days"] if touches(d)]
        rest = [d for d in g["days"] if d not in odd]
        for d in odd:
            out.append(dict(g, days=[d], repeat=False, clock_change=True))
        if rest:
            out.append(dict(g, days=rest))
    return out


def describe_days(days):
    return ", ".join(DAY_NAMES[d] for d in days)


# --- windows (ctypes only) --------------------------------------------------

def enum_top_level_windows():
    user32 = ctypes.windll.user32
    results = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        results.append((hwnd, buf.value, cls.value))
        return True

    user32.EnumWindows(proc(callback), 0)
    return results


def infloww_windows():
    return [(h, t, c) for h, t, c in enum_top_level_windows()
            if c == "Chrome_WidgetWin_1" and t.startswith("Infloww")
            and "Visual Studio Code" not in t]


def chrome_windows():
    return [(h, t, c) for h, t, c in enum_top_level_windows()
            if c == "Chrome_WidgetWin_1" and t.endswith(" - Google Chrome")]


def find_infloww_launcher():
    env = {"pf": os.environ.get("ProgramFiles", r"C:\Program Files"),
           "pf86": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
           "local": os.environ.get("LOCALAPPDATA", "")}
    for pattern in (r"{pf}\*nfloww*\Infloww.exe", r"{pf86}\*nfloww*\Infloww.exe",
                    r"{local}\Programs\*nfloww*\Infloww.exe", r"{local}\*nfloww*\Infloww.exe"):
        for hit in sorted(glob.glob(pattern.format(**env))):
            if os.path.isfile(hit):
                return hit
    return None


def ensure_infloww_running():
    """(ok, message). Starts Infloww if it is installed but not open; the
    worker then waits for it to load with its own retries."""
    if infloww_windows():
        return True, None
    exe = find_infloww_launcher()
    if not exe:
        return False, INFLOWW_CLOSED
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)
    except OSError:
        return False, INFLOWW_CLOSED
    return True, "Infloww was not open - started it."
