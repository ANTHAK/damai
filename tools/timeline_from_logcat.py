"""把 logcat 整理成抢票时间线（只读离线分析）。

用法：
    .\\.venv-phone\\Scripts\\python.exe tools\\timeline_from_logcat.py artifacts\\logcat_1130_full.txt 11:29:57 11:30:40
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TS = re.compile(r"^(\d\d-\d\d) (\d\d:\d\d:\d\d\.\d{3}) ")
TAP = re.compile(r"publisher action=0x0, .*channel '\w+ cn\.damai/([\w.$]+)'")
START = re.compile(r"START u0 .*cmp=cn\.damai/([\w.$]+)")
FINISH = re.compile(r"(?:wm_finish_activity|Finishing activity|finishActivity|removeChild.*ActivityRecord).*cn\.damai/([\w.$]+)")
RESUMED = re.compile(r"(?:setResumedActivity|wm_set_resumed_activity|topResumedActivity).*cn\.damai/([\w.$]+)")
TOAST_SHOW = re.compile(r"showSurfaceRobustly mWin:Window\{(\w+) u0 Toast\}")
DIALOG = re.compile(r"Window\{\w+ u0 cn\.damai/[\w.$]+\}.*(?:TYPE_APPLICATION\b|ty=APPLICATION\b)|addWindow.*cn\.damai.*Dialog", re.I)

SHORT = {
    "ProjectDetailActivity": "详情页",
    "NcovSkuActivity": "票档面板",
    "DmOrderActivity": "确认订单页",
}


def short(name: str) -> str:
    leaf = name.rsplit(".", 1)[-1]
    return SHORT.get(leaf, leaf)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    path = Path(sys.argv[1])
    start = sys.argv[2] if len(sys.argv) > 2 else "00:00:00"
    end = sys.argv[3] if len(sys.argv) > 3 else "99:99:99"
    last_tap_page = None
    tap_run = 0
    run_start = ""

    def flush_taps() -> None:
        nonlocal tap_run
        if tap_run:
            print(f"{run_start}  点击 x{tap_run:<3} -> {short(last_tap_page)}")
        tap_run = 0

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TS.match(raw.lstrip("\ufeff"))
        if not m:
            continue
        t = m.group(2)
        if not (start <= t[:8] <= end):
            continue
        if (tm := TAP.search(raw)) :
            page = tm.group(1)
            if page == last_tap_page and tap_run:
                tap_run += 1
            else:
                flush_taps()
                last_tap_page, tap_run, run_start = page, 1, t
            continue
        event = None
        if (sm := START.search(raw)):
            event = f"打开 {short(sm.group(1))}"
        elif (fm := FINISH.search(raw)):
            event = f"关闭 {short(fm.group(1))}"
        elif (rm := RESUMED.search(raw)):
            event = f"前台 = {short(rm.group(1))}"
        elif TOAST_SHOW.search(raw):
            event = "弹出 Toast 提示"
        if event:
            flush_taps()
            print(f"{t}  {event}")
    flush_taps()
    return 0


if __name__ == "__main__":
    sys.exit(main())
