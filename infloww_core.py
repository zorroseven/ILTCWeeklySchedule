"""
Drives Infloww's Employees > Shift schedule page through Windows UI
Automation: filter the calendar to one creator, read every shift in the
week, create weekly-repeating shifts and delete ones that should not be
there.

Infloww is Electron, so its page text comes through the accessibility
tree exactly. The one thing the tree does not expose is which weekday
buttons are ticked in the repeat options; that is read from the button's
colour (ticked = solid blue).
"""
import re
import time
import datetime
import uiautomation as auto

from schedule_shared import (infloww_windows, INFLOWW_CLOSED, parse_clock, clock_12h,
                             norm_name, DAY_NAMES)
from uia_util import (dfs, find, find_all, name_of, rect, center, visible, activate, bring_to_front,
                      wait_for, click, hover, type_text, select_all_and_type, pixel)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InflowwError(RuntimeError):
    pass


class ShiftGone(InflowwError):
    """The shift to delete is not on the calendar any more - usually taken
    by an earlier "This and all future shifts" of the same repeat."""


class EmployeeProblem(InflowwError):
    """A create that failed in a way a deactivated employee would cause -
    worth checking Manage employees and trying again."""
    def __init__(self, employee, message):
        super().__init__(message)
        self.employee = employee


class EmployeeNotPicked(EmployeeProblem):
    pass


class ShiftRejected(EmployeeProblem):
    """OK was pressed but the Create form stayed open. `said` is any new
    text Infloww showed (field errors, a toast)."""
    def __init__(self, employee, said):
        said = [s for s in said if s][:4]
        message = "Infloww did not accept the shift (the form stayed open)"
        if said:
            message += " - Infloww said: " + " | ".join(said)
        super().__init__(employee, message)
        self.said = said


# --- window and page --------------------------------------------------------

def get_window():
    wins = infloww_windows()
    if not wins:
        raise InflowwError(INFLOWW_CLOSED)
    for hwnd, title, cls in wins:
        if title == "Infloww Home":
            return auto.ControlFromHandle(hwnd)
    for hwnd, title, cls in wins:
        if "Messages" not in title:
            return auto.ControlFromHandle(hwnd)
    return auto.ControlFromHandle(wins[0][0])


_TZ_LABEL = re.compile(r"^UTC[+-]\d\d:\d\d$")


def _top_bar_timezone(window):
    return find(window, lambda n: _TZ_LABEL.match(name_of(n)) is not None
                and rect(n)[1] < 70 and visible(n), 25)


def _tz_dropdown(window):
    """Open Settings from the top bar and return the Regular time zone
    dropdown's current-value text."""
    if find(window, lambda n: name_of(n) == "Regular time zone" and visible(n)) is None:
        tz = _top_bar_timezone(window)
        if tz is None:
            # Something is covering the top bar (a popup or dialog) - Esc
            # closes most of them.
            auto.SendKeys("{Esc}", waitTime=0.8)
            tz = _top_bar_timezone(window)
        if tz is None:
            raise InflowwError("could not find the time zone in Infloww's top bar - "
                               "is a popup or dialog open in Infloww?")
        click(tz, 1.5)
    heading = wait_for(lambda: find(window, lambda n: n.ControlTypeName == "TextControl"
                                    and name_of(n) == "Regular time zone" and visible(n)), 6)
    if heading is None:
        raise InflowwError("clicking the time zone did not open Settings")
    hl, ht, hr, hb = rect(heading)
    current = find(window, lambda n: n.ControlTypeName == "TextControl" and visible(n)
                   and name_of(n).startswith("(UTC") and abs(rect(n)[0] - hl) < 40
                   and hb < rect(n)[1] < hb + 70)
    if current is None:
        raise InflowwError("could not find the Regular time zone dropdown")
    return current


def _close_settings(window):
    """The X in the Settings page's top-right corner."""
    settings = find(window, lambda n: n.ControlTypeName == "TextControl" and name_of(n) == "Settings"
                    and visible(n) and rect(n)[1] < 140)
    st = rect(settings)[1] if settings is not None else 87
    closers = [n for n in find_all(window, lambda n: n.ControlTypeName == "ImageControl" and visible(n))
               if abs(rect(n)[1] - st) < 25 and rect(n)[0] > rect(window)[2] - 120]
    if closers:
        click(max(closers, key=lambda n: rect(n)[0]), 1.5)
    else:
        auto.SendKeys("{Esc}", waitTime=1.0)


# The exact Settings zone to use per offset: ones without daylight saving.
# A zone that has it (Guadalajara/Mexico City, "... Time (US ...)") moves
# weekly-repeating shifts an hour when its clocks change - seen on
# 2026-10-25: 02:00-10:00 repeats came out as 01:00-09:00.
FIXED_ZONES = {
    "UTC-05:00": "(UTC-05:00) Bogota, Lima, Quito",
    "UTC-06:00": "(UTC-06:00) Central America",
    "UTC+02:00": "(UTC+02:00) Harare, Pretoria",
}


def ensure_timezone(window, want, force=False):
    """Make Settings > Your preferences > Regular time zone the fixed zone
    for `want` (e.g. "UTC-06:00" -> "(UTC-06:00) Central America"); for an
    offset not in FIXED_ZONES, any "(UTC-06:00) ..." zone without
    "Time (" will do. Shift times are entered in that zone, so this
    happens before anything else. Returns the zone it was set to before.

    Checked in Settings, not the top bar, which does not always agree with
    the zone picked.
    """
    if want.startswith("UTC") and not force:
        top = _top_bar_timezone(window)
        if top is not None and name_of(top) == want:
            return f"({want}) (top bar)"     # already there - no Settings round trip
    prefix = f"({want})" if want.startswith("UTC") else want.split(")", 1)[0] + ")"
    # `want` may also be a full label, e.g. "(UTC+02:00) Gaza, Hebron", to
    # put the user's own zone back after a run.
    exact = FIXED_ZONES.get(want) or (want if want.startswith("(") else None)

    def ok(label):
        return label == exact if exact else (label.startswith(prefix) and "Time (" not in label)

    current = _tz_dropdown(window)
    before = name_of(current)
    try:
        if ok(before) and not force:
            return before
        click(current, 0.8)
        type_text(exact.split(") ", 1)[1] if exact else want, 1.0)
        options = wait_for(lambda: [o for o in _popup_options(window, lambda s: s.startswith(prefix))
                                    if ok(name_of(o))], 5) or []
        if not options:
            auto.SendKeys("{Esc}", waitTime=0.3)
            raise InflowwError(f"Infloww has no Regular time zone {exact or prefix}")
        click(options[0], 1.5)
        if not wait_for(lambda: ok(name_of(_tz_dropdown(window))), 5):
            raise InflowwError(f"could not set Infloww's Regular time zone to {exact or prefix}")
        return before
    finally:
        _close_settings(window)


def _schedule_tab(window):
    return find(window, lambda n: n.ControlTypeName == "TabItemControl"
                and name_of(n) == "Shift schedule" and visible(n))


def _wait_page_loaded(window):
    """Bring Infloww forward and wait until its page can be read. Infloww
    drops its accessibility tree while nobody reads it, and right after
    waking it the page can still read as an empty frame for a few
    seconds - so keep waking it, up to ~15s."""
    for attempt in range(15):
        activate(window, 0.3 if attempt == 0 else 1.0)
        if (find(window, lambda n: n.ControlTypeName == "MenuItemControl", max_depth=20)
                and _top_bar_timezone(window) is not None):
            return
        if attempt % 3 == 1:
            # The wake-up message alone sometimes is not enough; a real
            # click into the page is (seen 2026-11-08: 0 -> readable). The
            # page heading ("Manage employees", "Shift schedule") at the
            # top-left is a spot where a click does nothing.
            l, t, r, b = rect(window)
            auto.Click(l + 190, t + 101, waitTime=1.0)
    raise InflowwError("Infloww is open but its page did not load - is it still "
                       "starting, or waiting for you to log in?")


def _awake(window):
    """Infloww can go blank to UI Automation in the middle of a run too:
    a cheap check before each step, waking it if it did."""
    # Clicks land on whatever window is on top - make sure that is Infloww.
    bring_to_front(window.NativeWindowHandle)
    if find(window, lambda n: n.ControlTypeName == "MenuItemControl", max_depth=20) is None:
        _wait_page_loaded(window)


def wait_until_readable(window):
    """Ready to start: page readable, and no form or box left open by an
    earlier attempt (they cover the page and block every click)."""
    _wait_page_loaded(window)
    for _ in range(4):
        box = _in_portals(window, lambda n: n.ControlTypeName == "CustomControl" and visible(n)
                          and _button(n, "Cancel") is not None)
        if box is None:
            return
        _invoke(_button(box, "Cancel"), 1.0)


def _open_employees_item(window, item, arrived):
    """Employees in the sidebar > `item` ("Shift schedule" / "Manage
    employees"), then wait for `arrived(window)`."""
    employees = find(window, lambda n: n.ControlTypeName == "MenuItemControl"
                     and name_of(n) == "Employees" and visible(n))
    if employees is None:
        raise InflowwError('could not find "Employees" in Infloww\'s sidebar')
    # Hovering Employees opens a submenu: Manage employees / Shift schedule.
    hover(*center(employees), settle=1.0)
    link = wait_for(lambda: find(window, lambda n: n.ControlTypeName == "MenuItemControl"
                                 and name_of(n) == item and visible(n)), 5)
    if link is None:
        raise InflowwError(f'hovering "Employees" did not show "{item}"')
    click(link, 2.0)
    if wait_for(lambda: arrived(window), 8) is None:
        raise InflowwError(f"could not open Employees > {item} in Infloww")


def open_shift_schedule(window):
    """Bring Infloww forward and get it onto Employees > Shift schedule."""
    _wait_page_loaded(window)
    if _schedule_tab(window) is None:
        _open_employees_item(window, "Shift schedule", _schedule_tab)
    # The page also has a Shift history tab; the calendar and its Create
    # button only exist on Shift schedule.
    if _button(window, "Create shift(s)") is None:
        click(_schedule_tab(window), 1.5)
    if wait_for(lambda: _button(window, "Create shift(s)"), 6) is None:
        raise InflowwError("the Shift schedule calendar did not load in Infloww")


def _button(root, label):
    return find(root, lambda n: n.ControlTypeName == "ButtonControl" and name_of(n) == label
                and visible(n))


def _confirm_button(window):
    """The Confirm button of Infloww's "Shift assignment details" box."""
    return _in_portals(window, lambda n: n.ControlTypeName == "ButtonControl"
                       and name_of(n) == "Confirm" and visible(n))


def _invoke(node, wait=0.8, what="a button"):
    if node is None:
        raise InflowwError(f"could not find {what} in Infloww - is a popup or dialog in the way?")
    try:
        node.GetInvokePattern().Invoke()
        time.sleep(wait)
    except Exception:
        click(node, wait)


def refresh(window):
    _awake(window)
    search = _button(window, "Search")
    if search is None:
        raise InflowwError('no "Search" button on the Shift schedule page')
    _invoke(search, 1.5)


# --- creator filter -----------------------------------------------------------

def _filter_combo(window, label):
    lbl = find(window, lambda n: n.ControlTypeName == "TextControl" and name_of(n) == label
               and visible(n))
    if lbl is None:
        raise InflowwError(f'no "{label}" filter on the Shift schedule page')
    ll, lt, lr, lb = rect(lbl)
    combos = [c for c in find_all(window, lambda n: n.ControlTypeName == "ComboBoxControl"
                                  and visible(n))
              if rect(c)[0] > lr and abs(rect(c)[1] - lt) < 25]
    if not combos:
        raise InflowwError(f'no dropdown next to "{label}"')
    return min(combos, key=lambda c: rect(c)[0])


def _code_prefix(code):
    return code.strip().casefold() + " - "


def _label_code(label):
    """"ATH Free - Jakara Free" -> "ath free"."""
    return label.split(" - ", 1)[0].strip().casefold()


def _portals(window):
    """Everything Infloww renders outside its main page: the Create form,
    dropdown lists, hover cards, confirmations. Each is a small sibling of
    the "app" element, so searching these instead of the whole window is
    the difference between ~0.2s and ~20s on a busy calendar - and it
    means a name on a calendar card can never be mistaken for a dropdown
    option."""
    doc = find(window, lambda n: n.ControlTypeName == "DocumentControl", 6)
    kids = doc.GetChildren() if doc is not None else []
    if not kids:
        return []
    return [c for c in kids[0].GetChildren()
            if c.AutomationId != "app" and not c.AutomationId.startswith("intercom")]


def _in_portals(window, pred, max_depth=40):
    for portal in _portals(window):
        hit = find(portal, pred, max_depth)
        if hit is not None:
            return hit
    return None


def _is_form(n):
    return n.ControlTypeName == "CustomControl" and name_of(n).startswith(
        ("Create shift", "Edit shift", "Delete"))


def _popup_options(window, pred):
    out = []
    for portal in _portals(window):
        if find(portal, _is_form, 5) is not None:
            continue
        out += find_all(portal, lambda n: n.ControlTypeName == "TextControl" and visible(n)
                        and pred(name_of(n)))
    return out


def _popup_option(window, pred):
    """A visible option in an open dropdown list (never inside a form)."""
    for portal in _portals(window):
        if find(portal, _is_form, 5) is not None:
            continue
        hit = find(portal, lambda n: n.ControlTypeName == "TextControl" and visible(n)
                   and pred(name_of(n)))
        if hit is not None:
            return hit
    return None


def _choose_creator(options, want):
    """Which creator a code means. An exact code wins ("TRI" -> "TRI -
    trippie bri"). Otherwise the code names a family like "DSTY Main" /
    "DSTY Free", and it means the one that is NOT Free - a Free page is
    only used when its code says so ("DSTY FREE"). If that still leaves
    more than one, "Main" wins; else None (ambiguous)."""
    exact = [o for o in options if _label_code(name_of(o)) == want]
    if exact:
        return exact[0]
    paid = [o for o in options if "free" not in _label_code(name_of(o)).split()]
    codes = {_label_code(name_of(o)) for o in paid}
    if len(codes) == 1:
        return paid[0]
    main = [o for o in paid if _label_code(name_of(o)) == want + " main"]
    return main[0] if main else None


def select_creator(window, code):
    """Filter the calendar to one creator; returns its full label, e.g.
    "ANT - 🍉 Antje"."""
    _awake(window)
    want = code.strip().casefold()

    def family(s):
        c = _label_code(s)
        return " - " in s and (c == want or c.startswith(want + " "))

    combo = _filter_combo(window, "Creator:")
    click(combo, 0.3)
    type_text(code.strip(), 0.2)
    if wait_for(lambda: _popup_options(window, family), 6, poll=0.1) is None:
        auto.SendKeys("{Esc}", waitTime=0.3)
        raise InflowwError(f'no creator starting with "{code} - " in Infloww')
    time.sleep(0.3)            # let the whole list arrive
    options = _popup_options(window, family)
    option = _choose_creator(options, want)
    if option is None:
        auto.SendKeys("{Esc}", waitTime=0.3)
        raise InflowwError(f'no creator "{code} - " in Infloww, and more than one to choose '
                           f'from: {", ".join(sorted({name_of(o) for o in options}))} - '
                           f'put the full code (e.g. "{_label_code(name_of(options[0])).upper()}") '
                           f'in the code box')
    label = name_of(option)
    click(option, 0.6)
    refresh(window)
    return label


# --- reading the week -----------------------------------------------------------

def _header(window):
    """[(left, day_number)] for Sun..Sat, plus the header's (top, bottom)."""
    cols = []
    for day in DAY_NAMES:
        node = find(window, lambda n: n.ControlTypeName == "TextControl" and name_of(n) == day
                    and visible(n) and rect(n)[1] > 300)
        if node is None:
            raise InflowwError("could not read the week header on the Shift schedule page")
        l, t, r, b = rect(node)
        num = find(window, lambda n: n.ControlTypeName == "TextControl" and visible(n)
                   and name_of(n).isdigit() and abs(rect(n)[0] - l) <= 4 and t < rect(n)[1] < b + 30)
        cols.append((l, int(name_of(num)) if num else None, t, rect(num)[3] if num else b))
    top = min(c[2] for c in cols)
    bottom = max(c[3] for c in cols)
    return [(c[0], c[1]) for c in cols], (top, bottom)


def _week_arrows(window, header_rows):
    top, bottom = header_rows
    imgs = [n for n in find_all(window, lambda n: n.ControlTypeName == "ImageControl" and visible(n))
            if top - 15 <= (rect(n)[1] + rect(n)[3]) // 2 <= bottom + 15]
    if len(imgs) < 2:
        raise InflowwError("could not find the week arrows on the Shift schedule page")
    imgs.sort(key=lambda n: rect(n)[0])
    return imgs[0], imgs[-1]


def go_to_week(window, sunday):
    """Page the calendar until it shows the week starting `sunday`."""
    _awake(window)
    want = [(sunday + datetime.timedelta(days=i)).day for i in range(7)]
    for _ in range(10):
        cols, rows = _header(window)
        shown = [c[1] for c in cols]
        if shown == want:
            return
        # Which Sunday is on screen? The one with that day number nearest
        # the target - enough to know which arrow to press.
        guess = min((sunday + datetime.timedelta(days=7 * k) for k in range(-8, 9)),
                    key=lambda d: (d.day != shown[0], abs((d - sunday).days)))
        # A shift's details popup, left open by the mouse resting on a
        # card, covers the -> arrow and swallows the click (seen
        # 2026-11-15): move the mouse off the calendar first.
        wl, wt, wr, wb = rect(window)
        auto.MoveTo(wl + 190, wt + 101, moveSpeed=1, waitTime=0.4)
        prev_arrow, next_arrow = _week_arrows(window, rows)
        click(next_arrow if guess < sunday else prev_arrow, 1.5)
    raise InflowwError(f"could not page the calendar to the week of {sunday.isoformat()}")


def _column(cols, x):
    for i in range(6, -1, -1):
        if x >= cols[i][0] - 25:
            return i
    return None


def _texts(node):
    return [name_of(n) for n in dfs(node, 6) if n.ControlTypeName == "TextControl"]


def _clock_pair(texts):
    times = [parse_clock(t) for t in texts if parse_clock(t)]
    return (times[0], times[-1]) if len(times) >= 2 else None


def _creator_rows(window, header_bottom, first_left):
    return [name_of(n) for n in find_all(window, lambda n: n.ControlTypeName == "TextControl"
                                         and visible(n) and rect(n)[1] > header_bottom
                                         and rect(n)[2] <= first_left and " - " in name_of(n))]


def read_shifts(window, creator_label):
    """Every shift in the displayed week for the filtered creator:
    [{day, employee, start, end, where}] - where is "card" for the ones
    shown in the day cell, "more" for ones behind "+N more"."""
    cols, (htop, hbottom) = _header(window)
    rows = _creator_rows(window, hbottom, cols[0][0])
    # Compare by code only: the dropdown and the calendar can show the same
    # creator with different names ("ATH Free - Jakara Free" in the filter,
    # "ATH Free - jakara 🖤" on the calendar).
    code = _label_code(creator_label)
    if rows and any(_label_code(r) != code for r in rows):
        raise InflowwError(f"the calendar is showing {rows}, not just {creator_label}")
    shifts, mores = [], []
    for g in find_all(window, lambda n: n.ControlTypeName == "GroupControl" and visible(n)
                      and rect(n)[1] > hbottom):
        kids = g.GetChildren()
        texts = [name_of(k) for k in kids if k.ControlTypeName == "TextControl"]
        if len(texts) == 3 and texts[0] == "+" and texts[1].isdigit() and texts[2] == "more":
            mores.append((g, int(texts[1])))
            continue
        if len(kids) != 2 or any(k.ControlTypeName != "GroupControl" for k in kids):
            continue
        name_texts = [t for t in _texts(kids[0]) if t]
        pair = _clock_pair(_texts(kids[1]))
        if len(name_texts) != 1 or pair is None:
            continue
        day = _column(cols, rect(g)[0])
        if day is None:
            continue
        shifts.append({"day": day, "employee": name_texts[0], "start": pair[0],
                       "end": pair[1], "where": "card"})
    for g, count in mores:
        day = _column(cols, rect(g)[0])
        entries = _read_more(window, g)
        if len(entries) != count:
            raise InflowwError(f"{DAY_NAMES[day]}: '+{count} more' listed {len(entries)} shifts")
        for e in entries:
            shifts.append({"day": day, "employee": e["employee"], "start": e["start"],
                           "end": e["end"], "where": "more"})
    shifts.sort(key=lambda s: (s["day"], s["start"]))
    return shifts


def _more_popup(window):
    return _in_portals(window, lambda n: n.ControlTypeName == "ToolTipControl" and visible(n)
                and re.match(r"^\d+ Shifts", name_of(n)))


def _open_more(window, group):
    more = find(group, lambda n: name_of(n) == "more", max_depth=2) or group
    l, t, r, b = rect(more)
    click((l + 8, (t + b) // 2), 0.3)
    popup = wait_for(lambda: _more_popup(window), 4, poll=0.1)
    if popup is None:
        raise InflowwError("'+N more' did not open its list of shifts")
    return popup


def _more_entries(popup):
    """[(employee, start, end, name_node, end_node)] in a '+N more' list."""
    nodes = [n for n in dfs(popup, 10) if n.ControlTypeName == "TextControl"]
    out = []
    i = 0
    while i < len(nodes):
        name = name_of(nodes[i])
        if name and not parse_clock(name) and i + 1 < len(nodes) and parse_clock(name_of(nodes[i + 1])):
            j = i + 2
            while j < len(nodes) and not parse_clock(name_of(nodes[j])):
                j += 1
            if j < len(nodes):
                out.append((name, parse_clock(name_of(nodes[i + 1])), parse_clock(name_of(nodes[j])),
                            nodes[i], nodes[j]))
                i = j + 1
                continue
        i += 1
    return out


def _close_more(window, popup):
    auto.SendKeys("{Esc}", waitTime=0.25)
    if _more_popup(window) is None:
        return
    pl, pt, pr, pb = rect(popup)
    icons = [n for n in find_all(popup, lambda n: n.ControlTypeName == "ImageControl" and visible(n), 8)
             if rect(n)[1] < pt + 40]
    if icons:
        click(max(icons, key=lambda n: rect(n)[0]), 0.6)
    if _more_popup(window) is not None:
        auto.SendKeys("{Esc}", waitTime=0.5)


def _read_more(window, group):
    popup = _open_more(window, group)
    try:
        return [{"employee": e[0], "start": e[1], "end": e[2]} for e in _more_entries(popup)]
    finally:
        _close_more(window, popup)


# --- deleting -------------------------------------------------------------------

def _dialog(window, title_prefix):
    return _in_portals(window, lambda n: n.ControlTypeName == "CustomControl" and visible(n)
                and name_of(n).startswith(title_prefix))


def _confirm_delete(window, only_this=False):
    """Answer the delete confirmation for a repeating shift - 'Only this
    shift' or 'This and all future shifts' - then Delete."""
    dlg = wait_for(lambda: _dialog(window, "Delete"), 5, poll=0.1)
    if dlg is None:
        edit = _dialog(window, "Edit shift")
        if edit is not None:
            _invoke(_button(edit, "Cancel"))
        raise InflowwError("the delete confirmation did not appear")
    if not only_this:
        # Default is "This shift only". A plain click on the radio did not
        # register (every "all future" deleted one day - seen 2026-11-15),
        # so select it through UI Automation, fall back to its label, and
        # make sure it took before pressing Delete.
        choice = find(dlg, lambda n: n.ControlTypeName == "RadioButtonControl"
                      and name_of(n) == "This and all future shifts")
        if choice is not None:
            def chosen():
                try:
                    return choice.GetSelectionItemPattern().IsSelected
                except Exception:
                    return False
            try:
                choice.GetSelectionItemPattern().Select()
            except Exception:
                pass
            if not wait_for(chosen, 0.6, poll=0.1):
                label = find(dlg, lambda n: n.ControlTypeName == "TextControl"
                             and name_of(n) == "This and all future shifts" and visible(n))
                click(label or choice, 0.2)
            if not wait_for(chosen, 1.0, poll=0.1):
                _invoke(_button(dlg, "Cancel"), 0.3)
                raise InflowwError('could not select "This and all future shifts"')
    button = _button(dlg, "Delete") or _button(dlg, "OK") or _button(dlg, "Confirm")
    if button is None:
        raise InflowwError("no Delete button in the confirmation")
    _invoke(button, 0.1)
    if wait_for(lambda: _dialog(window, "Delete") is None, 6, poll=0.1) is None:
        raise InflowwError("the delete confirmation did not close")


def _find_card(window, shift):
    # The day boxes found for creating are reused when known: reading the
    # week header again costs 14 searches per delete.
    cells = _CELLS.get(window.NativeWindowHandle)
    if cells:
        hbottom = min(c[1] for c in cells) - 1
        cols = [(c[0] + 25, None) for c in cells]
    else:
        cols, (htop, hbottom) = _header(window)
    for g in find_all(window, lambda n: n.ControlTypeName == "GroupControl" and visible(n)
                      and rect(n)[1] > hbottom):
        kids = g.GetChildren()
        if len(kids) != 2 or any(k.ControlTypeName != "GroupControl" for k in kids):
            continue
        names = [t for t in _texts(kids[0]) if t]
        pair = _clock_pair(_texts(kids[1]))
        if (len(names) == 1 and norm_name(names[0]) == norm_name(shift["employee"])
                and pair == (shift["start"], shift["end"]) and _column(cols, rect(g)[0]) == shift["day"]):
            return kids[0]
    return None


def find_card(window, shift):
    return _find_card(window, shift)


def _find_more_group(window, day):
    cols, (htop, hbottom) = _header(window)
    for g in find_all(window, lambda n: n.ControlTypeName == "GroupControl" and visible(n)
                      and rect(n)[1] > hbottom):
        texts = [name_of(k) for k in g.GetChildren() if k.ControlTypeName == "TextControl"]
        if len(texts) == 3 and texts[0] == "+" and texts[2] == "more" and _column(cols, rect(g)[0]) == day:
            return g
    return None


def delete_shift(window, shift, only_this=False):
    """Delete one shift - and, for a repeating one, every later occurrence
    unless `only_this`."""
    _awake(window)
    # Look for a card first whatever `where` said: once other shifts are
    # deleted, one that was behind "+N more" moves out into a card.
    card = _find_card(window, shift)
    if card is not None:
        l, t, r, b = rect(card)
        who = norm_name(shift["employee"])
        tip = None
        for _ in range(2):
            hover(l + 25, (t + b) // 2, 0.45)
            tip = wait_for(lambda: _in_portals(window, lambda n: n.ControlTypeName == "ToolTipControl"
                                        and visible(n) and "UTC" in name_of(n)), 3, poll=0.1)
            if tip is None:
                continue
            # The popup shows whose shift it is - only delete if it is the
            # one meant (cards still moving after a delete can put the
            # mouse on a neighbour).
            if who in [norm_name(name_of(n)) for n in dfs(tip, 10) if n.ControlTypeName == "TextControl"]:
                break
            auto.MoveTo(l + 25, b + 120, moveSpeed=1, waitTime=0.4)
            card = _find_card(window, shift)
            if card is None:
                raise ShiftGone(f"{shift['employee']}'s shift on {DAY_NAMES[shift['day']]} is already gone")
            l, t, r, b = rect(card)
            tip = None
        if tip is None:
            raise InflowwError(f"hovering {shift['employee']}'s shift did not open its details")
        tl, tt, tr, tb = rect(tip)
        icons = sorted([n for n in find_all(tip, lambda n: n.ControlTypeName == "ImageControl"
                                            and visible(n), 6) if rect(n)[1] < tt + 45],
                       key=lambda n: rect(n)[0])
        if len(icons) < 2:
            raise InflowwError("no delete icon in the shift's details")
        click(icons[-2], 0.2)          # edit, delete, close
        _confirm_delete(window, only_this)
        return
    group = _find_more_group(window, shift["day"])
    if group is None:
        raise ShiftGone(f"{shift['employee']}'s shift on {DAY_NAMES[shift['day']]} is already gone")
    popup = _open_more(window, group)
    entry = next((e for e in _more_entries(popup)
                  if norm_name(e[0]) == norm_name(shift["employee"])
                  and (e[1], e[2]) == (shift["start"], shift["end"])), None)
    if entry is None:
        _close_more(window, popup)
        raise ShiftGone(f"{shift['employee']} is not in {DAY_NAMES[shift['day']]}'s '+N more' list")
    top, bottom = rect(entry[3])[1] - 4, rect(entry[4])[3] + 4
    icons = sorted([n for n in find_all(popup, lambda n: n.ControlTypeName == "ImageControl"
                                        and visible(n), 8)
                    if top <= (rect(n)[1] + rect(n)[3]) // 2 <= bottom], key=lambda n: rect(n)[0])
    if len(icons) < 2:
        _close_more(window, popup)
        raise InflowwError("no delete icon in the '+N more' list")
    click(icons[1], 0.2)               # edit, delete
    _confirm_delete(window, only_this)


# --- creating -------------------------------------------------------------------

def _dialog_field_bottom(dlg, label):
    node = find(dlg, lambda n: n.ControlTypeName == "TextControl" and name_of(n) == label)
    if node is None:
        raise InflowwError(f'no "{label}" in the Create shift(s) form')
    return rect(node)[3]


_DATE_SHIFT = None    # how far Infloww moves a typed date, once seen


def _set_date(edit, iso):
    """Type a date into a date box.

    Infloww reads a typed date as midnight on this PC's clock and shows it
    in its own time zone, so with Infloww behind the PC (UTC-06:00 vs
    Norway) "2026-10-25" usually comes out as 2026-10-24 - and around a
    clock change the offset is not even consistent (26 -> 25 but 27 -> 27).
    Whatever the box shows is what gets saved, so nearby dates are typed
    until it shows the one wanted.

    The box only updates a moment after Enter, so it is read once it has
    settled - reading it straight away gave the OLD value, which made a
    right date look wrong and a wrong one look right (seen 2026-11-08:
    "2026-11-15->2026-11-13").
    """
    global _DATE_SHIFT
    if _edit_value(edit) == iso:
        return
    want = datetime.date.fromisoformat(iso)
    tried, typed_already = [], set()
    candidates = [want + datetime.timedelta(days=d) for d in (0, 1, -1, 2, -2)]
    if _DATE_SHIFT is not None:
        # Seen already on this run: type the date that lands right first.
        candidates.insert(0, want - _DATE_SHIFT)
    while candidates and len(tried) < 5:
        typed = candidates.pop(0)
        if typed in typed_already:
            continue
        typed_already.add(typed)
        before = _edit_value(edit)
        click(edit, 0.1)
        select_all_and_type(typed.isoformat(), 0.05)
        auto.SendKeys("{Enter}", waitTime=0.1)
        shown = _settled_date(edit, before)
        if shown:
            _DATE_SHIFT = datetime.date.fromisoformat(shown) - typed
        if shown == iso:
            return
        tried.append(f"{typed.isoformat()}->{shown}")
        if shown:
            # Infloww moved it by a fixed amount: type the date that lands
            # on the wanted one next.
            candidates.insert(0, want - (datetime.date.fromisoformat(shown) - typed))
    raise InflowwError(f"could not set a date to {iso} ({', '.join(tried)})")


def _settled_date(edit, before, timeout=2.5):
    """The date box's value once it stops changing: the same date read
    twice 0.3s apart, and either different from `before` or unchanged
    for over a second (the typed date may land back on the old value)."""
    start, last = time.time(), None
    while True:
        value = _edit_value(edit) or ""
        good = bool(_ISO_DATE.match(value))
        if good and value == last and (value != before or time.time() - start >= 0.8):
            return value
        last = value if good else None
        if time.time() - start >= timeout:
            return value if good else None
        time.sleep(0.12)


def _edit_value(edit):
    try:
        return edit.GetValuePattern().Value
    except Exception:
        return name_of(edit)


def _combo_text(dlg, combo):
    cl, ct, cr, cb = rect(combo)
    texts = [n for n in find_all(dlg, lambda n: n.ControlTypeName == "TextControl" and visible(n))
             if abs(rect(n)[1] - ct) < 15 and cl - 5 <= rect(n)[0] <= cr]
    return name_of(texts[0]) if texts else ""


_HOUR_ITEM = re.compile(r"^\d{1,2} (AM|PM)$")


def _time_columns(window, combo):
    """The time picker's (hour column, minute column) once it is open: a
    panel with two scrolling lists, "12 AM".."11 PM" and "00".."59"."""
    cl, ct, cr, cb = rect(combo)
    nodes = []
    for portal in _portals(window):
        if find(portal, _is_form, 5) is None:
            nodes += find_all(portal, lambda n: n.ControlTypeName == "TextControl"
                              and _HOUR_ITEM.match(name_of(n)) and visible(n))
    for node in nodes:
        # Under or over the box, whichever has room - but lined up with it.
        if not cl - 20 <= rect(node)[0] <= cr:
            continue
        hours = node.GetParentControl().GetParentControl()
        panel = hours.GetParentControl()
        cols = panel.GetChildren()
        if len(cols) == 2:
            return cols[0], cols[1]
    return None


def _column_items(column):
    out = []
    for item in column.GetChildren():
        text = find(item, lambda n: n.ControlTypeName == "TextControl" and name_of(n), 3)
        if text is not None:
            out.append((name_of(text), text))
    return out


def _pick_in_column(window, combo, which, index):
    """Scroll picker column `which` (0 = hours, 1 = minutes) until item
    `index` is in view, then click it.

    By position, not by label: on the day the clocks change, Infloww
    lists "2 AM" twice but still sets the hour from the item's position,
    so the item labelled "10 AM" gives 11 AM. The list is re-read after
    every scroll because Infloww replaces its items as it scrolls.
    """
    for _ in range(30):
        cols = _time_columns(window, combo)
        if cols is None:
            raise InflowwError("the time picker closed while choosing a time")
        column = cols[which]
        items = _column_items(column)
        if not 0 <= index < len(items):
            raise InflowwError(f"the time picker has no item {index}")
        col = rect(column)
        l, t, r, b = rect(items[index][1])
        if r > l and t >= col[1] - 2 and b <= col[3] + 2:
            click(items[index][1], 0.4)
            return
        shown = [i for i, (_, n) in enumerate(items)
                 if rect(n)[2] > rect(n)[0] and col[1] - 2 <= rect(n)[1] <= col[3]]
        auto.MoveTo((col[0] + col[2]) // 2, (col[1] + col[3]) // 2, moveSpeed=1, waitTime=0.05)
        if not shown or index > max(shown):
            auto.WheelDown(2, waitTime=0.4)
        else:
            auto.WheelUp(2, waitTime=0.4)
    raise InflowwError(f"could not scroll the time picker to item {index}")


def _set_time(window, dlg, combo, hhmm):
    want = clock_12h(hhmm)
    hour, minute = int(hhmm[:2]), int(hhmm[3:])
    title = find(dlg, lambda n: name_of(n) == "Create shift(s)" and n.ControlTypeName == "TextControl")
    hour_index, minute_index = hour, minute
    for _ in range(3):
        click(combo, 0.6)
        cols = wait_for(lambda: _time_columns(window, combo), 4)
        if cols is None:
            raise InflowwError("the time picker did not open")
        _pick_in_column(window, combo, 0, hour_index)
        _pick_in_column(window, combo, 1, minute_index)
        if _time_columns(window, combo) is not None:
            click(title, 0.4)
        got = parse_clock(_combo_text(dlg, combo))
        if got == hhmm:
            return
        if got is None:
            continue
        # Whatever the labels say, step by how far off the result was.
        hour_index += hour - int(got[:2])
        minute_index += minute - int(got[3:])
    raise InflowwError(f"could not set a time to {want} (shows {_combo_text(dlg, combo)!r})")


def _set_time_by_label(window, dlg, combo, hhmm):
    """For days the PC's clock changes: pick the hour item whose LABEL is
    the hour wanted (first match - "2 AM" is listed twice). That is what
    Infloww saves, although the box itself may then read an hour later."""
    hour, minute = int(hhmm[:2]), int(hhmm[3:])
    label = f"{(hour % 12) or 12} {'AM' if hour < 12 else 'PM'}"
    title = find(dlg, lambda n: name_of(n) == "Create shift(s)" and n.ControlTypeName == "TextControl")
    click(combo, 0.6)
    cols = wait_for(lambda: _time_columns(window, combo), 4)
    if cols is None:
        raise InflowwError("the time picker did not open")
    names = [n for n, _ in _column_items(cols[0])]
    if label not in names:
        raise InflowwError(f'"{label}" is not in the time picker on this day')
    _pick_in_column(window, combo, 0, names.index(label))
    _pick_in_column(window, combo, 1, minute)
    if _time_columns(window, combo) is not None:
        click(title, 0.4)


def _pick_option(window, dlg, label, typed, matches, tag_matches, check=True):
    """Type into a multi-select under `label` and click the matching
    option; then confirm a tag for it is in the field."""
    bottom = _dialog_field_bottom(dlg, label)
    field = min((n for n in find_all(dlg, lambda n: n.ControlTypeName == "ComboBoxControl"
                                     and visible(n)) if rect(n)[1] > bottom),
                key=lambda n: rect(n)[1], default=None)
    if field is None:
        raise InflowwError(f'no input under "{label}"')
    fl, ft, fr, fb = rect(field)
    click((rect(dlg)[0] + 40, (ft + fb) // 2), 0.1)
    type_text(typed, 0.1)
    option = wait_for(lambda: _popup_option(window, matches), 6, poll=0.05)
    if option is None:
        auto.SendKeys("{Ctrl}a{Delete}", waitTime=0.2)
        raise InflowwError(f'"{typed}" is not an option under {label}')
    time.sleep(0.12)           # the list slides into place as it opens
    option = _popup_option(window, matches) or option
    picked = name_of(option)
    opened_above = rect(option)[1] < ft
    click(option, 0.12)
    # Close the dropdown without Esc (Esc would close the whole form), by
    # clicking empty form space on the side away from the list: a list
    # that opened upwards covers the title, and a click there lands on the
    # option just picked - which un-picks it.
    dl, dt, dr, db = rect(dlg)
    click((dl + 25, db - 30) if opened_above else (dl + 25, dt + 30), 0.1)
    if not check:
        return picked
    tag = find(dlg, lambda n: n.ControlTypeName == "TextControl" and visible(n)
               and abs(rect(n)[1] - ft) < 30 and tag_matches(name_of(n)))
    if tag is None:
        raise InflowwError(f'picked "{picked}" under {label} but it did not stick')
    return picked


def _day_button_ticked(text_node):
    l, t, r, b = rect(text_node)
    red, green, blue = pixel(l - 6, (t + b) // 2)
    return blue > 170 and blue > red + 70


def _set_repeat_days(dlg, days):
    want = set(days)
    for _ in range(3):
        nodes = {d: find(dlg, lambda n, d=d: n.ControlTypeName == "TextControl"
                         and name_of(n) == DAY_NAMES[d] and visible(n)) for d in range(7)}
        if any(v is None for v in nodes.values()):
            raise InflowwError("could not find the Repeat on weekday buttons")
        wrong = [d for d in range(7) if _day_button_ticked(nodes[d]) != (d in want)]
        if not wrong:
            return
        for d in wrong:
            click(nodes[d], 0.3)
        time.sleep(0.3)
    raise InflowwError("could not tick the right Repeat on weekdays")


def _presets(dlg):
    """[(start, end, group)] for the blue preset chips, hidden ones too."""
    out = []
    for g in find_all(dlg, lambda n: n.ControlTypeName == "GroupControl", 12):
        texts = [name_of(k) for k in g.GetChildren() if k.ControlTypeName == "TextControl"]
        if len(texts) == 3 and texts[1] == "-" and parse_clock(texts[0]) and parse_clock(texts[2]):
            out.append((parse_clock(texts[0]), parse_clock(texts[2]), g))
    return out


def _click_preset(dlg, start, end):
    """Click the preset for start-end, paging the chip row with its < >
    arrows when it is off to one side. False when there is no such preset.

    Chips scrolled out of the row still report a normal on-screen
    position (just under the arrows or past the form's edge), so a chip
    only counts as clickable when it sits fully between the arrows.
    """
    dl, dt, dr, db = rect(dlg)
    for _ in range(10):
        presets = _presets(dlg)
        match = next((g for s, e, g in presets if (s, e) == (start, end)), None)
        if match is None:
            return False
        row = [g for s, e, g in presets if visible(g)]
        if not row:
            return False
        top, bottom = rect(row[0])[1], rect(row[0])[3]
        arrows = sorted([n for n in find_all(dlg, lambda n: n.ControlTypeName == "ImageControl"
                                             and visible(n), 12)
                         if top - 10 <= (rect(n)[1] + rect(n)[3]) // 2 <= bottom + 10],
                        key=lambda n: rect(n)[0])
        mid = (dl + dr) // 2
        left = [a for a in arrows if rect(a)[0] < mid]
        right = [a for a in arrows if rect(a)[0] >= mid]
        lo = rect(left[-1])[2] if left else dl
        hi = rect(right[0])[0] if right else dr - 15
        ml, mt, mr, mb = rect(match)
        if mr > ml and ml >= lo - 2 and mr <= hi + 2:
            click(match, 0.12)
            return True
        if right and (mr <= ml or mr > hi + 2):
            # Partly under the right arrow ("6:00 PM-2:0..."), which
            # swallows clicks: one arrow click brings it in. Follow this
            # chip's own position rather than re-scanning the whole row
            # (the re-scans made this take 15s).
            for _ in range(4):
                click(right[0], 0.3)
                ml, mt, mr, mb = rect(match)
                arrow_left = rect(right[0])[0] if visible(right[0]) else 10 ** 6
                if mr > ml and mr <= arrow_left + 2:
                    click(match, 0.12)
                    return True
            continue
        if (mr <= ml or mr > hi) and right:
            click(right[0], 0.6)
        elif left:
            click(left[-1], 0.6)
        else:
            return False
    return False


def create_shift(window, code, creator_label, group, sunday, crosses, dry_fill=False, keep_open=False):
    """One weekly-repeating shift: `group` = {employee, start, end, days}.
    Runs from the first day it is worked to the end of the week (the Sunday
    after, for overnight shifts - see until_day)."""
    _awake(window)
    # A form or box left open by an earlier attempt blocks everything.
    leftover = _dialog(window, "Create shift")
    if leftover is not None and _button(leftover, "Cancel") is not None:
        _invoke(_button(leftover, "Cancel"), 1.0)
    _invoke(_button(window, "Create shift(s)"), 1.0, '"Create shift(s)"')
    dlg = wait_for(lambda: _dialog(window, "Create shift"), 6)
    if dlg is None:
        raise InflowwError("the Create shift(s) form did not open")
    try:
        first = sunday + datetime.timedelta(days=group["days"][0])
        end_day = first + datetime.timedelta(days=1 if crosses else 0)
        # Infloww keeps a repeat only if it ENDS by this date, so an
        # overnight Saturday shift needs the Sunday after. That Sunday's
        # own overnight shift would end on Monday, so it is not added.
        until_day = sunday + datetime.timedelta(days=7 if crosses else 6)

        dates = sorted([n for n in find_all(dlg, lambda n: n.ControlTypeName == "EditControl"
                                            and visible(n)) if _ISO_DATE.match(_edit_value(n) or "")],
                       key=lambda n: (rect(n)[1], rect(n)[0]))[:2]
        if len(dates) != 2:
            raise InflowwError("could not find the shift's start and end dates")
        combos = sorted([n for n in find_all(dlg, lambda n: n.ControlTypeName == "ComboBoxControl"
                                             and visible(n))
                         if abs(rect(n)[1] - rect(dates[0])[1]) < 20], key=lambda n: rect(n)[0])
        if len(combos) != 2:
            raise InflowwError("could not find the shift's start and end times")
        # A blue preset sets both times in one click; without one that
        # matches exactly, the time boxes are set by hand.
        odd_day = group.get("clock_change", False)
        preset = False if odd_day else _click_preset(dlg, group["start"], group["end"])
        _set_date(dates[0], first.isoformat())
        _set_date(dates[1], end_day.isoformat())
        for combo, hhmm in ((combos[0], group["start"]), (combos[1], group["end"])):
            if odd_day:
                _set_time_by_label(window, dlg, combo, hhmm)
            elif not preset or parse_clock(_combo_text(dlg, combo)) != hhmm:
                _set_time(window, dlg, combo, hhmm)

        until = None
        prefix = _code_prefix(code)
        _pick_option(window, dlg, "Creator(s)", code.strip() + " - ",
                     lambda s: s.casefold().startswith(prefix),
                     lambda s: s.casefold().startswith(prefix))
        who = norm_name(group["employee"])
        try:
            _pick_option(window, dlg, "Employee(s)", group["employee"],
                         lambda s: norm_name(s) == who, lambda s: norm_name(s) == who)
        except InflowwError as e:
            # A deactivated employee may be missing from the list.
            raise EmployeeNotPicked(group["employee"], str(e)) from None

        # A one-off shift (group["repeat"] False) keeps "No repeat".
        if group.get("repeat", True):
            repeat = find(dlg, lambda n: n.ControlTypeName == "TextControl" and visible(n)
                          and name_of(n) in ("No repeat", "Repeat weekly"))
            if repeat is None:
                raise InflowwError("could not find Repeat Options")
            if name_of(repeat) != "Repeat weekly":
                click(repeat, 0.6)
                weekly = wait_for(lambda: _popup_option(window, lambda s: s == "Repeat weekly"), 4)
                if weekly is None:
                    raise InflowwError('no "Repeat weekly" option')
                click(weekly, 0.8)
            _set_repeat_days(dlg, group["days"])

            on = find(dlg, lambda n: n.ControlTypeName == "RadioButtonControl" and name_of(n) == "On")
            if on is None:
                raise InflowwError('no "Repeat ends: On" option')
            click(on, 0.4)
            until = find(dlg, lambda n: n.ControlTypeName == "EditControl" and visible(n)
                         and rect(n)[1] > rect(on)[1] - 15 and rect(n)[0] > rect(on)[2])
            if until is None:
                raise InflowwError("no end date box for the repeat")
            _set_date(until, until_day.isoformat())

        # Last look before saving: fields can reset each other, so every
        # date box is checked (and set again once if it moved), not just
        # the start date. (On a clock-change day the time boxes may read an
        # hour off by design; the calendar is read back after saving.)
        boxes = [("start date", dates[0], first), ("end date", dates[1], end_day)]
        if until is not None:
            boxes.append(("repeat end date", until, until_day))
        for label, box, day in boxes:
            if _edit_value(box) != day.isoformat():
                _set_date(box, day.isoformat())
        wrong = [f"{label} shows {_edit_value(box)}, not {day.isoformat()}"
                 for label, box, day in boxes if _edit_value(box) != day.isoformat()]
        if not odd_day:
            for label, combo, hhmm in (("start time", combos[0], group["start"]),
                                       ("end time", combos[1], group["end"])):
                if parse_clock(_combo_text(dlg, combo)) != hhmm:
                    wrong.append(f"{label} shows {_combo_text(dlg, combo)!r}, not {clock_12h(hhmm)}")
        if wrong:
            raise InflowwError("the form changed while it was being filled: " + "; ".join(wrong))

        if dry_fill:
            return creator_label
        before = set(_portal_texts(window))
        _invoke(_button(dlg, "OK"), 1.5, 'the form\'s "OK" button')

        def after_ok():
            if _dialog(window, "Create shift") is None:
                return "closed"
            if _confirm_button(window) is not None:
                return "confirm"
            return None

        state = wait_for(after_ok, 8)
        if state == "confirm":
            # "Shift assignment details: Employee X isn't currently assigned
            # to JBR. Scheduling them for this shift will grant them access
            # to the Creator." - Confirm is what we want.
            _invoke(_confirm_button(window), 1.5, '"Confirm"')
            state = wait_for(lambda: _dialog(window, "Create shift") is None and "closed", 8)
        if state != "closed":
            said = [t for t in _portal_texts(window) if t not in before]
            raise ShiftRejected(group["employee"], said)
        return creator_label
    finally:
        still = None if keep_open else _dialog(window, "Create shift")
        if still is not None:
            cancel = _button(still, "Cancel")
            if cancel is not None:
                _invoke(cancel, 0.8)


def _portal_texts(window):
    """Every visible text in Infloww's popups and forms - used to catch
    what Infloww says when it refuses a form."""
    out = []
    for portal in _portals(window):
        out += [name_of(n) for n in find_all(portal, lambda n: n.ControlTypeName == "TextControl"
                                             and visible(n) and name_of(n))]
    return out


# --- Manage employees: reactivating a chatter -------------------------------------

def _employee_search_box(window):
    return find(window, lambda n: n.ControlTypeName == "EditControl"
                and n.AutomationId == "employee" and visible(n))


def open_manage_employees(window):
    _wait_page_loaded(window)
    if _employee_search_box(window) is None:
        _open_employees_item(window, "Manage employees", _employee_search_box)


_STATUSES = ("Activated", "Deactivated")


def _employee_row(window, employee):
    """(status, row) for the employee's row in the Manage employees table,
    or None. A row's cells: tick box, Employee, Assigned Creators, Role,
    Status, Actions."""
    who = norm_name(employee)
    for row in find_all(window, lambda n: n.ControlTypeName == "CustomControl" and visible(n)):
        cells = [c for c in row.GetChildren() if c.ControlTypeName == "DataItemControl"]
        if len(cells) < 5 or not any(norm_name(name_of(c)) == who for c in cells[:3]):
            continue
        status = next((name_of(c) for c in cells if name_of(c) in _STATUSES), None)
        if status:
            return status, row
    return None


def _search_employee(window, employee):
    box = _employee_search_box(window)
    if box is None:
        raise InflowwError('no "By employee" search box on Manage employees')
    click(box, 0.4)
    select_all_and_type(employee, 0.4)
    search = _button(window, "Search")
    if search is None:
        auto.SendKeys("{Enter}", waitTime=1.5)
    else:
        _invoke(search, 1.5)
    return wait_for(lambda: _employee_row(window, employee), 6)


def ensure_employee_active(window, employee):
    """Manage employees > search the employee > if Deactivated: hover
    More > Activate > Activate. Returns "activated", "already active" or
    "not found". Leaves Infloww on Manage employees."""
    open_manage_employees(window)
    found = _search_employee(window, employee)
    if found is None:
        return "not found"
    status, row = found
    if status == "Activated":
        return "already active"
    more = find(row, lambda n: name_of(n) == "More" and visible(n), 6)
    if more is None:
        raise InflowwError(f'no "More" next to {employee} on Manage employees')
    hover(*center(more), settle=1.0)
    item = wait_for(lambda: _in_portals(window, lambda n: n.ControlTypeName in
                                        ("TextControl", "MenuItemControl")
                                        and name_of(n) == "Activate" and visible(n)), 4)
    if item is None:
        raise InflowwError(f'hovering "More" for {employee} did not show "Activate"')
    click(item, 1.0)
    dlg = wait_for(lambda: _dialog(window, "Activate employee"), 5)
    if dlg is None:
        raise InflowwError(f'the "Activate employee?" box did not appear for {employee}')
    button = _button(dlg, "Activate")
    if button is None:
        raise InflowwError('no "Activate" button in the "Activate employee?" box')
    _invoke(button, 1.5)
    if wait_for(lambda: _dialog(window, "Activate employee") is None, 6) is None:
        raise InflowwError('the "Activate employee?" box did not close')
    # Read it back.
    for _ in range(3):
        found = _search_employee(window, employee)
        if found and found[0] == "Activated":
            return "activated"
        time.sleep(1.0)
    raise InflowwError(f"activated {employee}, but Manage employees still does not show them as Activated")



# --- fast create: from the creator's own row ----------------------------------------
#
# Clicking the empty grey space in a day's box on the creator's row opens
# "Create shift(s)" with that creator and that date already filled in
# (0.4s). The blue time button sets both times - and, for an overnight
# shift, the next-day end date. So per shift only the employee, the
# repeat and the repeat's end date are left to fill. The form is filled
# straight from the site's data, without reading it back; the week is read
# once after the account instead.

_NOTICE = re.compile(r"please|required|error|fail|invalid|conflict|overlap|already|not |can't|cannot|"
                     r"isn't|exceed|limit", re.I)

_CELLS = {}      # window handle -> [(l, t, r, b)] of Sun..Sat on the creator's row


def _day_cells(window, fresh=False):
    key = window.NativeWindowHandle
    if not fresh and key in _CELLS:
        return _CELLS[key]
    cols, (htop, hbottom) = _header(window)
    width = cols[1][0] - cols[0][0]
    cells = [None] * 7
    for g in find_all(window, lambda n: n.ControlTypeName == "GroupControl" and visible(n)
                      and hbottom - 5 <= rect(n)[1] <= hbottom + 60, 40):
        l, t, r, b = rect(g)
        if r - l < width * 0.9 or b - t < 60:
            continue
        day = _column(cols, l + 10)
        if day is not None and (cells[day] is None or b > cells[day][3]):
            cells[day] = (l, t, r, b)
    if any(c is None for c in cells):
        raise InflowwError("could not find the day boxes on the creator's row")
    _CELLS[key] = cells
    return cells


def forget_cells(window):
    _CELLS.pop(window.NativeWindowHandle, None)


def _open_form_on_day(window, day):
    for fresh in (False, True):
        l, t, r, b = _day_cells(window, fresh)[day]
        # The strip under the cards and "+N more", away from both edges.
        auto.Click(l + (r - l) * 2 // 3, b - 9, waitTime=0.05)
        dlg = wait_for(lambda: _dialog(window, "Create shift"), 4, poll=0.05)
        if dlg is not None:
            return dlg
    raise InflowwError(f"clicking {DAY_NAMES[day]}'s empty space did not open Create shift(s)")


def create_shift_fast(window, group, sunday, crosses):
    """One weekly-repeating shift, from the creator's row: `group` =
    {employee, start, end, days}. Repeats on its days and ends on the
    Saturday (the Sunday after, for overnight shifts)."""
    bring_to_front(window.NativeWindowHandle)
    leftover = _dialog(window, "Create shift")
    if leftover is not None and _button(leftover, "Cancel") is not None:
        _invoke(_button(leftover, "Cancel"), 0.8)
    dlg = _open_form_on_day(window, group["days"][0])
    try:
        if not _click_preset(dlg, group["start"], group["end"]):
            # No blue button for these times: set them by hand.
            combos = sorted([n for n in find_all(dlg, lambda n: n.ControlTypeName == "ComboBoxControl"
                                                 and visible(n))], key=lambda n: (rect(n)[1], rect(n)[0]))[:2]
            for combo, hhmm in zip(combos, (group["start"], group["end"])):
                _set_time(window, dlg, combo, hhmm)
            if crosses:
                dates = sorted([n for n in find_all(dlg, lambda n: n.ControlTypeName == "EditControl"
                                                    and visible(n) and _ISO_DATE.match(_edit_value(n) or ""))],
                               key=lambda n: (rect(n)[1], rect(n)[0]))
                _set_date(dates[1], (sunday + datetime.timedelta(days=group["days"][0] + 1)).isoformat())
        who = norm_name(group["employee"])
        try:
            _pick_option(window, dlg, "Employee(s)", group["employee"],
                         lambda s: norm_name(s) == who, lambda s: norm_name(s) == who, check=False)
        except InflowwError as e:
            raise EmployeeNotPicked(group["employee"], str(e)) from None
        # One day only: leave "No repeat" - OK makes just the day clicked.
        if group.get("repeat", True) and len(group["days"]) > 1:
            repeat = find(dlg, lambda n: n.ControlTypeName == "TextControl" and visible(n)
                          and name_of(n) == "No repeat")
            if repeat is not None:
                click(repeat, 0.1)
                weekly = wait_for(lambda: _popup_option(window, lambda s: s == "Repeat weekly"), 3, poll=0.05)
                if weekly is None:
                    raise InflowwError('no "Repeat weekly" option')
                click(weekly, 0.15)
            want = set(group["days"])
            nodes = {}
            for n in find_all(dlg, lambda n: n.ControlTypeName == "TextControl"
                              and name_of(n) in DAY_NAMES and visible(n)):
                nodes.setdefault(DAY_NAMES.index(name_of(n)), n)
            if len(nodes) != 7:
                raise InflowwError("could not find the Repeat on weekday buttons")
            for d in range(7):
                if _day_button_ticked(nodes[d]) != (d in want):
                    click(nodes[d], 0.08)
            on = find(dlg, lambda n: n.ControlTypeName == "RadioButtonControl" and name_of(n) == "On")
            if on is None:
                raise InflowwError('no "Repeat ends: On" option')
            click(on, 0.08)
            until = find(dlg, lambda n: n.ControlTypeName == "EditControl" and visible(n)
                         and rect(n)[1] > rect(on)[1] - 15 and rect(n)[0] > rect(on)[2])
            if until is None:
                raise InflowwError("no end date box for the repeat")
            last = 7 if crosses and not group.get("until_saturday") else 6
            _set_date(until, (sunday + datetime.timedelta(days=last)).isoformat())
        _invoke(_button(dlg, "OK"), 0.05, 'the form OK button')

        def after_ok():
            if _dialog(window, "Create shift") is None:
                return "closed"
            if _confirm_button(window) is not None:
                return "confirm"
            return None

        state = wait_for(after_ok, 8, poll=0.08)
        if state == "confirm":
            _invoke(_confirm_button(window), 0.05, '"Confirm"')
            state = wait_for(lambda: _dialog(window, "Create shift") is None and "closed", 8, poll=0.08)
        if state != "closed":
            said = [t for t in _portal_texts(window) if _NOTICE.search(t)]
            raise ShiftRejected(group["employee"], said)
    finally:
        still = _dialog(window, "Create shift")
        if still is not None and _button(still, "Cancel") is not None:
            _invoke(_button(still, "Cancel"), 0.6)
