from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable
from xml.etree import ElementTree as ET

import uiautomator2 as u2

from damai_android_tail import run_tail_probe_loop


# Device and safety configuration.
# REDMI K80（24117RK2CC）：屏幕 1440 x 3200，与旧设备 8595251f
# （1440 x 3200）完全一致，坐标无需缩放，直接沿用。
# 注意：K80 默认处于 FHD+（Override 1080x2400），必须切到 WQHD+（1440x3200）
# 预检才能通过，请先运行 tools\precheck_K80.py。
# 设备序列号：留空 = 自动选择唯一一台已授权的 adb 设备；
# 也可以用环境变量 DAMAI_DEVICE_SERIAL 指定（多台设备同时连接时必须指定）。
DEVICE_SERIAL = ""
EXPECTED_SCREEN_SIZE = (1440, 3200)
# 使用项目目录内的官方 platform-tools（damai-main\platform-tools\adb.exe）。
# 如果改成 None，脚本会回退为系统 PATH 里的 adb。
ADB_PATH = Path(__file__).resolve().parent.parent / "platform-tools" / "adb.exe"
DAMAI_PACKAGE = "cn.damai"
ALIPAY_PACKAGE = "com.eg.android.AlipayGphone"

# Calibrated/inferred tap points for 1440 x 3200 only.
# MAIN_ACTION_POINT is inside all three proven bottom-right actions:
# detail purchase entry, ticket-panel Confirm, and order-page Immediate Submit.
MAIN_ACTION_POINT = (1119, 3088)
# Inferred from the supplied photographed dialog. The primary retry button is
# [244,1891][1195,2041] in every Android retry-dialog capture under
# artifacts/8_14. Use the measured center, not the old photographed estimate.
# K80 修复后：点击「继续尝试」时不再使用这个固定坐标，而是用探测线程实时读取的
# 按钮 bounds 中心（见 RetryPointHolder）。12:20 实战证明库存不足弹窗的按钮
# [244,1975][1195,2125] 离此点仅 9px。此常量只保留给 tools\inspect_point_K80.py 参考。
RETRY_DIALOG_POINT = (720, 1966)

# All timing knobs are kept together for quick tuning.
PRECLICK_LEAD_S = 2.0
CLICK_INTERVAL_S = 0.150
FIRST_STATE_CHECK_AFTER_S = 0.0
STATE_CHECK_INTERVAL_S = 0.0
RISK_STATE_CHECK_INTERVAL_S = 0.25
MAX_RUNTIME_AFTER_SALE_S = 10 * 60.0
THREAD_JOIN_TIMEOUT_S = 3.0

# K80 修复（2026-10-14 11:30 实战）：第 2 次点「继续尝试」后弹窗没有消失，
# 原状态机对同一弹窗只点一次，导致空等 30 秒。现在弹窗持续存在超过
# RETRY_REPEAT_AFTER_S 秒就再点一次（只在探测到弹窗仍在时才点）。
RETRY_REPEAT_AFTER_S = 1.0
# 选座页「确认选座」按钮的重复点击间隔（按钮不消失就再点）。
SEAT_REPEAT_AFTER_S = 1.5
# 同一弹窗持续存在时，自动保存界面层级 + 截图到 artifacts\，最多保存几份。
DIALOG_SNAPSHOT_LIMIT = 3
# K80 修复（2026-10-14 13:00 实战）：确认页一直被判为 order_page_loading，
# 「立即提交」始终探测不到，空等 10 分钟且没有任何现场。现在这类非弹窗页面
# 停留超过 STUCK_PAGE_SNAPSHOT_AFTER_S 秒也会保存现场（只读，不点击）。
STUCK_PAGE_SNAPSHOT_AFTER_S = 3.0
STUCK_PAGE_SNAPSHOT_LIMIT = 3
STUCK_SNAPSHOT_PAGES = ("order_page_loading", "damai_other", "order_page", "seat_page")
# 现场里额外记录屏幕底部区域（按钮所在位置）的控件文字，便于看出「立即提交」变成了什么。
BOTTOM_AREA_TOP_RATIO = 0.80
# 页面无变化时，每隔多少秒输出一条 heartbeat 日志。
HEARTBEAT_INTERVAL_S = 1.0
ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"

# UI identifiers used only for in-memory state checks.
ORDER_TITLE_RESOURCE = "cn.damai:id/order_activity_title"
RETRY_RESOURCE = "cn.damai:id/damai_theme_dialog_confirm_btn"
BACK_REDIRECT_RESOURCE = "cn.damai:id/damai_theme_dialog_cancel_btn"
RISK_RESOURCE = "cn.damai:id/water_mark_view_id"
SUBMIT_TEXT = "立即提交"
# 选座票：进入选座页（TSeatActivity）后，要点底部「确认选座」才能进确认页。
# 2026-10-14 15:00 实战：选座票卡在这一页，盲点坐标 (1119,3088) 擦着按钮上边缘过不去。
SEAT_CONFIRM_RESOURCE = "cn.damai:id/seat_confirm_order"
SEAT_CONFIRM_TEXTS = ("确认选座", "确认选座并支付")
TIP_RESOURCE = "cn.damai:id/damai_theme_dialog_tip_content"
# K80 修复（2026-10-14 12:20 实战）：库存不足弹窗的「返回重新选购」与「继续尝试」
# 共用 damai_theme_dialog_confirm_btn，只看 resource-id 会把它误判成重试弹窗。
# 现在必须按钮文字精确等于「继续尝试」才算重试弹窗。
RETRY_TEXT = "继续尝试"
SOLD_OUT_BUTTON_KEYWORDS = ("返回", "重新选购")
SOLD_OUT_TIP_KEYWORDS = ("库存不足", "已售罄", "售罄")
# 连续几次探测到库存不足弹窗就停止（防止瞬时误判）。
SOLD_OUT_CONFIRM_COUNT = 2
# 「继续尝试」按钮中心点的有效期：超过这个时间没被探测重新确认就不点。
RETRY_POINT_MAX_AGE_S = 1.0

PAGE_RETRY_DIALOG = "retry_dialog"
PAGE_SEAT_PAGE = "seat_page"
PAGE_SOLD_OUT_DIALOG = "sold_out_dialog"
PAGE_UNKNOWN_DIALOG = "unknown_dialog"
PAGE_TERMINAL_DIALOG = "terminal_dialog"
PAGE_ORDER_PAGE = "order_page"
PAGE_ORDER_LOADING = "order_page_loading"
PAGE_RISK_VERIFICATION = "risk_verification"
PAGE_DAMAI_OTHER = "damai_other"


@dataclass(frozen=True)
class Mode0Schedule:
    sale_monotonic: float
    click_start_monotonic: float
    first_probe_monotonic: float
    deadline_monotonic: float


@dataclass
class RuntimeState:
    status: str = "waiting"
    last_package: str = ""
    last_page: str = "unknown"
    main_tap_count: int = 0
    retry_tap_count: int = 0
    probe_count: int = 0
    retry_dialog_hits: int = 0
    terminal_dialog_hits: int = 0
    risk_verification_hits: int = 0
    risk_back_count: int = 0
    risk_wait_seconds: float = 0.0
    state_transitions: list[dict[str, object]] = field(default_factory=list)
    foreground_check_errors: int = 0
    worker_error: str | None = None
    first_tap_wall_time: str | None = None
    risk_wait_started: float | None = None
    skipped_retry_count: int = 0
    seat_tap_count: int = 0
    skipped_seat_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update_package(self, package: str) -> None:
        with self.lock:
            self.last_package = package

    def record_tap(self, retry: bool) -> None:
        with self.lock:
            if self.first_tap_wall_time is None:
                self.first_tap_wall_time = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )
            if retry:
                self.retry_tap_count += 1
            else:
                self.main_tap_count += 1

    def record_risk_back(self) -> None:
        with self.lock:
            self.risk_back_count += 1

    def record_skipped_retry(self) -> None:
        with self.lock:
            self.skipped_retry_count += 1

    def record_seat_tap(self) -> None:
        with self.lock:
            if self.first_tap_wall_time is None:
                self.first_tap_wall_time = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )
            self.seat_tap_count += 1

    def record_skipped_seat(self) -> None:
        with self.lock:
            self.skipped_seat_count += 1

    def record_probe(self, page: str) -> None:
        with self.lock:
            self.probe_count += 1
            self.last_page = page

    def record_dialog(self, page: str) -> None:
        with self.lock:
            if page == PAGE_RETRY_DIALOG:
                self.retry_dialog_hits += 1
            elif page == PAGE_TERMINAL_DIALOG:
                self.terminal_dialog_hits += 1

    def transition(self, previous: str, current: str, page: str) -> None:
        if previous == current:
            return
        with self.lock:
            self.state_transitions.append(
                {
                    "from": previous,
                    "to": current,
                    "page": page,
                    "wall_time": datetime.now().astimezone().isoformat(
                        timespec="milliseconds"
                    ),
                }
            )

    def enter_risk_wait(self, now: float) -> None:
        with self.lock:
            if self.risk_wait_started is None:
                self.risk_verification_hits += 1
                self.risk_wait_started = now

    def leave_risk_wait(self, now: float) -> None:
        with self.lock:
            if self.risk_wait_started is not None:
                self.risk_wait_seconds += now - self.risk_wait_started
                self.risk_wait_started = None

    def record_foreground_error(self) -> None:
        with self.lock:
            self.foreground_check_errors += 1

    def finish(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.status = status
            if error is not None:
                self.worker_error = error

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            risk_wait_seconds = self.risk_wait_seconds
            if self.risk_wait_started is not None:
                risk_wait_seconds += time.perf_counter() - self.risk_wait_started
            return {
                "status": self.status,
                "last_package": self.last_package,
                "last_page": self.last_page,
                "main_tap_count": self.main_tap_count,
                "retry_tap_count": self.retry_tap_count,
                "probe_count": self.probe_count,
                "retry_dialog_hits": self.retry_dialog_hits,
                "terminal_dialog_hits": self.terminal_dialog_hits,
                "risk_verification_hits": self.risk_verification_hits,
                "risk_back_count": self.risk_back_count,
                "risk_wait_seconds": round(risk_wait_seconds, 3),
                "state_transitions": list(self.state_transitions),
                "foreground_check_errors": self.foreground_check_errors,
                "foreground_query_errors": self.foreground_check_errors,
                "worker_error": self.worker_error,
                "first_tap_wall_time": self.first_tap_wall_time,
            }


@dataclass(frozen=True)
class StateDecision:
    previous_phase: str
    phase: str
    action: str | None = None
    status: str | None = None
    clear_pending: bool = False
    dialog_hit: str | None = None
    entered_risk: bool = False
    left_risk: bool = False


class Mode0StateController:
    """Pure state machine for the post-submit retry loop."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self.phase = "blind"
        self.terminal_streak = 0
        self.clock = clock
        self.last_retry_at: float | None = None
        self.retry_repeat_count = 0
        self.last_seat_at: float | None = None
        self.seat_click_count = 0
        self.sold_out_streak = 0
        self.unknown_dialog_hits = 0

    @property
    def controlled(self) -> bool:
        return self.phase != "blind"

    def observe(self, page: str) -> StateDecision:
        previous = self.phase
        entered_risk = False
        left_risk = False

        if page == PAGE_RISK_VERIFICATION:
            action = None
            if self.phase != "risk_back_requested":
                self.phase = "risk_back_requested"
                entered_risk = True
                action = "back"
            self.terminal_streak = 0
            return StateDecision(
                previous,
                self.phase,
                action=action,
                clear_pending=True,
                entered_risk=entered_risk,
            )

        # 选座页：点「确认选座」进入确认页。座位由大麦已分配/已锁定，脚本不挑座位。
        # 每 SEAT_REPEAT_AFTER_S 秒最多点一次，按钮由探测线程实时定位。
        if page == PAGE_SEAT_PAGE:
            self.sold_out_streak = 0
            self.terminal_streak = 0
            now = self.clock()
            if (
                self.last_seat_at is None
                or now - self.last_seat_at >= SEAT_REPEAT_AFTER_S
            ):
                self.last_seat_at = now
                self.seat_click_count += 1
                return StateDecision(previous, self.phase, action="seat")
            return StateDecision(previous, self.phase)

        # 库存不足弹窗：绝不点击，连续确认 SOLD_OUT_CONFIRM_COUNT 次后停止。
        if page == PAGE_SOLD_OUT_DIALOG:
            self.sold_out_streak += 1
            self.terminal_streak = 0
            status = (
                "terminal_no_retry_option"
                if self.sold_out_streak >= SOLD_OUT_CONFIRM_COUNT
                else None
            )
            return StateDecision(
                previous,
                self.phase,
                status=status,
                clear_pending=True,
                dialog_hit=PAGE_TERMINAL_DIALOG if self.sold_out_streak == 1 else None,
                left_risk=self.phase == "risk_back_requested",
            )
        self.sold_out_streak = 0

        # 文字无法识别的弹窗：绝不点击，只清空待点请求并等待。
        if page == PAGE_UNKNOWN_DIALOG:
            self.unknown_dialog_hits += 1
            return StateDecision(previous, self.phase, clear_pending=True)

        if self.phase == "risk_back_requested":
            if page not in (
                PAGE_RETRY_DIALOG,
                PAGE_TERMINAL_DIALOG,
                PAGE_ORDER_PAGE,
                PAGE_ORDER_LOADING,
            ):
                return StateDecision(previous, self.phase, clear_pending=True)
            left_risk = True
            self.phase = (
                "await_result"
                if page == PAGE_TERMINAL_DIALOG
                else "await_submit"
            )

        if page == PAGE_RETRY_DIALOG:
            self.terminal_streak = 0
            now = self.clock()
            if self.phase != "retry_clicked":
                self.phase = "retry_clicked"
                self.last_retry_at = now
                return StateDecision(
                    previous,
                    self.phase,
                    action="retry",
                    dialog_hit=PAGE_RETRY_DIALOG,
                    left_risk=left_risk,
                )
            # 已点过「继续尝试」但弹窗仍在：超过 RETRY_REPEAT_AFTER_S 再点一次。
            if (
                self.last_retry_at is None
                or now - self.last_retry_at >= RETRY_REPEAT_AFTER_S
            ):
                self.last_retry_at = now
                self.retry_repeat_count += 1
                return StateDecision(
                    previous,
                    self.phase,
                    action="retry",
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if page == PAGE_TERMINAL_DIALOG:
            if self.phase == "await_result":
                self.terminal_streak += 1
                status = (
                    "terminal_no_retry_option"
                    if self.terminal_streak >= 2
                    else None
                )
                return StateDecision(
                    previous,
                    self.phase,
                    status=status,
                    clear_pending=True,
                    dialog_hit=(
                        PAGE_TERMINAL_DIALOG if self.terminal_streak == 1 else None
                    ),
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        self.terminal_streak = 0
        if page == PAGE_ORDER_PAGE:
            if self.phase != "await_result":
                self.phase = "await_result"
                return StateDecision(
                    previous,
                    self.phase,
                    action="submit",
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if page == PAGE_ORDER_LOADING:
            if self.phase in ("blind", "retry_clicked"):
                self.phase = "await_submit"
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if self.phase == "retry_clicked":
            self.phase = "await_submit"
        return StateDecision(previous, self.phase, left_risk=left_risk)


class TapRequestQueue:
    """Single-slot request queue; the ADB worker is its only consumer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: str | None = None
        self.changed = threading.Event()

    def request(self, action: str) -> bool:
        if action not in ("main", "submit", "retry", "back", "seat"):
            raise ValueError(f"未知点击动作：{action}")
        with self._lock:
            if self._pending is not None:
                return False
            self._pending = action
            self.changed.set()
            return True

    def pop(self) -> str | None:
        with self._lock:
            action = self._pending
            self._pending = None
            self.changed.clear()
            return action

    def clear(self) -> None:
        with self._lock:
            self._pending = None
            self.changed.clear()


class PersistentTapShell:
    """Dispatch low-overhead ADB taps through one persistent device shell."""

    def __init__(self, adb_path: Path, serial: str) -> None:
        self.process = subprocess.Popen(
            [str(adb_path), "-s", serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None:
            raise RuntimeError("无法创建持久 ADB shell")

    def tap(self, point: tuple[int, int]) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB shell 已意外结束")
        assert self.process.stdin is not None
        self.process.stdin.write(f"input tap {point[0]} {point[1]}\n")
        self.process.stdin.flush()

    def back(self) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB shell 已意外结束")
        assert self.process.stdin is not None
        self.process.stdin.write("input keyevent 4\n")
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()


# Lightweight foreground query: parse only the mCurrentFocus / mFocusedApp
# lines instead of the full `dumpsys window windows` output that
# app_current() transfers back over adb.
FOCUS_LINE_RE = re.compile(r"mCurrentFocus=Window\{[^}]*\s+u\d+\s+([^\s/]+)/")
FOCUSED_APP_RE = re.compile(r"mFocusedApp=ActivityRecord\{[^}]*\s+u\d+\s+([^\s/]+)/")


def parse_foreground_package(line: str) -> str | None:
    """Extract the foreground package from one dumpsys window line."""
    match = FOCUS_LINE_RE.search(line)
    if match:
        return match.group(1)
    match = FOCUSED_APP_RE.search(line)
    if match:
        return match.group(1)
    return None


class PersistentQueryShell:
    """One persistent adb shell for lightweight foreground queries.

    Runs `dumpsys window | grep -E "mCurrentFocus|mFocusedApp"` on the device
    and reads back only the matched lines (terminated by a __EOT__ marker),
    avoiding the full dumpsys round trip of app_current(). Independent of the
    tap shell: taps are fire-and-forget, this shell owns its stdout stream.
    """

    def __init__(self, adb_path: Path, serial: str) -> None:
        self.process = subprocess.Popen(
            [str(adb_path), "-s", serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("无法创建持久 ADB 查询 shell")

    def query_package(self) -> str | None:
        """Return the current foreground package, or None when unparseable."""
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB 查询 shell 已意外结束")
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'; echo __EOT__\n"
        )
        self.process.stdin.flush()
        package: str | None = None
        while True:
            line = self.process.stdout.readline()
            if line.strip() == "__EOT__":
                return package
            parsed = parse_foreground_package(line)
            if parsed is not None and package is None:
                package = parsed

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()


def parse_sale_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "时间格式应为 YYYY-MM-DD HH:MM:SS"
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone Damai mode 0: repeatedly tap the shared bottom action "
            "and stop immediately when Alipay becomes foreground."
        )
    )
    parser.add_argument(
        "sale_time",
        type=parse_sale_time,
        help='Local start time, for example: "2026-08-18 13:00:00"',
    )
    return parser.parse_args()


def make_schedule(
    sale_time: datetime,
    now_wall: datetime,
    now_monotonic: float,
) -> Mode0Schedule:
    seconds_to_sale = (sale_time - now_wall).total_seconds()
    sale_monotonic = now_monotonic + seconds_to_sale
    return Mode0Schedule(
        sale_monotonic=sale_monotonic,
        click_start_monotonic=sale_monotonic - PRECLICK_LEAD_S,
        first_probe_monotonic=sale_monotonic + FIRST_STATE_CHECK_AFTER_S,
        deadline_monotonic=sale_monotonic + MAX_RUNTIME_AFTER_SALE_S,
    )


def classify_fast_state(
    package: str,
    order_page_visible: bool = False,
) -> str:
    if package == ALIPAY_PACKAGE:
        return "alipay"
    if package != DAMAI_PACKAGE:
        return "other_app"
    if order_page_visible:
        return "order_page"
    return "damai_other"


def classify_hierarchy_xml(xml_text: str) -> str:
    """Classify a saved Android hierarchy without talking to a device."""
    root = ET.fromstring(xml_text)
    nodes = list(root.iter("node"))

    def has_resource(resource_id: str, *, clickable: bool | None = None) -> bool:
        for node in nodes:
            if node.attrib.get("resource-id") != resource_id:
                continue
            if clickable is None or node.attrib.get("clickable") == str(clickable).lower():
                return True
        return False

    confirm = next(
        (
            node
            for node in nodes
            if node.attrib.get("resource-id") == RETRY_RESOURCE
            and node.attrib.get("clickable") == "true"
        ),
        None,
    )
    if confirm is not None:
        tip = next(
            (
                node.attrib.get("text", "")
                for node in nodes
                if node.attrib.get("resource-id") == TIP_RESOURCE
            ),
            "",
        )
        return classify_confirm_button(confirm.attrib.get("text", ""), tip)
    seat = next(
        (
            node
            for node in nodes
            if node.attrib.get("resource-id") == SEAT_CONFIRM_RESOURCE
            and node.attrib.get("text", "").strip() in SEAT_CONFIRM_TEXTS
        ),
        None,
    )
    if seat is not None:
        return PAGE_SEAT_PAGE
    if has_resource(BACK_REDIRECT_RESOURCE, clickable=True):
        return PAGE_TERMINAL_DIALOG
    if any(
        node.attrib.get("package") == DAMAI_PACKAGE
        and node.attrib.get("text") == SUBMIT_TEXT
        and node.attrib.get("clickable") == "true"
        for node in nodes
    ):
        return PAGE_ORDER_PAGE
    if has_resource(ORDER_TITLE_RESOURCE):
        return PAGE_ORDER_LOADING
    if has_resource(RISK_RESOURCE):
        return PAGE_RISK_VERIFICATION
    return PAGE_DAMAI_OTHER


class RetryPointHolder:
    """探测线程写入「继续尝试」按钮的实测中心点，点击线程只读取新鲜的点。"""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._lock = threading.Lock()
        self._point: tuple[int, int] | None = None
        self._at = 0.0
        self.clock = clock

    def set(self, point: tuple[int, int]) -> None:
        with self._lock:
            self._point = point
            self._at = self.clock()

    def clear(self) -> None:
        with self._lock:
            self._point = None

    def fresh(self, max_age_s: float = RETRY_POINT_MAX_AGE_S) -> tuple[int, int] | None:
        with self._lock:
            if self._point is None or self.clock() - self._at > max_age_s:
                return None
            return self._point


def classify_confirm_button(text: str, tip: str) -> str:
    """按按钮文字 / 提示文字区分重试弹窗与库存不足弹窗。"""
    text = text.strip()
    if text == RETRY_TEXT:
        return PAGE_RETRY_DIALOG
    if any(k in text for k in SOLD_OUT_BUTTON_KEYWORDS) or any(
        k in tip for k in SOLD_OUT_TIP_KEYWORDS
    ):
        return PAGE_SOLD_OUT_DIALOG
    return PAGE_UNKNOWN_DIALOG


def bounds_center(bounds: dict | None) -> tuple[int, int] | None:
    if not bounds:
        return None
    try:
        return (
            (int(bounds["left"]) + int(bounds["right"])) // 2,
            (int(bounds["top"]) + int(bounds["bottom"])) // 2,
        )
    except (KeyError, TypeError, ValueError):
        return None


class FastPageProbe:
    """Probe verified selectors only; never dump or save the UI tree."""

    def __init__(self, device: object, retry_points: RetryPointHolder | None = None) -> None:
        self.device = device
        self.retry_points = retry_points or RetryPointHolder()
        self.seat_points = RetryPointHolder()
        self.last_dialog_text = ""
        self.order_title = device(resourceId=ORDER_TITLE_RESOURCE)
        self.tip = device(resourceId=TIP_RESOURCE)
        self.retry = device(resourceId=RETRY_RESOURCE, clickable=True)
        self.back_redirect = device(
            resourceId=BACK_REDIRECT_RESOURCE,
            clickable=True,
        )
        self.submit = device(
            packageName=DAMAI_PACKAGE,
            text=SUBMIT_TEXT,
            clickable=True,
        )
        self.risk = device(resourceId=RISK_RESOURCE)
        self.seat_confirm = device(
            resourceId=SEAT_CONFIRM_RESOURCE,
            clickable=True,
            textContains="确认选座",
        )

    def read(self, phase: str) -> str:
        submit_checked = False
        if phase in ("retry_clicked", "await_submit"):
            submit_checked = True
            if self.submit.exists(timeout=0):
                self.retry_points.clear()
                self.seat_points.clear()
                return PAGE_ORDER_PAGE
        if self.retry.exists(timeout=0):
            dialog = self._classify_dialog()
            if dialog is not None:
                self.seat_points.clear()
                return dialog
        self.retry_points.clear()
        # 选座页：识别「确认选座」按钮，记录它的实测位置供点击线程使用。
        if self.seat_confirm.exists(timeout=0):
            seat = self._classify_seat()
            if seat is not None:
                return seat
        self.seat_points.clear()
        if phase in ("await_result", "risk_back_requested") and self.back_redirect.exists(
            timeout=0
        ):
            return PAGE_TERMINAL_DIALOG
        if not submit_checked and self.submit.exists(timeout=0):
            return PAGE_ORDER_PAGE
        if phase in ("await_result", "risk_back_requested") and self.risk.exists(timeout=0):
            return PAGE_RISK_VERIFICATION
        if self.order_title.exists(timeout=0):
            return PAGE_ORDER_LOADING
        return PAGE_DAMAI_OTHER

    def _classify_dialog(self) -> str | None:
        """读取 confirm_btn 的文字和位置；按钮在读取前消失时返回 None。"""
        try:
            info = self.retry.info
        except Exception:
            return None
        text = str(info.get("text") or "")
        tip = ""
        try:
            if self.tip.exists(timeout=0):
                tip = str(self.tip.info.get("text") or "")
        except Exception:
            tip = ""
        self.last_dialog_text = f"{tip} | {text}".strip(" |")
        dialog = classify_confirm_button(text, tip)
        if dialog == PAGE_RETRY_DIALOG:
            center = bounds_center(info.get("visibleBounds") or info.get("bounds"))
            if center is None:
                self.retry_points.clear()
                return PAGE_UNKNOWN_DIALOG
            self.retry_points.set(center)
        else:
            self.retry_points.clear()
        return dialog

    def _classify_seat(self) -> str | None:
        """识别选座页的「确认选座」按钮，记录实测中心点。按钮消失时返回 None。"""
        try:
            info = self.seat_confirm.info
        except Exception:
            return None
        text = str(info.get("text") or "").strip()
        if text not in SEAT_CONFIRM_TEXTS:
            self.seat_points.clear()
            return PAGE_SEAT_PAGE
        center = bounds_center(info.get("visibleBounds") or info.get("bounds"))
        if center is None:
            self.seat_points.clear()
            return PAGE_SEAT_PAGE
        self.seat_points.set(center)
        return PAGE_SEAT_PAGE


def summarize_bottom_controls(
    xml_text: str,
    screen_height: int = EXPECTED_SCREEN_SIZE[1],
    limit: int = 12,
) -> list[dict[str, object]]:
    """列出屏幕底部区域带文字的控件（文字、resource-id、clickable、enabled、bounds）。"""
    root = ET.fromstring(xml_text)
    top_limit = screen_height * BOTTOM_AREA_TOP_RATIO
    found: list[dict[str, object]] = []
    for node in root.iter("node"):
        text = (node.attrib.get("text") or node.attrib.get("content-desc") or "").strip()
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
        if not text or not match or int(match.group(2)) < top_limit:
            continue
        found.append(
            {
                "text": text[:40],
                "id": node.attrib.get("resource-id", ""),
                "clickable": node.attrib.get("clickable") == "true",
                "enabled": node.attrib.get("enabled") == "true",
                "bounds": node.attrib.get("bounds"),
            }
        )
        if len(found) >= limit:
            break
    return found


class DiagnosticProbe:
    """K80 专用：包装 FastPageProbe，增加 heartbeat 日志和弹窗卡住时的现场快照。

    不改变探测结果，也不发出任何点击；只读 + 写 artifacts\\ 文件。
    快照在后台线程里做，不阻塞探测/重试闭环。
    """

    def __init__(self, inner: FastPageProbe, device: object, sale_monotonic: float) -> None:
        self.inner = inner
        self.device = device
        self.sale_monotonic = sale_monotonic
        self.last_page: str | None = None
        self.page_since = 0.0
        self.last_heartbeat = 0.0
        self.snapshots: list[str] = []
        self.stuck_page_snapshots = 0
        self.dialog_snapshots = 0
        self._snapshot_thread: threading.Thread | None = None
        self._snapshot_due_at: float | None = None
        self._lock = threading.Lock()

    def read(self, phase: str) -> str:
        page = self.inner.read(phase)
        now = time.perf_counter()
        if page != self.last_page:
            self.last_page = page
            self.page_since = now
            self.last_heartbeat = now
            if page in (PAGE_SOLD_OUT_DIALOG, PAGE_UNKNOWN_DIALOG):
                self._emit(
                    {
                        "event": "dialog_text",
                        "page": page,
                        "text": getattr(self.inner, "last_dialog_text", ""),
                    }
                )
                self._start_snapshot(page, 0.0)  # 新类型弹窗立即留证
                self._snapshot_due_at = None
            elif page == PAGE_RETRY_DIALOG:
                self._snapshot_due_at = now + RETRY_REPEAT_AFTER_S
            elif page in STUCK_SNAPSHOT_PAGES and now >= self.sale_monotonic:
                self._snapshot_due_at = now + STUCK_PAGE_SNAPSHOT_AFTER_S
            else:
                self._snapshot_due_at = None
            return page

        stuck_for = now - self.page_since
        if now - self.last_heartbeat >= HEARTBEAT_INTERVAL_S:
            self.last_heartbeat = now
            self._emit(
                {
                    "event": "heartbeat",
                    "page": page,
                    "phase": phase,
                    "same_page_seconds": round(stuck_for, 3),
                }
            )
        # 开售前就停在详情页属于正常等待，开售后才开始计时。
        if (
            self._snapshot_due_at is None
            and page in STUCK_SNAPSHOT_PAGES
            and now >= self.sale_monotonic + STUCK_PAGE_SNAPSHOT_AFTER_S
            and now - max(self.page_since, self.sale_monotonic) >= STUCK_PAGE_SNAPSHOT_AFTER_S
            and self.stuck_page_snapshots < STUCK_PAGE_SNAPSHOT_LIMIT
        ):
            self._snapshot_due_at = now
        if self._snapshot_due_at is not None and now >= self._snapshot_due_at:
            # 同一页面持续卡住时，每 2 秒（弹窗）/ 5 秒（非弹窗）最多再存一份
            self._snapshot_due_at = now + (2.0 if page == PAGE_RETRY_DIALOG else 5.0)
            self._start_snapshot(page, stuck_for)
        return page

    def _emit(self, record: dict[str, object]) -> None:
        record["wall_time"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        record["seconds_after_sale"] = round(time.perf_counter() - self.sale_monotonic, 3)
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def _start_snapshot(self, page: str, stuck_for: float) -> None:
        with self._lock:
            # 弹窗现场与非弹窗卡页现场分别计数，互不挤占名额。
            is_stuck_page = page in STUCK_SNAPSHOT_PAGES
            if is_stuck_page:
                if self.stuck_page_snapshots >= STUCK_PAGE_SNAPSHOT_LIMIT:
                    return
            elif self.dialog_snapshots >= DIALOG_SNAPSHOT_LIMIT:
                return
            if self._snapshot_thread is not None and self._snapshot_thread.is_alive():
                return
            if is_stuck_page:
                self.stuck_page_snapshots += 1
            else:
                self.dialog_snapshots += 1
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            base = ARTIFACTS_DIR / f"stuck_{page}_{stamp}"
            self.snapshots.append(str(base))
            self._snapshot_thread = threading.Thread(
                target=self._snapshot,
                args=(base, page, stuck_for),
                name="damai-k80-snapshot",
                daemon=True,
            )
            self._snapshot_thread.start()

    def _snapshot(self, base: Path, page: str, stuck_for: float) -> None:
        saved: list[str] = []
        error = None
        bottom: list[dict[str, object]] = []
        try:
            ARTIFACTS_DIR.mkdir(exist_ok=True)
            xml_text = self.device.dump_hierarchy()
            xml_path = base.with_suffix(".xml")
            xml_path.write_text(xml_text, encoding="utf-8")
            saved.append(xml_path.name)
            bottom = summarize_bottom_controls(xml_text)
            png_path = base.with_suffix(".png")
            self.device.screenshot(str(png_path))
            saved.append(png_path.name)
        except Exception as exc:  # 快照失败不影响抢票
            error = f"{type(exc).__name__}: {exc}"
        self._emit(
            {
                "event": "dialog_snapshot" if page not in STUCK_SNAPSHOT_PAGES else "stuck_page_snapshot",
                "page": page,
                "same_page_seconds": round(stuck_for, 3),
                "files": saved,
                "bottom_controls": bottom,
                "error": error,
            }
        )

    def join(self, timeout: float) -> None:
        thread = self._snapshot_thread
        if thread is not None:
            thread.join(timeout=timeout)


def wait_until(target: float, stop_event: threading.Event) -> bool:
    while not stop_event.is_set():
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return True
        stop_event.wait(min(remaining, 0.1))
    return False


def tap_loop(
    schedule: Mode0Schedule,
    state: RuntimeState,
    stop_event: threading.Event,
    controlled_event: threading.Event,
    requests: TapRequestQueue,
    tap_shell: PersistentTapShell,
    query_shell: PersistentQueryShell,
    retry_points: RetryPointHolder | None = None,
    seat_points: RetryPointHolder | None = None,
) -> None:
    try:
        if not wait_until(schedule.click_start_monotonic, stop_event):
            return

        next_blind_tap = max(schedule.click_start_monotonic, time.perf_counter())
        state.finish("clicking")
        while not stop_event.is_set():
            now = time.perf_counter()
            if now >= schedule.deadline_monotonic:
                state.finish("timeout")
                stop_event.set()
                return

            try:
                package = query_shell.query_package()
            except Exception:
                # A failed foreground check means no coordinate tap is safe.
                state.record_foreground_error()
                stop_event.wait(0.01)
                continue
            if package is None:
                # Unparseable foreground means unknown: still no tap.
                state.record_foreground_error()
                stop_event.wait(0.01)
                continue

            state.update_package(package)
            if package == ALIPAY_PACKAGE:
                requests.clear()
                state.finish("alipay_reached")
                stop_event.set()
                return
            if package != DAMAI_PACKAGE:
                stop_event.wait(0.01)
                continue

            action = requests.pop()
            now = time.perf_counter()
            if action is None and not controlled_event.is_set() and now >= next_blind_tap:
                action = "main"
                next_blind_tap += CLICK_INTERVAL_S
                if next_blind_tap <= now:
                    next_blind_tap = now + CLICK_INTERVAL_S

            if action is not None:
                if action == "back":
                    tap_shell.back()
                    state.record_risk_back()
                    continue
                retry = action == "retry"
                if action == "seat":
                    # 只点探测线程刚实测到的「确认选座」按钮中心；拿不到就不点。
                    point = seat_points.fresh() if seat_points is not None else None
                    if point is None:
                        state.record_skipped_seat()
                        continue
                    tap_shell.tap(point)
                    state.record_seat_tap()
                    continue
                if retry:
                    # 只点探测线程刚实测到的「继续尝试」按钮中心；拿不到就不点。
                    point = retry_points.fresh() if retry_points is not None else None
                    if point is None:
                        state.record_skipped_retry()
                        continue
                else:
                    point = MAIN_ACTION_POINT
                tap_shell.tap(point)
                state.record_tap(retry=retry)
                continue

            if controlled_event.is_set():
                requests.changed.wait(0.01)
            else:
                stop_event.wait(min(max(next_blind_tap - now, 0.0), 0.01))
    except BaseException as exc:
        state.finish("error", f"{type(exc).__name__}: {exc}")
        stop_event.set()


def query_foreground_once(adb_path: Path) -> str | None:
    """One-shot foreground query using the same command as PersistentQueryShell.

    K80 (HyperOS) does not print mCurrentFocus in `dumpsys window windows`, so
    uiautomator2's app_current() falls back to `dumpsys activity top` and
    reports the *last* task (e.g. QQ Mail) instead of the real foreground app.
    `dumpsys window` does contain mCurrentFocus, and it is exactly what the
    runtime tap gate uses, so the preflight now checks the same source.
    """
    proc = subprocess.run(
        [
            str(adb_path),
            "-s",
            DEVICE_SERIAL,
            "shell",
            "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    for line in proc.stdout.splitlines():
        package = parse_foreground_package(line)
        if package is not None:
            return package
    return None


def validate_preflight(device: object, adb_path: Path) -> None:
    actual_size = tuple(device.window_size())
    if actual_size != EXPECTED_SCREEN_SIZE:
        raise RuntimeError(
            f"手机分辨率为 {actual_size[0]}×{actual_size[1]}，"
            f"但模式 0 坐标按 {EXPECTED_SCREEN_SIZE[0]}×{EXPECTED_SCREEN_SIZE[1]} 校准。"
            "K80 请在 设置 → 显示与亮度 → 屏幕分辨率 中选择 WQHD+"
        )
    current_package = query_foreground_once(adb_path)
    if current_package != DAMAI_PACKAGE:
        raise RuntimeError(
            f"当前前台为 {current_package or '未知'}，请先将大麦置于前台并停在目标演唱会详情页"
        )


def resolve_adb() -> Path:
    """Resolve the adb executable: explicit ADB_PATH first, then PATH lookup."""
    if ADB_PATH is not None:
        if not ADB_PATH.is_file():
            raise FileNotFoundError(f"ADB 不存在：{ADB_PATH}")
        return ADB_PATH
    found = shutil.which("adb")
    if not found:
        raise FileNotFoundError(
            "ADB_PATH 为 None 且系统 PATH 中找不到 adb，"
            "请设置 ADB_PATH 为本机 adb.exe 的绝对路径"
        )
    return Path(found)


def resolve_serial(adb_path: Path) -> str:
    """DEVICE_SERIAL 常量 > 环境变量 DAMAI_DEVICE_SERIAL > 唯一一台已授权设备。"""
    import os

    explicit = DEVICE_SERIAL or os.environ.get("DAMAI_DEVICE_SERIAL", "").strip()
    if explicit:
        return explicit
    proc = subprocess.run(
        [str(adb_path), "devices"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    ready = [
        parts[0]
        for parts in (line.split() for line in proc.stdout.splitlines()[1:])
        if len(parts) >= 2 and parts[1] == "device"
    ]
    if len(ready) != 1:
        raise RuntimeError(
            f"检测到 {len(ready)} 台已授权的 adb 设备 {ready}；"
            "请只连接一台，或设置环境变量 DAMAI_DEVICE_SERIAL"
        )
    return ready[0]


def run(sale_time: datetime) -> dict[str, object]:
    global DEVICE_SERIAL
    adb_path = resolve_adb()
    DEVICE_SERIAL = resolve_serial(adb_path)

    probe_device = u2.connect(DEVICE_SERIAL)
    validate_preflight(probe_device, adb_path)
    schedule = make_schedule(sale_time, datetime.now(), time.perf_counter())
    retry_points = RetryPointHolder()
    page_probe = DiagnosticProbe(
        FastPageProbe(probe_device, retry_points),
        probe_device,
        schedule.sale_monotonic,
    )
    if time.perf_counter() >= schedule.deadline_monotonic:
        return {
            "status": "timeout_before_start",
            "sale_time": sale_time.isoformat(sep=" "),
            "safety": "no_tap_dispatched",
        }

    state = RuntimeState()
    stop_event = threading.Event()
    controlled_event = threading.Event()
    requests = TapRequestQueue()
    controller = Mode0StateController()
    tap_shell = PersistentTapShell(adb_path, DEVICE_SERIAL)
    query_shell = PersistentQueryShell(adb_path, DEVICE_SERIAL)
    worker = threading.Thread(
        target=tap_loop,
        args=(
            schedule,
            state,
            stop_event,
            controlled_event,
            requests,
            tap_shell,
            query_shell,
            retry_points,
            page_probe.inner.seat_points,
        ),
        name="damai-mode0-k80-tap-loop",
        daemon=True,
    )
    worker.start()

    try:
        run_tail_probe_loop(
            schedule=schedule,
            state=state,
            stop_event=stop_event,
            controlled_event=controlled_event,
            requests=requests,
            controller=controller,
            page_probe=page_probe,
            wait_until=wait_until,
            state_check_interval_s=STATE_CHECK_INTERVAL_S,
            risk_state_check_interval_s=RISK_STATE_CHECK_INTERVAL_S,
        )
    finally:
        requests.clear()
        state.leave_risk_wait(time.perf_counter())
        stop_event.set()
        worker.join(timeout=THREAD_JOIN_TIMEOUT_S)
        page_probe.join(timeout=THREAD_JOIN_TIMEOUT_S)
        tap_shell.close()
        query_shell.close()

    result = state.snapshot()
    result.update(
        {
            "retry_repeat_count": controller.retry_repeat_count,
            "seat_click_count": controller.seat_click_count,
            "seat_tap_count": state.seat_tap_count,
            "skipped_seat_count": state.skipped_seat_count,
            "skipped_retry_count": state.skipped_retry_count,
            "sold_out_dialog_seen": controller.sold_out_streak > 0
            or result.get("last_page") == PAGE_SOLD_OUT_DIALOG,
            "unknown_dialog_hits": controller.unknown_dialog_hits,
            "dialog_snapshots": page_probe.snapshots,
            "sale_time": sale_time.isoformat(sep=" "),
            "finished_at": datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            ),
            "seconds_after_sale": round(
                time.perf_counter() - schedule.sale_monotonic,
                3,
            ),
            "main_action_point": list(MAIN_ACTION_POINT),
            "retry_dialog_point": "measured_from_retry_button_bounds",
            "safety": (
                "stops_on_alipay_foreground_or_terminal_or_sold_out_dialog; "
                "clicks_seat_confirm_when_on_seat_page; "
                "clicks_retry_only_when_button_text_is_继续尝试; "
                "never_clicks_unknown_dialog; "
                "backs_once_from_risk_verification; "
                "never_clicks_alipay_pay; never_clicks_back_redirect"
            ),
        }
    )
    return result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args()
    result = run(args.sale_time)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
