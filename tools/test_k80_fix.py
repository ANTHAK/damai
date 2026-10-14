"""离线验证 K80 修复（不连手机、不发出任何点击）。

用法（项目根目录）：
    .\\.venv-phone\\Scripts\\python.exe tools\\test_k80_fix.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import damai_mode0_K80 as k80  # noqa: E402
from damai_android_tail import run_tail_probe_loop  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.c = k80.Mode0StateController(clock=self.clock)

    def observe(self, page: str, at: float) -> str | None:
        self.clock.now = at
        return self.c.observe(page).action

    def test_replay_1130_stuck_dialog_is_retried(self) -> None:
        """重放 11:30 实战：第 2 个弹窗点了没消失，之后 30 秒一直是弹窗。"""
        self.assertIsNone(self.observe(k80.PAGE_ORDER_LOADING, 1.436))
        self.assertEqual(self.observe(k80.PAGE_ORDER_PAGE, 2.074), "submit")
        self.assertEqual(self.observe(k80.PAGE_RETRY_DIALOG, 2.894), "retry")
        self.assertIsNone(self.observe(k80.PAGE_ORDER_LOADING, 3.279))
        self.assertEqual(self.observe(k80.PAGE_ORDER_PAGE, 3.463), "submit")
        self.assertEqual(self.observe(k80.PAGE_RETRY_DIALOG, 4.638), "retry")
        # 旧逻辑：之后 30 秒 action 全是 None。新逻辑：约每 1 秒重点一次。
        retries = []
        step = 0
        while True:
            step += 1
            t = 4.638 + step * 0.2  # 背靠背探测，一轮约 0.2s
            if t >= 35.5:
                break
            if self.observe(k80.PAGE_RETRY_DIALOG, t) == "retry":
                retries.append(t)
        self.assertGreaterEqual(len(retries), 29)
        self.assertLessEqual(len(retries), 31)
        self.assertAlmostEqual(retries[0], 5.638, delta=0.25)
        gaps = [b - a for a, b in zip(retries, retries[1:])]
        self.assertTrue(all(0.95 <= g <= 1.25 for g in gaps), gaps)
        self.assertEqual(self.c.retry_repeat_count, len(retries))

    def test_no_repeat_within_interval(self) -> None:
        self.observe(k80.PAGE_ORDER_PAGE, 0.0)
        self.assertEqual(self.observe(k80.PAGE_RETRY_DIALOG, 1.0), "retry")
        self.assertIsNone(self.observe(k80.PAGE_RETRY_DIALOG, 1.5))
        self.assertIsNone(self.observe(k80.PAGE_RETRY_DIALOG, 1.99))
        self.assertEqual(self.observe(k80.PAGE_RETRY_DIALOG, 2.0), "retry")

    def test_dialog_closes_normally_unchanged(self) -> None:
        """弹窗正常消失时，行为与原版完全一致。"""
        self.observe(k80.PAGE_ORDER_PAGE, 0.0)
        self.assertEqual(self.observe(k80.PAGE_RETRY_DIALOG, 0.5), "retry")
        self.assertIsNone(self.observe(k80.PAGE_ORDER_LOADING, 0.8))
        self.assertEqual(self.c.phase, "await_submit")
        self.assertEqual(self.observe(k80.PAGE_ORDER_PAGE, 1.0), "submit")
        self.assertEqual(self.observe(k80.PAGE_RETRY_DIALOG, 1.2), "retry")
        self.assertEqual(self.c.retry_repeat_count, 0)

    def test_terminal_dialog_still_never_clicked(self) -> None:
        self.observe(k80.PAGE_ORDER_PAGE, 0.0)
        d1 = self.c.observe(k80.PAGE_TERMINAL_DIALOG)
        d2 = self.c.observe(k80.PAGE_TERMINAL_DIALOG)
        self.assertIsNone(d1.action)
        self.assertIsNone(d2.action)
        self.assertEqual(d2.status, "terminal_no_retry_option")

    def test_risk_page_still_backs_once(self) -> None:
        self.observe(k80.PAGE_ORDER_PAGE, 0.0)
        self.assertEqual(self.observe(k80.PAGE_RISK_VERIFICATION, 1.0), "back")
        self.assertIsNone(self.observe(k80.PAGE_RISK_VERIFICATION, 3.0))
        self.assertIsNone(self.observe(k80.PAGE_RISK_VERIFICATION, 9.0))


class FakeDevice:
    def __init__(self) -> None:
        self.dumps = 0

    def dump_hierarchy(self) -> str:
        self.dumps += 1
        return "<hierarchy><node text='继续尝试'/></hierarchy>"

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG fake")


class FakeInner:
    def __init__(self, pages: list[str]) -> None:
        self.pages = pages

    def read(self, phase: str) -> str:
        return self.pages.pop(0) if self.pages else k80.PAGE_RETRY_DIALOG


class DiagnosticProbeTests(unittest.TestCase):
    def test_heartbeat_and_limited_snapshots(self) -> None:
        fake_now = [100.0]
        real_perf = k80.time.perf_counter
        old_dir = k80.ARTIFACTS_DIR
        out = io.StringIO()
        device = FakeDevice()
        with tempfile.TemporaryDirectory() as tmp:
            k80.ARTIFACTS_DIR = Path(tmp)
            k80.time.perf_counter = lambda: fake_now[0]
            try:
                probe = k80.DiagnosticProbe(FakeInner([]), device, sale_monotonic=0.0)
                with contextlib.redirect_stdout(out):
                    for _ in range(100):  # 同一弹窗持续 10 秒
                        probe.read("retry_clicked")
                        probe.join(1.0)
                        fake_now[0] += 0.1
            finally:
                k80.time.perf_counter = real_perf
                k80.ARTIFACTS_DIR = old_dir
            files = sorted(p.name for p in Path(tmp).iterdir())
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        beats = [e for e in events if e["event"] == "heartbeat"]
        snaps = [e for e in events if e["event"] == "dialog_snapshot"]
        self.assertGreaterEqual(len(beats), 8)
        self.assertEqual(len(snaps), k80.DIALOG_SNAPSHOT_LIMIT)
        self.assertEqual(device.dumps, k80.DIALOG_SNAPSHOT_LIMIT)
        self.assertEqual(len(files), 2 * k80.DIALOG_SNAPSHOT_LIMIT)
        self.assertTrue(all(e["error"] is None for e in snaps))

    def test_page_passthrough_unchanged(self) -> None:
        pages = [k80.PAGE_ORDER_PAGE, k80.PAGE_RETRY_DIALOG, k80.PAGE_ORDER_LOADING]
        probe = k80.DiagnosticProbe(FakeInner(list(pages)), FakeDevice(), 0.0)
        self.assertEqual([probe.read("x") for _ in pages], pages)


class TailLoopIntegrationTest(unittest.TestCase):
    """真实 run_tail_probe_loop + 新状态机：卡住的弹窗会持续产生 retry 请求。"""

    def test_loop_requests_repeated_retry(self) -> None:
        requested: list[str] = []
        stop = threading.Event()
        pages = [k80.PAGE_ORDER_PAGE] + [k80.PAGE_RETRY_DIALOG] * 100_000

        class Probe:
            def read(self, phase: str) -> str:
                return pages.pop(0)

        class Requests:
            def request(self, action: str) -> None:
                requested.append(action)
                if requested.count("retry") >= 3:
                    stop.set()

            def clear(self) -> None:
                pass

        now = k80.time.perf_counter()
        schedule = SimpleNamespace(
            first_probe_monotonic=0.0,
            deadline_monotonic=now + 10,
            sale_monotonic=now,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            run_tail_probe_loop(
                schedule=schedule,
                state=k80.RuntimeState(),
                stop_event=stop,
                controlled_event=threading.Event(),
                requests=Requests(),
                controller=k80.Mode0StateController(),
                page_probe=Probe(),
                wait_until=lambda target, ev: not ev.is_set(),
                state_check_interval_s=0.01,
                risk_state_check_interval_s=0.25,
            )
        elapsed = k80.time.perf_counter() - now
        self.assertEqual(requested[0], "submit")
        self.assertEqual(requested.count("retry"), 3)
        self.assertGreater(elapsed, 1.9)  # 第 2、3 次各间隔约 1s
        self.assertLess(elapsed, 4.0)


class StuckPageSnapshotTests(unittest.TestCase):
    """13:00 实战：确认页一直是 order_page_loading，也要自动留现场（只读）。"""

    def run_probe(self, page: str, start: float, sale: float, seconds: float) -> tuple[list[dict], FakeDevice]:
        fake_now = [start]
        real_perf = k80.time.perf_counter
        old_dir = k80.ARTIFACTS_DIR
        out = io.StringIO()
        device = FakeDevice()
        with tempfile.TemporaryDirectory() as tmp:
            k80.ARTIFACTS_DIR = Path(tmp)
            k80.time.perf_counter = lambda: fake_now[0]
            try:
                probe = k80.DiagnosticProbe(FakeInner([page] * 100_000), device, sale_monotonic=sale)
                with contextlib.redirect_stdout(out):
                    while fake_now[0] < start + seconds:
                        probe.read("await_submit")
                        probe.join(1.0)
                        fake_now[0] += 0.1
            finally:
                k80.time.perf_counter = real_perf
                k80.ARTIFACTS_DIR = old_dir
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        return events, device

    def test_order_loading_stuck_saves_limited_snapshots(self) -> None:
        events, device = self.run_probe(k80.PAGE_ORDER_LOADING, start=1.5, sale=0.0, seconds=60)
        snaps = [e for e in events if e["event"] == "stuck_page_snapshot"]
        self.assertEqual(len(snaps), k80.STUCK_PAGE_SNAPSHOT_LIMIT)
        self.assertEqual(device.dumps, k80.STUCK_PAGE_SNAPSHOT_LIMIT)
        self.assertGreaterEqual(snaps[0]["same_page_seconds"], 2.9)
        self.assertLess(snaps[0]["seconds_after_sale"], 5.0)
        self.assertIn("bottom_controls", snaps[0])

    def test_waiting_on_detail_before_sale_does_not_snapshot(self) -> None:
        """开售前停在详情页（damai_other）是正常等待，不应留现场。"""
        events, device = self.run_probe(k80.PAGE_DAMAI_OTHER, start=0.0, sale=120.0, seconds=60)
        self.assertEqual([e for e in events if "snapshot" in e["event"]], [])
        self.assertEqual(device.dumps, 0)

    def test_bottom_controls_summary(self) -> None:
        xml = (
            "<hierarchy>"
            "<node text='立即提交' resource-id='x:id/submit' clickable='false' enabled='false' bounds='[900,2960][1360,3120]'/>"
            "<node text='¥380.00' resource-id='' clickable='false' enabled='true' bounds='[80,3000][400,3080]'/>"
            "<node text='标题' resource-id='' clickable='false' enabled='true' bounds='[0,100][500,200]'/>"
            "</hierarchy>"
        )
        rows = k80.summarize_bottom_controls(xml)
        self.assertEqual([r["text"] for r in rows], ["立即提交", "¥380.00"])
        self.assertFalse(rows[0]["enabled"])


ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts"


class SeatPageTests(unittest.TestCase):
    """15:00 实战：选座票会卡在选座页，脚本必须点「确认选座」且不挑座位。"""

    SEAT_XML = ARTIFACTS / "stuck_damai_other_20261014_150455_692.xml"

    def make_seat_probe(self, button_text: str, bounds: dict) -> k80.FastPageProbe:
        holder = k80.RetryPointHolder()
        probe = k80.FastPageProbe.__new__(k80.FastPageProbe)
        probe.retry_points = holder
        probe.seat_points = k80.RetryPointHolder()
        probe.last_dialog_text = ""
        probe.seat_confirm = FakeSelector({"text": button_text, "visibleBounds": bounds})
        probe.retry = FakeSelector(None)
        probe.tip = FakeSelector(None)
        probe.submit = FakeSelector(None)
        probe.back_redirect = FakeSelector(None)
        probe.risk = FakeSelector(None)
        probe.order_title = FakeSelector(None)
        return probe

    def test_seat_page_records_measured_center(self) -> None:
        probe = self.make_seat_probe(
            "确认选座", {"left": 784, "top": 2945, "right": 1384, "bottom": 3118}
        )
        self.assertEqual(probe.read("blind"), k80.PAGE_SEAT_PAGE)
        # 中心 = ((784+1384)/2, (2945+3118)/2) = (1084, 3031)，与盲点坐标 (1119,3088) 不同
        self.assertEqual(probe.seat_points.fresh(), (1084, 3031))

    def test_seat_page_wrong_text_not_recorded(self) -> None:
        probe = self.make_seat_probe(
            "选好了", {"left": 784, "top": 2945, "right": 1384, "bottom": 3118}
        )
        self.assertEqual(probe.read("blind"), k80.PAGE_SEAT_PAGE)
        self.assertIsNone(probe.seat_points.fresh())

    def test_seat_page_emits_seat_action_with_throttle(self) -> None:
        clock = FakeClock()
        c = k80.Mode0StateController(clock=clock)
        d1 = c.observe(k80.PAGE_SEAT_PAGE)
        self.assertEqual(d1.action, "seat")
        clock.now = 0.5  # 未到间隔
        self.assertIsNone(c.observe(k80.PAGE_SEAT_PAGE).action)
        clock.now = 1.6  # 超过 SEAT_REPEAT_AFTER_S
        self.assertEqual(c.observe(k80.PAGE_SEAT_PAGE).action, "seat")
        self.assertEqual(c.seat_click_count, 2)

    def test_seat_page_does_not_stop(self) -> None:
        clock = FakeClock()
        c = k80.Mode0StateController(clock=clock)
        for i in range(20):
            clock.now = i * 2.0
            d = c.observe(k80.PAGE_SEAT_PAGE)
            self.assertIsNone(d.status)
            self.assertIsNotNone(d.action)  # 每 1.5s 都会点一次

    @unittest.skipUnless(
        ARTIFACTS.joinpath("stuck_damai_other_20261014_150455_692.xml").exists(),
        "缺少 15:00 选座页实战快照",
    )
    def test_real_1500_seat_snapshot_is_seat_page(self) -> None:
        xml = self.SEAT_XML.read_text(encoding="utf-8")
        self.assertEqual(k80.classify_hierarchy_xml(xml), k80.PAGE_SEAT_PAGE)

    def test_classify_hierarchy_seat_page(self) -> None:
        xml = (
            "<hierarchy>"
            "<node resource-id='cn.damai:id/seat_confirm_order' text='确认选座' clickable='true' bounds='[784,2945][1384,3118]'/>"
            "</hierarchy>"
        )
        self.assertEqual(k80.classify_hierarchy_xml(xml), k80.PAGE_SEAT_PAGE)


SOLD_OUT_XML = ARTIFACTS / "stuck_retry_dialog_20261014_122036_927.xml"


class SoldOutDialogTests(unittest.TestCase):
    """12:20 实战：库存不足弹窗绝不能被当成「继续尝试」。"""

    def test_classify_button_text(self) -> None:
        self.assertEqual(k80.classify_confirm_button("继续尝试", ""), k80.PAGE_RETRY_DIALOG)
        self.assertEqual(
            k80.classify_confirm_button("返回重新选购", "对不起，您选购的商品库存不足，请重新选购"),
            k80.PAGE_SOLD_OUT_DIALOG,
        )
        self.assertEqual(k80.classify_confirm_button("确定", "库存不足"), k80.PAGE_SOLD_OUT_DIALOG)
        self.assertEqual(k80.classify_confirm_button("我知道了", "网络异常"), k80.PAGE_UNKNOWN_DIALOG)
        self.assertEqual(k80.classify_confirm_button("", ""), k80.PAGE_UNKNOWN_DIALOG)

    @unittest.skipUnless(SOLD_OUT_XML.exists(), "缺少 12:20 实战快照")
    def test_real_1220_snapshot_is_sold_out(self) -> None:
        xml = SOLD_OUT_XML.read_text(encoding="utf-8")
        self.assertEqual(k80.classify_hierarchy_xml(xml), k80.PAGE_SOLD_OUT_DIALOG)

    def test_sold_out_never_clicks_and_stops(self) -> None:
        c = k80.Mode0StateController(clock=FakeClock())
        c.observe(k80.PAGE_ORDER_PAGE)
        d1 = c.observe(k80.PAGE_SOLD_OUT_DIALOG)
        d2 = c.observe(k80.PAGE_SOLD_OUT_DIALOG)
        self.assertIsNone(d1.action)
        self.assertIsNone(d1.status)
        self.assertTrue(d1.clear_pending)
        self.assertIsNone(d2.action)
        self.assertEqual(d2.status, "terminal_no_retry_option")

    def test_replay_1220_sold_out_after_retry(self) -> None:
        """重放 12:20：继续尝试循环中弹出库存不足 → 0 次点击，立刻停止。"""
        clock = FakeClock()
        c = k80.Mode0StateController(clock=clock)
        clock.now = 34.0
        c.observe(k80.PAGE_ORDER_PAGE)
        clock.now = 35.8
        actions = []
        status = None
        while status is None and clock.now < 600:
            d = c.observe(k80.PAGE_SOLD_OUT_DIALOG)
            actions.append(d.action)
            status = d.status
            clock.now += 0.2
        self.assertEqual(status, "terminal_no_retry_option")
        self.assertEqual([a for a in actions if a], [])
        self.assertLess(clock.now, 36.5)

    def test_unknown_dialog_never_clicked(self) -> None:
        clock = FakeClock()
        c = k80.Mode0StateController(clock=clock)
        c.observe(k80.PAGE_ORDER_PAGE)
        for i in range(50):
            clock.now = i * 0.2
            d = c.observe(k80.PAGE_UNKNOWN_DIALOG)
            self.assertIsNone(d.action)
            self.assertIsNone(d.status)
        self.assertEqual(c.unknown_dialog_hits, 50)


class RetryPointTests(unittest.TestCase):
    def test_point_expires_and_clears(self) -> None:
        clock = FakeClock()
        h = k80.RetryPointHolder(clock=clock)
        self.assertIsNone(h.fresh())
        h.set((720, 1966))
        self.assertEqual(h.fresh(), (720, 1966))
        clock.now = k80.RETRY_POINT_MAX_AGE_S + 0.01
        self.assertIsNone(h.fresh())
        h.set((700, 2000))
        h.clear()
        self.assertIsNone(h.fresh())

    def test_bounds_center(self) -> None:
        b = {"left": 244, "top": 1891, "right": 1195, "bottom": 2041}
        self.assertEqual(k80.bounds_center(b), (719, 1966))
        self.assertIsNone(k80.bounds_center(None))
        self.assertIsNone(k80.bounds_center({"left": 1}))


class FakeSelector:
    def __init__(self, info: dict | None) -> None:
        self._info = info

    def exists(self, timeout: float = 0) -> bool:
        return self._info is not None

    @property
    def info(self) -> dict:
        if self._info is None:
            raise RuntimeError("gone")
        return self._info


class ProbeClassificationTests(unittest.TestCase):
    def make_probe(self, button_text: str, tip_text: str, bounds: dict) -> k80.FastPageProbe:
        holder = k80.RetryPointHolder()
        probe = k80.FastPageProbe.__new__(k80.FastPageProbe)
        probe.retry_points = holder
        probe.seat_points = k80.RetryPointHolder()
        probe.last_dialog_text = ""
        probe.retry = FakeSelector({"text": button_text, "visibleBounds": bounds})
        probe.tip = FakeSelector({"text": tip_text})
        probe.submit = FakeSelector(None)
        probe.back_redirect = FakeSelector(None)
        probe.risk = FakeSelector(None)
        probe.order_title = FakeSelector(None)
        probe.seat_confirm = FakeSelector(None)
        return probe

    def test_sold_out_probe_has_no_tap_point(self) -> None:
        probe = self.make_probe(
            "返回重新选购",
            "对不起，您选购的商品库存不足，请重新选购",
            {"left": 244, "top": 1975, "right": 1195, "bottom": 2125},
        )
        self.assertEqual(probe.read("await_result"), k80.PAGE_SOLD_OUT_DIALOG)
        self.assertIsNone(probe.retry_points.fresh())

    def test_retry_probe_records_measured_center(self) -> None:
        probe = self.make_probe(
            "继续尝试", "", {"left": 244, "top": 1891, "right": 1195, "bottom": 2041}
        )
        self.assertEqual(probe.read("await_result"), k80.PAGE_RETRY_DIALOG)
        self.assertEqual(probe.retry_points.fresh(), (719, 1966))



if __name__ == "__main__":
    unittest.main()

