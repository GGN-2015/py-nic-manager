from __future__ import annotations

import os
import platform


def is_admin() -> bool:
    try:
        from is_admin_user import is_admin_user

        return bool(is_admin_user())
    except Exception:
        return _fallback_is_admin()


def _fallback_is_admin() -> bool:
    if platform.system().lower() == "windows":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0

