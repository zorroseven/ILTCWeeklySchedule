"""
Reads the ILTC scheduling site out of the Chrome tab it is open in.

The page's own text, through Chrome's accessibility tree - no OCR, so
names like "a6 kin" or "blxxs" come through exactly. Chrome only builds
that tree while its window is in the foreground, so the window is brought
forward first.
"""
import re
import time
import uiautomation as auto

from schedule_shared import chrome_windows, SITE_CLOSED
from uia_util import (find, find_all, name_of, activate, wait_for,
                      wake_accessibility)

TAB_PREFIX = "Scheduling - Admin"
WEEK_BUTTON = "View week schedule"
POPUP_TITLE = "Account · Week Schedule"
_DAY_LABEL = re.compile(r"^(Sun|Mon|Tues|Wednes|Thurs|Fri|Satur)day\s+\d{1,2}(st|nd|rd|th)$")


def open_site():
    """The scheduling page's document, with its tab selected and Chrome in
    front. Returns (document, None) or (None, problem)."""
    for hwnd, title, cls in chrome_windows():
        window = auto.ControlFromHandle(hwnd)
        tab = find(window, lambda n: n.ControlTypeName == "TabItemControl"
                   and name_of(n).startswith(TAB_PREFIX), max_depth=16)
        if tab is None and not title.startswith(TAB_PREFIX):
            continue
        if tab is not None and not title.startswith(TAB_PREFIX):
            try:
                tab.GetSelectionItemPattern().Select()
            except Exception:
                tab.Click(simulateMove=False)
            time.sleep(1.0)
        for _ in range(6):
            activate(window, 0.8)
            doc = find(window, lambda n: n.ControlTypeName == "DocumentControl"
                       and n.AutomationId == "RootWebArea", max_depth=16)
            if doc is not None and find(doc, lambda n: name_of(n) == WEEK_BUTTON) is not None:
                return doc, None
            # An open modal (a chatter's profile, say) hides the table from
            # the accessibility tree; Esc closes it.
            if doc is not None and find(doc, lambda n: n.ControlTypeName == "WindowControl",
                                        max_depth=4) is not None:
                auto.SendKeys("{Esc}", waitTime=0.8)
            wake_accessibility(hwnd)
        return None, ("The scheduling tab is open but its account table did not load - "
                      "is it logged in and on the Scheduling page?")
    return None, SITE_CLOSED


def _account_cells(doc):
    return find_all(doc, lambda n: n.ControlTypeName == "DataItemControl"
                    and WEEK_BUTTON in name_of(n))


def _cell_account_name(cell):
    text = find(cell, lambda n: n.ControlTypeName == "TextControl" and name_of(n), max_depth=3)
    return name_of(text)


def list_accounts(doc):
    """[{name, section, number}] in page order. Section headers are rows
    with one wide cell, like "☀️MIKKEL☀️"."""
    table = find(doc, lambda n: n.ControlTypeName == "TableControl"
                 and find(n, lambda c: WEEK_BUTTON in name_of(c), max_depth=4) is not None)
    if table is None:
        return []
    accounts, section = [], ""
    for row in table.GetChildren():
        cells = row.GetChildren()
        if not cells:
            continue
        names = [name_of(c) for c in cells]
        account_cell = next((c for c in cells if WEEK_BUTTON in name_of(c)), None)
        if account_cell is not None:
            accounts.append({"name": _cell_account_name(account_cell), "section": section,
                             "number": names[0]})
        elif len(cells) == 2 and not names[0] and names[1]:
            section = _section_name(names[1])
    return accounts


# Buttons inside a section header cell, which the cell's text includes:
# "Drag to reorder ☀️MIKKEL☀️ Edit header Delete header".
_HEADER_CONTROLS = ("Drag to reorder", "Edit header", "Delete header")


def _section_name(text):
    for word in _HEADER_CONTROLS:
        text = text.replace(word, " ")
    return " ".join(text.split())


def _popup_title(doc):
    return find(doc, lambda n: n.ControlTypeName == "TextControl" and name_of(n) == POPUP_TITLE)


def close_popup(doc):
    for _ in range(3):
        if _popup_title(doc) is None:
            return
        close = find(doc, lambda n: n.ControlTypeName == "ButtonControl" and name_of(n) == "Close")
        if close is None:
            auto.SendKeys("{Esc}", waitTime=0.5)
            continue
        try:
            close.GetInvokePattern().Invoke()
        except Exception:
            close.Click(simulateMove=False)
        time.sleep(0.6)


def read_week(doc, account):
    """One account's week from its "View week schedule" popup:
    {"days": ["Sunday 25th", ...], "slots": {"Grave": [7 names], ...}}."""
    close_popup(doc)
    cell = next((c for c in _account_cells(doc) if _cell_account_name(c) == account), None)
    if cell is None:
        raise LookupError(f'"{account}" is not on the scheduling page')
    button = find(cell, lambda n: n.ControlTypeName == "ButtonControl"
                  and name_of(n) == WEEK_BUTTON, max_depth=6)
    try:
        button.GetInvokePattern().Invoke()
    except Exception:
        button.Click(simulateMove=False)

    def popup_table():
        if _popup_title(doc) is None:
            return None
        for table in find_all(doc, lambda n: n.ControlTypeName == "TableControl"):
            rows = table.GetChildren()
            if rows and sum(1 for c in rows[0].GetChildren() if _DAY_LABEL.match(name_of(c))) == 7:
                return table
        return None

    table = wait_for(popup_table, timeout=8)
    if table is None:
        raise RuntimeError(f'the week schedule popup for "{account}" did not open')
    try:
        heading = [name_of(n) for n in find_all(doc, lambda n: n.ControlTypeName == "TextControl")]
        shown = heading[heading.index(POPUP_TITLE) + 1] if POPUP_TITLE in heading else ""
        # The account list can mark a name with "*" ("TrippieBRI FREE*")
        # that the popup's title leaves off.
        if shown and shown.rstrip("* ") != account.rstrip("* "):
            raise RuntimeError(f'opened the week for "{shown}" instead of "{account}"')
        rows = table.GetChildren()
        days = [name_of(c) for c in rows[0].GetChildren() if _DAY_LABEL.match(name_of(c))]
        slots = {}
        for row in rows[1:]:
            cells = row.GetChildren()
            if len(cells) < 8:
                continue
            label = name_of(cells[0])
            slots[label] = [name_of(c) for c in cells[1:8]]
        return {"days": days, "slots": slots}
    finally:
        close_popup(doc)


# --- fast reading: many weeks in one pass ---------------------------------------------
#
# The account table holds every account's row - most of the page. Searching
# the whole page for the week popup, its title and its Close button walked
# all of it several times per account (~10s each). Here the table is found
# once, its rows give the "View week schedule" buttons directly, and every
# other search skips the table.

def _runtime_id(node):
    try:
        return tuple(node.GetRuntimeId())
    except Exception:
        return None


def _dfs_skip(node, skip, max_depth=40, depth=0):
    yield node
    if depth >= max_depth:
        return
    try:
        children = node.GetChildren()
    except Exception:
        return
    for c in children:
        try:
            if c.ControlTypeName == "TableControl" and _runtime_id(c) in skip:
                continue
        except Exception:
            continue
        yield from _dfs_skip(c, skip, max_depth, depth + 1)


def _find_skip(doc, skip, pred):
    for n in _dfs_skip(doc, skip):
        try:
            if pred(n):
                return n
        except Exception:
            continue
    return None


def _account_table(doc):
    return find(doc, lambda n: n.ControlTypeName == "TableControl"
                and find(n, lambda c: WEEK_BUTTON in name_of(c), max_depth=4) is not None)


def _rows(table):
    """[(account dict, View week button or None)] in page order."""
    out, section = [], ""
    for row in table.GetChildren():
        cells = row.GetChildren()
        if not cells:
            continue
        names = [name_of(c) for c in cells]
        account_cell = next((c for c, n in zip(cells, names) if WEEK_BUTTON in n), None)
        if account_cell is not None:
            out.append(({"name": _cell_account_name(account_cell), "section": section,
                         "number": names[0]}, account_cell))
        elif len(cells) == 2 and not names[0] and names[1]:
            section = _section_name(names[1])
    return out


def read_accounts_and_weeks(doc, names=None, numbers=None, log=print):
    """Account list + the weeks of the accounts named, or numbered
    first..last. Returns (accounts, picked, weeks, errors)."""
    table = _account_table(doc)
    if table is None:
        return [], [], {}, {"*": "the account table is not on the page"}
    rows = _rows(table)
    accounts = [a for a, _ in rows]

    def num(a):
        try:
            return int(str(a.get("number", "")).strip().rstrip("."))
        except ValueError:
            return None

    if numbers:
        first, last = sorted(numbers)
        picked = [a["name"] for a in accounts if num(a) is not None and first <= num(a) <= last]
    else:
        picked = list(names or [])
    cells = {a["name"]: c for a, c in rows}
    skip = {_runtime_id(table)}
    weeks, errors = {}, {}
    for name in picked:
        try:
            if name not in cells:
                raise LookupError(f'"{name}" is not on the scheduling page')
            weeks[name] = _read_one(doc, skip, cells[name], name)
            log(f"read {name}")
        except Exception as e:
            errors[name] = str(e)
            log(f"could not read {name}: {e}")
    return accounts, picked, weeks, errors


def _popup_parts(doc, skip):
    title = _find_skip(doc, skip, lambda n: n.ControlTypeName == "TextControl"
                       and name_of(n) == POPUP_TITLE)
    if title is None:
        return None
    box = title.GetParentControl()
    return title, box, box.GetParentControl()


def _close(doc, skip, parts):
    title, box, outer = parts
    close = find(box, lambda n: n.ControlTypeName == "ButtonControl" and name_of(n) == "Close", 3)
    if close is None:
        auto.SendKeys("{Esc}", waitTime=0.2)
    else:
        try:
            close.GetInvokePattern().Invoke()
        except Exception:
            close.Click(simulateMove=False)
    wait_for(lambda: _popup_parts(doc, skip) is None, 4, poll=0.1)


def _read_one(doc, skip, cell, account):
    open_parts = _popup_parts(doc, skip)
    if open_parts is not None:
        _close(doc, skip, open_parts)
    button = find(cell, lambda n: n.ControlTypeName == "ButtonControl"
                  and name_of(n) == WEEK_BUTTON, max_depth=6)
    try:
        button.GetInvokePattern().Invoke()
    except Exception:
        button.Click(simulateMove=False)

    def ready():
        parts = _popup_parts(doc, skip)
        if parts is None:
            return None
        title, box, outer = parts
        for t in find_all(outer, lambda n: n.ControlTypeName == "TableControl", 4):
            rows = t.GetChildren()
            if rows and sum(1 for c in rows[0].GetChildren() if _DAY_LABEL.match(name_of(c))) == 7:
                return parts, t
        return None

    got = wait_for(ready, 8, poll=0.1)
    if got is None:
        raise RuntimeError(f'the week schedule popup for "{account}" did not open')
    parts, table = got
    try:
        texts = [name_of(n) for n in parts[1].GetChildren() if n.ControlTypeName == "TextControl"]
        shown = texts[texts.index(POPUP_TITLE) + 1] if POPUP_TITLE in texts[:-1] else ""
        if shown and shown.rstrip("* ") != account.rstrip("* "):
            raise RuntimeError(f'opened the week for "{shown}" instead of "{account}"')
        rows = table.GetChildren()
        days = [name_of(c) for c in rows[0].GetChildren() if _DAY_LABEL.match(name_of(c))]
        slots = {}
        for row in rows[1:]:
            cells = row.GetChildren()
            if len(cells) >= 8:
                slots[name_of(cells[0])] = [name_of(c) for c in cells[1:8]]
        return {"days": days, "slots": slots}
    finally:
        _close(doc, skip, parts)
