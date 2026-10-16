"""The one thing that still needs Win32: staying on top of a fullscreen game.

The plugin achieved this with a generated PowerShell script that called
SetWindowPos on its own window. In-process it is just a few ctypes calls, and it
keeps the same guarantees: only when the foreground window really covers a whole
monitor, and always with SWP_NOACTIVATE so the game keeps focus and never gets
kicked out of fullscreen.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
MONITOR_DEFAULTTONEAREST = 2
ERROR_ALREADY_EXISTS = 183
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080

#:: mutex handles must outlive the call that created them
_LOCK_HANDLES: list[int] = []


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _user32():
    return ctypes.windll.user32


def foreground_is_fullscreen() -> bool:
    """True when the foreground window covers its whole monitor."""
    if not IS_WINDOWS:
        return False
    user32 = _user32()
    window = user32.GetForegroundWindow()
    if not window:
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(window, ctypes.byref(rect)):
        return False
    monitor = user32.MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return False
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(_MonitorInfo)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    screen = info.rcMonitor
    return (
        rect.left <= screen.left + 1
        and rect.top <= screen.top + 1
        and rect.right >= screen.right - 1
        and rect.bottom >= screen.bottom - 1
    )


def raise_above_fullscreen(hwnd: int) -> bool:
    """Re-assert topmost for ``hwnd`` without taking focus."""
    if not IS_WINDOWS or not hwnd:
        return False
    return bool(
        _user32().SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    )


def set_dpi_awareness() -> None:
    """Opt into per-monitor scaling before any window exists."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


def window_ex_style(hwnd: int) -> int:
    """Extended window style bits, so a test can assert what the OS really got."""
    if not IS_WINDOWS or not hwnd:
        return 0
    return int(ctypes.windll.user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE))


def is_non_activating(hwnd: int) -> bool:
    """True when clicking this window cannot steal the foreground from a game."""
    return bool(window_ex_style(hwnd) & WS_EX_NOACTIVATE)


def acquire_instance_lock(name: str) -> bool:
    """True when this process is the only one holding ``name``.

    A named kernel mutex is the dependable version of "only one instance": unlike
    a socket or a lock file it disappears with the process, so a crashed instance
    never blocks the next launch.
    """
    if not IS_WINDOWS:
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        return True  # cannot tell; better to run than to refuse to start
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _LOCK_HANDLES.append(handle)
    return True
