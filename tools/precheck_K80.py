"""REDMI K80 一键预检（只读，不点击、不修改手机设置）。

用法（项目根目录）：
    .\\.venv-phone\\Scripts\\python.exe tools\\precheck_K80.py
    .\\.venv-phone\\Scripts\\python.exe tools\\precheck_K80.py --skip-time

设备常量直接从 scripts/damai_mode0_K80.py 用 AST 读取，保证与抢票脚本一致。
退出码：0 = 无 FAIL；1 = 存在 FAIL（不要启动抢票脚本）。
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
TARGET_SCRIPT = ROOT / "scripts" / "damai_mode0_K80.py"
DEFAULT_ADB = ROOT / "platform-tools" / "adb.exe"
EXPECTED_MODEL = "24117RK2CC"
TAOBAO_TIME_URL = "https://api.m.taobao.com/rest/api3.do?api=mtop.common.getTimestamp"
NTP_SERVER = "ntp.aliyun.com"
NTP_EPOCH_DELTA = 2208988800  # 1900-01-01 -> 1970-01-01
CLOCK_WARN_MS = 200.0

results: list[tuple[str, str, str]] = []


def report(level: str, item: str, detail: str) -> None:
    results.append((level, item, detail))
    print(f"[{level:<4}] {item}: {detail}", flush=True)


def load_constants() -> dict[str, object]:
    """Read literal constants from the K80 script without importing it."""
    wanted = {
        "DEVICE_SERIAL",
        "EXPECTED_SCREEN_SIZE",
        "MAIN_ACTION_POINT",
        "RETRY_DIALOG_POINT",
        "DAMAI_PACKAGE",
        "ALIPAY_PACKAGE",
    }
    tree = ast.parse(TARGET_SCRIPT.read_text(encoding="utf-8"))
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                found[target.id] = ast.literal_eval(node.value)
    missing = wanted - found.keys()
    if missing:
        raise RuntimeError(f"{TARGET_SCRIPT.name} 缺少常量：{sorted(missing)}")
    return found


def resolve_serial(adb_path: Path, configured: str = "") -> str:
    """与抢票脚本相同：常量 > 环境变量 DAMAI_DEVICE_SERIAL > 唯一一台已授权设备。

    找不到唯一设备时返回空字符串，由 check_device 给出 FAIL 提示。
    """
    import os

    explicit = configured or os.environ.get("DAMAI_DEVICE_SERIAL", "").strip()
    if explicit or not adb_path.is_file():
        return explicit
    try:
        listing = subprocess.run(
            [str(adb_path), "devices"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        ).stdout
    except Exception:
        return ""
    ready = [
        parts[0]
        for parts in (line.split() for line in listing.splitlines()[1:])
        if len(parts) >= 2 and parts[1] == "device"
    ]
    return ready[0] if len(ready) == 1 else ""


class Adb:
    def __init__(self, path: Path, serial: str) -> None:
        self.path = path
        self.serial = serial

    def run(self, *args: str, device: bool = True, timeout: float = 15.0) -> str:
        cmd = [str(self.path)]
        if device:
            cmd += ["-s", self.serial]
        cmd += list(args)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(message or f"exit {proc.returncode}")
        return proc.stdout.strip()

    def shell(self, command: str) -> str:
        return self.run("shell", command)


def parse_size(text: str, label: str) -> tuple[int, int] | None:
    match = re.search(rf"{label} size:\s*(\d+)x(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def check_adb(adb: Adb) -> bool:
    if not adb.path.is_file():
        report("FAIL", "ADB", f"找不到 {adb.path}")
        return False
    version = adb.run("version", device=False).splitlines()
    shown = version[1] if len(version) > 1 else version[0]
    report("OK", "ADB", f"{shown} @ {adb.path}")
    return True


def check_device(adb: Adb) -> bool:
    listing = adb.run("devices", "-l", device=False)
    lines = [line for line in listing.splitlines()[1:] if line.strip()]
    mine = [line for line in lines if adb.serial and line.split()[0] == adb.serial]
    if not adb.serial:
        report(
            "FAIL",
            "设备连接",
            f"没有找到唯一一台已授权设备（当前：{[l.split()[:2] for l in lines] or '无'}）。"
            "请只连接一台手机并在手机上允许 USB 调试，或设置环境变量 DAMAI_DEVICE_SERIAL",
        )
        return False
    if not mine:
        others = ", ".join(line.split()[0] for line in lines) or "无"
        report("FAIL", "设备连接", f"未找到序列号 {adb.serial}（当前已连接：{others}）")
        return False
    state = mine[0].split()[1]
    if state != "device":
        hint = "请在手机上点「允许 USB 调试」" if state == "unauthorized" else "请重新插拔数据线"
        report("FAIL", "设备连接", f"{adb.serial} 状态为 {state}，{hint}")
        return False
    report("OK", "设备连接", f"{adb.serial} 已授权")
    if len(lines) > 1:
        report("WARN", "多设备", f"同时连接了 {len(lines)} 台设备，脚本只会操作 {adb.serial}")
    model = adb.shell("getprop ro.product.model")
    name = adb.shell("getprop ro.product.marketname")
    if model == EXPECTED_MODEL:
        report("OK", "机型", f"{name} ({model})")
    else:
        report("WARN", "机型", f"{name} ({model})，不是 K80（{EXPECTED_MODEL}），坐标可能不适用")
    return True


def check_resolution(adb: Adb, expected: tuple[int, int]) -> None:
    text = adb.shell("wm size")
    physical = parse_size(text, "Physical")
    override = parse_size(text, "Override")
    effective = override or physical
    if effective is None:
        report("FAIL", "分辨率", f"无法解析 wm size 输出：{text!r}")
        return
    if effective == tuple(expected):
        report("OK", "分辨率", f"{effective[0]}x{effective[1]}（与脚本校准一致）")
    else:
        report(
            "FAIL",
            "分辨率",
            f"当前生效 {effective[0]}x{effective[1]}（物理 {physical[0]}x{physical[1]}），"
            f"脚本需要 {expected[0]}x{expected[1]}。请在手机：设置 → 显示与亮度 → "
            "屏幕分辨率 → 选「WQHD+」，然后重新预检",
        )
    report("INFO", "DPI", adb.shell("wm density").replace("\n", "; "))


def check_packages(adb: Adb, damai: str, alipay: str) -> None:
    for label, package in (("大麦 App", damai), ("支付宝 App", alipay)):
        listed = adb.shell(f"pm list packages {package}").splitlines()
        if f"package:{package}" not in listed:
            report("FAIL", label, f"未安装 {package}")
            continue
        info = adb.shell(f"dumpsys package {package} | grep versionName")
        version = re.search(r"versionName=(\S+)", info)
        report("OK", label, f"{package} {version.group(1) if version else ''}".strip())


FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[^}]*\s+u\d+\s+([^\s/]+)/([^\s}]+)")


def foreground_activity(adb: Adb) -> tuple[str, str] | None:
    match = FOCUS_RE.search(adb.shell("dumpsys window | grep mCurrentFocus"))
    return (match.group(1), match.group(2)) if match else None


def foreground_package(adb: Adb) -> str | None:
    focus = foreground_activity(adb)
    return focus[0] if focus else None


def check_foreground(adb: Adb, damai: str) -> None:
    focus = foreground_activity(adb)
    shown = f"{focus[0]}/{focus[1]}" if focus else "未知"
    if focus and focus[0] == damai:
        report("OK", "前台", shown)
    else:
        report("WARN", "前台", f"{shown}。开抢前必须手动停在大麦目标演唱会详情页")


def check_screen(adb: Adb) -> None:
    wake = re.search(r"mWakefulness=(\w+)", adb.shell("dumpsys power | grep mWakefulness="))
    state = wake.group(1) if wake else "unknown"
    report("OK" if state == "Awake" else "WARN", "屏幕状态", state)
    stay_on = adb.shell("settings get global stay_on_while_plugged_in")
    if stay_on not in ("", "0", "null"):
        report("OK", "充电常亮", f"stay_on_while_plugged_in={stay_on}（插线时不会息屏）")
        return
    timeout_ms = adb.shell("settings get system screen_off_timeout")
    minutes = int(timeout_ms) / 60000 if timeout_ms.isdigit() else -1
    report(
        "OK" if minutes >= 10 else "WARN",
        "息屏时间",
        f"{minutes:g} 分钟。建议在开发者选项开启「充电时屏幕保持唤醒」",
    )


def check_battery(adb: Adb) -> None:
    level = re.search(r"level:\s*(\d+)", adb.shell("dumpsys battery"))
    if level:
        pct = int(level.group(1))
        report("OK" if pct >= 30 else "WARN", "电量", f"{pct}%")


def sample_taobao() -> tuple[float, float]:
    """返回 (往返 ms, 服务器时间 - 电脑时间 ms)。"""
    t0 = time.time()
    with urllib.request.urlopen(TAOBAO_TIME_URL, timeout=3) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    t1 = time.time()
    server_ms = float(payload["data"]["t"])
    return (t1 - t0) * 1000, server_ms - (t0 + t1) / 2 * 1000


def sample_ntp() -> tuple[float, float]:
    """SNTP 查询阿里云 NTP，返回 (往返 ms, 服务器时间 - 电脑时间 ms)。"""
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        t0 = time.time()
        sock.sendto(packet, (NTP_SERVER, 123))
        data, _ = sock.recvfrom(48)
        t1 = time.time()
    seconds, fraction = struct.unpack("!II", data[40:48])
    server_s = seconds - NTP_EPOCH_DELTA + fraction / 2**32
    return (t1 - t0) * 1000, (server_s - (t0 + t1) / 2) * 1000


def best_sample(sampler) -> tuple[float, float] | None:
    best = None
    for _ in range(5):
        try:
            sample = sampler()
        except Exception:
            continue
        if best is None or sample[0] < best[0]:
            best = sample
    return best


def check_clock(adb: Adb) -> None:
    """抢票脚本按【电脑】本地时间计算开售时刻，所以重点校验电脑时钟。"""
    source = "淘宝服务器时间"
    best = best_sample(sample_taobao)
    if best is None:
        source = f"NTP {NTP_SERVER}"
        best = best_sample(sample_ntp)
    if best is None:
        report("WARN", "电脑时钟", "淘宝时间接口与 NTP 均不可达，跳过校时检查")
    else:
        rtt, offset = best
        direction = "慢" if offset > 0 else "快"
        ok = abs(offset) <= CLOCK_WARN_MS
        detail = f"电脑比{source}{direction} {abs(offset):.0f} ms（最小往返 {rtt:.0f} ms）"
        if not ok:
            detail += "。请同步 Windows 时间：设置 → 时间和语言 → 立即同步"
        report("OK" if ok else "WARN", "电脑时钟", detail)
    try:
        phone_s = float(adb.shell("date +%s.%N"))
        diff = (time.time() - phone_s) * 1000
        report("INFO", "手机时钟", f"与电脑相差 {diff:+.0f} ms（含 adb 延迟，仅供参考）")
    except Exception as exc:
        report("INFO", "手机时钟", f"读取失败：{exc}")



def bounds_of(node: ET.Element) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match:
        return None
    x1, y1, x2, y2 = (int(v) for v in match.groups())
    return x1, y1, x2, y2


def check_uiautomator(
    adb: Adb, expected: tuple[int, int], point: tuple[int, int], damai: str
) -> None:
    """用抢票脚本同一口径（u2 window_size）复核，并只读检查主操作点命中的控件。"""
    try:
        import uiautomator2 as u2
    except ImportError:
        report("WARN", "uiautomator2", "未安装，请用 .venv-phone\\Scripts\\python.exe 运行本脚本")
        return
    try:
        device = u2.connect(adb.serial)
        size = tuple(device.window_size())
    except Exception as exc:
        report("FAIL", "uiautomator2", f"连接失败：{type(exc).__name__}: {exc}")
        return
    if size == tuple(expected):
        report("OK", "u2 window_size", f"{size[0]}x{size[1]}（与抢票脚本预检口径一致）")
    else:
        report("FAIL", "u2 window_size", f"{size} != {tuple(expected)}，抢票脚本会直接报错退出")

    # 注意：不要用 device.app_current()。K80/HyperOS 上它会误报最后一个任务为前台，
    # 这里与抢票脚本保持一致，用 `dumpsys window` 的 mCurrentFocus 判断。
    if foreground_package(adb) != damai:
        report("INFO", "坐标命中", "大麦不在前台，跳过")
        return
    # 分辨率不一致时，按比例换算出主操作点在当前屏幕上的等价位置，仅用于诊断。
    px = round(point[0] * size[0] / expected[0])
    py = round(point[1] * size[1] / expected[1])
    note = "" if size == tuple(expected) else f"（换算自 {point[0]},{point[1]}）"
    try:
        root = ET.fromstring(device.dump_hierarchy())
    except Exception as exc:
        report("WARN", "坐标命中", f"读取界面层级失败：{exc}")
        return
    hits = []
    for node in root.iter("node"):
        box = bounds_of(node)
        if box and node.get("clickable") == "true" and box[0] <= px <= box[2] and box[1] <= py <= box[3]:
            hits.append(((box[2] - box[0]) * (box[3] - box[1]), node, box))
    if not hits:
        report(
            "WARN",
            "坐标命中",
            f"主操作点 ({px},{py}){note} 在当前页面未落在可点击控件上。"
            "请在详情页、票档面板分别再跑一次预检确认",
        )
        return
    _, node, box = min(hits, key=lambda item: item[0])
    rid = node.get("resource-id") or "-"
    text = node.get("text") or node.get("content-desc") or "-"
    report(
        "INFO",
        "坐标命中",
        f"主操作点 ({px},{py}){note} -> {rid}「{text}」 bounds={list(box)}。"
        "在详情页应命中购票按钮、在票档面板应命中「确认」，否则说明坐标不对",
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="REDMI K80 抢票前一键预检（只读）")
    parser.add_argument("--adb", type=Path, default=DEFAULT_ADB, help="adb.exe 路径")
    parser.add_argument("--skip-time", action="store_true", help="跳过联网校时检查")
    args = parser.parse_args()

    consts = load_constants()
    serial = resolve_serial(args.adb, str(consts["DEVICE_SERIAL"]))
    expected = tuple(consts["EXPECTED_SCREEN_SIZE"])
    damai = str(consts["DAMAI_PACKAGE"])
    print(f"== REDMI K80 预检 {datetime.now():%Y-%m-%d %H:%M:%S} ==")
    print(f"目标脚本：{TARGET_SCRIPT}")
    print(f"序列号：{serial}  校准分辨率：{expected[0]}x{expected[1]}\n")

    adb = Adb(args.adb, serial)
    try:
        if check_adb(adb) and check_device(adb):
            check_resolution(adb, expected)
            check_packages(adb, damai, str(consts["ALIPAY_PACKAGE"]))
            check_foreground(adb, damai)
            check_screen(adb)
            check_battery(adb)
            if not args.skip_time:
                check_clock(adb)
            check_uiautomator(adb, expected, tuple(consts["MAIN_ACTION_POINT"]), damai)
    except subprocess.TimeoutExpired as exc:
        report("FAIL", "ADB", f"命令超时：{exc.cmd}")
    except RuntimeError as exc:
        report("FAIL", "ADB", str(exc))

    fails = sum(1 for level, _, _ in results if level == "FAIL")
    warns = sum(1 for level, _, _ in results if level == "WARN")
    print(f"\n== 结果：{fails} 项 FAIL，{warns} 项 WARN ==")
    if fails:
        print("存在 FAIL，请先修复再运行 scripts\\damai_mode0_K80.py")
    else:
        print("预检通过。开抢前把手机停在目标演唱会详情页，然后运行：")
        print('  .\\.venv-phone\\Scripts\\python.exe scripts\\damai_mode0_K80.py "YYYY-MM-DD HH:MM:SS"')
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

