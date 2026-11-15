"""UI Automation helpers shared by the website reader and the Infloww driver."""
import time
import ctypes
import uiautomation as auto

WM_GETOBJECT = 0x003D
OBJID_CLIENT = 0xFFFFFFFC
SMTO_ABORTIFHUNG = 0x0002


def dfs(control, max_depth=70, depth=0):
    yield control
    if depth >= max_depth:
        return
    try:
        children = control.GetChildren()
    except Exception:
        children = []
    for c in children:
        yield from dfs(c, max_depth, depth + 1)


def find(control, pred, max_depth=70):
    for node in dfs(control, max_depth):
        try:
            if pred(node):
                return node
        except Exception:
            continue
    return None


def find_all(control, pred, max_depth=70):
    out = []
    for node in dfs(control, max_depth):
        try:
            if pred(node):
                out.append(node)
        except Exception:
            continue
    return out


def name_of(node):
    try:
        return (node.Name or "").strip()
    except Exception:
        return ""


def rect(node):
    r = node.BoundingRectangle
    return r.left, r.top, r.right, r.bottom


def visible(node):
    try:
        l, t, r, b = rect(node)
        return r > l and b > t and not node.IsOffscreen
    except Exception:
        return False


def center(node):
    l, t, r, b = rect(node)
    return (l + r) // 2, (t + b) // 2


def wake_accessibility(hwnd):
    """Chromium drops its accessibility tree when nobody asks for it, and
    for background windows; WM_GETOBJECT is what a screen reader sends to
    bring it back (see UPSELL SCRIPT INFLOWW's core for the long story)."""
    if not hwnd:
        return
    try:
        result = ctypes.c_void_p()
        ctypes.windll.user32.SendMessageTimeoutW(
            ctypes.c_void_p(hwnd), WM_GETOBJECT, 0, OBJID_CLIENT,
            SMTO_ABORTIFHUNG, 2000, ctypes.byref(result))
    except Exception:
        pass


def bring_to_front(hwnd):
    """Make `hwnd` the foreground window, for real. Windows refuses
    SetForegroundWindow to a process that did not just get user input, and
    then the program's clicks land on whatever window IS on top (seen
    2026-11-12: Infloww's clicks went to Chrome). Tapping Alt first is the
    standard way to be allowed; checked afterwards."""
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    for _ in range(3):
        if user32.GetForegroundWindow() == hwnd:
            return True
        if user32.IsIconic(ctypes.c_void_p(hwnd)):
            user32.ShowWindow(ctypes.c_void_p(hwnd), 9)          # SW_RESTORE
        user32.keybd_event(0x12, 0, 0, 0)                          # Alt down
        user32.keybd_event(0x12, 0, 2, 0)                          # Alt up
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        time.sleep(0.15)
    return user32.GetForegroundWindow() == hwnd


def activate(window, delay=0.6):
    try:
        window.SetActive()
    except Exception:
        pass
    bring_to_front(window.NativeWindowHandle)
    wake_accessibility(window.NativeWindowHandle)
    time.sleep(delay)


def wait_for(fn, timeout=6.0, poll=0.25):
    """Poll fn() until it returns something truthy; returns that or None."""
    deadline = time.time() + timeout
    while True:
        try:
            value = fn()
        except Exception:
            value = None
        if value:
            return value
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def click(node_or_point, wait=0.5):
    x, y = node_or_point if isinstance(node_or_point, tuple) else center(node_or_point)
    auto.MoveTo(x, y, moveSpeed=1, waitTime=0.05)
    auto.Click(x, y, waitTime=wait)


def hover(x, y, settle=1.0):
    """Hover with real movement: Infloww's shift popups only open on
    mouse-move events, not on the cursor jumping there."""
    auto.MoveTo(x, y - 40, moveSpeed=1, waitTime=0.05)
    auto.MoveTo(x, y, moveSpeed=1, waitTime=settle)


def type_text(text, wait=0.4):
    escaped = "".join("{{}" if ch == "{" else "{}}" if ch == "}" else ch for ch in text)
    auto.SendKeys(escaped, interval=0.02, waitTime=wait)


def select_all_and_type(text, wait=0.4):
    auto.SendKeys("{Ctrl}a", waitTime=0.1)
    auto.SendKeys("{Delete}", waitTime=0.1)
    type_text(text, wait)


def pixel(x, y):
    """(r, g, b) of one screen pixel - used only to tell a ticked weekday
    button (solid blue) from an unticked one (outlined); nothing is read
    as text this way."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    # Handles are pointer-sized; the default int conversion overflows them.
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.GetPixel.restype = ctypes.c_uint32
    dc = user32.GetDC(None)
    try:
        value = gdi32.GetPixel(dc, x, y)
    finally:
        user32.ReleaseDC(None, dc)
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
