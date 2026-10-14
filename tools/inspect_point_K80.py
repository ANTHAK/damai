"""只读：导出当前大麦页面层级，列出主操作点 / 重试点命中的控件和底部可点击按钮。

用法（项目根目录）：
    .\\.venv-phone\\Scripts\\python.exe tools\\inspect_point_K80.py [标签]
会把层级 XML 保存到 artifacts\\inspect_<标签>_<时间>.xml（artifacts 已被 .gitignore 忽略）。
不会点击手机。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET

import uiautomator2 as u2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from precheck_K80 import (  # noqa: E402
    DEFAULT_ADB,
    Adb,
    foreground_activity,
    load_constants,
    resolve_serial,
)

ROOT = Path(__file__).resolve().parent.parent


def bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    m = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    return tuple(int(v) for v in m.groups()) if m else None  # type: ignore[return-value]


def label(node: ET.Element) -> str:
    rid = node.get("resource-id") or "-"
    text = node.get("text") or node.get("content-desc") or ""
    return f"{rid}「{text}」 clickable={node.get('clickable')}"


def hits_at(root: ET.Element, x: int, y: int) -> list[tuple[int, ET.Element, tuple]]:
    found = []
    for node in root.iter("node"):
        box = bounds(node)
        if box and box[0] <= x <= box[2] and box[1] <= y <= box[3]:
            found.append(((box[2] - box[0]) * (box[3] - box[1]), node, box))
    return sorted(found, key=lambda item: item[0])


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tag = sys.argv[1] if len(sys.argv) > 1 else "page"
    consts = load_constants()
    adb = Adb(DEFAULT_ADB, resolve_serial(DEFAULT_ADB, str(consts["DEVICE_SERIAL"])))
    if not adb.serial:
        print("没有找到唯一一台已授权的 adb 设备，或请设置环境变量 DAMAI_DEVICE_SERIAL")
        return 1
    focus = foreground_activity(adb)
    print(f"前台：{focus[0] + '/' + focus[1] if focus else '未知'}")

    device = u2.connect(adb.serial)
    print(f"window_size：{device.window_size()}")
    xml = device.dump_hierarchy()
    out_dir = ROOT / "artifacts"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"inspect_{tag}_{datetime.now():%Y%m%d_%H%M%S}.xml"
    out.write_text(xml, encoding="utf-8")
    print(f"层级已保存：{out}\n")
    root = ET.fromstring(xml)

    for name in ("MAIN_ACTION_POINT", "RETRY_DIALOG_POINT"):
        x, y = consts[name]  # type: ignore[misc]
        print(f"== {name} ({x},{y}) 命中（从小到大）==")
        for _, node, box in hits_at(root, x, y)[:5]:
            print(f"  {list(box)}  {label(node)}")
        print()

    height = device.window_size()[1]
    print("== 屏幕底部 25% 内带文字的控件 ==")
    seen = set()
    for node in root.iter("node"):
        box = bounds(node)
        text = node.get("text") or node.get("content-desc") or ""
        if box and text.strip() and box[1] >= height * 0.75 and (box, text) not in seen:
            seen.add((box, text))
            print(f"  {list(box)}  {label(node)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
