"""端到端测试：以最小框架桩跑通 /任务列表 的完整链路。

覆盖命令注册、排序过滤、图片/文字切换、缓存复用与渲染降级。
可直接 `python tests/test_end_to_end.py` 运行。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))


# ─────────────────────── 假 astrbot 模块 ───────────────────────


def _install_fake_astrbot() -> None:
    """造出插件 import 时用到的 astrbot 模块骨架。"""

    import logging

    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")

    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("fake-astrbot")
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # pragma: no cover - 仅作为类型存在的替身
        pass

    class _Filter:
        """只保留插件真正会用的装饰器。"""

        PermissionType = types.SimpleNamespace(ADMIN="admin")

        @staticmethod
        def command(*_names: str, **_kwargs: Any):
            def wrapper(func):
                func._is_command = True
                return func

            return wrapper

        @staticmethod
        def permission_type(*_args: Any, **_kwargs: Any):
            def wrapper(func):
                return func

            return wrapper

        @staticmethod
        def llm_tool(*_args: Any, **_kwargs: Any):
            raise AssertionError("本插件不应该注册任何 LLM 工具")

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()

    star_mod = types.ModuleType("astrbot.api.star")

    class Context:  # pragma: no cover - 替身
        pass

    class Star:
        def __init__(self, context: Any = None, config: Any = None) -> None:
            self.context = context
            self.config = config

    star_mod.Context = Context
    star_mod.Star = Star

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_fake_astrbot()

from astrbot_plugin_task_list import main as plugin_main  # noqa: E402
from astrbot_plugin_task_list.models import TaskListData  # noqa: E402


# ─────────────────────── 假框架对象 ───────────────────────


class FakeJob:
    def __init__(self, **kwargs: Any) -> None:
        self.job_id = kwargs.get("job_id", "job-1")
        self.name = kwargs.get("name", "测试任务")
        self.job_type = kwargs.get("job_type", "active_agent")
        self.cron_expression = kwargs.get("cron_expression", "0 9 * * *")
        self.payload = kwargs.get("payload", {"session": "aiocqhttp:GroupMessage:123"})
        self.enabled = kwargs.get("enabled", True)
        self.run_once = kwargs.get("run_once", False)
        self.description = kwargs.get("description", "")
        self.last_error = kwargs.get("last_error", None)
        self.next_run_time = kwargs.get("next_run_time", None)
        self.timezone = kwargs.get("timezone", None)


def _utc(*args: int):
    from datetime import datetime, timezone

    return datetime(*args, tzinfo=timezone.utc)


class FakeDB:
    def __init__(self, jobs: list[FakeJob]) -> None:
        self.jobs = jobs

    async def list_cron_jobs(self, job_type: str | None = None):
        return list(self.jobs)


class FakeCronManager:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping

    def get_next_run_time(self, job_id: str):
        return self.mapping.get(job_id)


class FakeContext:
    def __init__(self, jobs: list[FakeJob], live: dict[str, Any] | None = None) -> None:
        self.db = FakeDB(jobs)
        self.cron_manager = FakeCronManager(live or {})

    def get_db(self):
        return self.db


class FakeEvent:
    def __init__(self, umo: str = "aiocqhttp:GroupMessage:123", admin: bool = True) -> None:
        self.unified_msg_origin = umo
        self._admin = admin
        self.stopped = False
        self.results: list[tuple[str, str]] = []

    def stop_event(self) -> None:
        self.stopped = True

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str):
        self.results.append(("text", text))
        return ("text", text)

    def image_result(self, path: str):
        self.results.append(("image", path))
        return ("image", path)


def _make_plugin(context: Any, config: dict | None = None):
    plugin = plugin_main.TaskListPlugin(context, config or {})
    return plugin


async def _run_command(plugin, event) -> list[tuple[str, str]]:
    out = []
    async for item in plugin.cmd_task_list(event):
        out.append(item)
    return out


# ─────────────────────── 用例 ───────────────────────


def _jobs() -> list[FakeJob]:
    return [
        FakeJob(job_id="late", name="晚上的任务", cron_expression="0 20 * * *", next_run_time=_utc(2026, 11, 24, 12, 0)),
        FakeJob(job_id="early", name="早上的任务", cron_expression="0 9 * * *", next_run_time=_utc(2026, 11, 23, 1, 0)),
        FakeJob(job_id="off", name="已停用任务", enabled=False, next_run_time=_utc(2026, 11, 23, 1, 0)),
        FakeJob(job_id="unsched", name="未排程任务", next_run_time=None),
    ]


def test_collect_detects_source_per_job() -> None:
    """三种来源要在同一次采集里各自识别正确。"""

    jobs = [
        FakeJob(job_id="a", name="AI 建的", payload={"session": "s", "origin": "tool"},
                next_run_time=_utc(2026, 11, 23, 1, 0)),
        FakeJob(job_id="b", name="面板建的", payload={"session": "s", "origin": "api"},
                next_run_time=_utc(2026, 11, 23, 2, 0)),
        FakeJob(job_id="c", name="插件建的", job_type="basic", payload={},
                next_run_time=_utc(2026, 11, 23, 3, 0)),
        FakeJob(job_id="d", name="老任务", payload={"session": "s"},
                next_run_time=_utc(2026, 11, 23, 4, 0)),
    ]
    data = asyncio.run(plugin_main.collect_tasks(FakeContext(jobs), max_tasks=10))
    got = {entry.name: (entry.source, entry.type_label) for entry in data.entries}
    assert got["AI 建的"] == ("ai", "AI 自建")
    assert got["面板建的"] == ("user", "用户自建")
    assert got["插件建的"] == ("plugin", "插件内置")
    assert got["老任务"] == ("unknown", "插件自建")


def test_one_time_sorted_before_recurring_in_sections() -> None:
    """一次性任务进「一次性提醒」段，循环任务进「循环提醒」段。"""

    jobs = [
        FakeJob(job_id="r1", name="循环任务", cron_expression="0 9 * * *",
                next_run_time=_utc(2026, 11, 23, 1, 0)),
        FakeJob(job_id="o1", name="一次性任务", run_once=True,
                payload={"session": "s", "origin": "api", "run_at": "2026-11-24T20:00:00+08:00"},
                next_run_time=_utc(2026, 11, 24, 12, 0)),
    ]
    data = asyncio.run(plugin_main.collect_tasks(FakeContext(jobs), max_tasks=10))
    assert [e.name for e in data.one_time_entries] == ["一次性任务"]
    assert [e.name for e in data.recurring_entries] == ["循环任务"]


def test_only_one_command_registered() -> None:
    """插件里只能有一个 @filter.command，且必须叫「任务列表」。"""

    import inspect
    import re

    source = inspect.getsource(plugin_main)
    commands = re.findall(r"@filter\.command\(([^)]*)\)", source)
    assert len(commands) == 1, f"只允许注册一个命令，实际找到 {len(commands)} 个：{commands}"
    assert plugin_main.COMMAND_NAME == "任务列表", plugin_main.COMMAND_NAME
    # 别名、指令组、LLM 工具一律不许出现。
    for forbidden in ("alias=", "@filter.command_group", "@filter.llm_tool", "register_llm_tool"):
        assert forbidden not in source, f"插件里不应该出现 {forbidden}"


def test_collect_sorts_and_filters() -> None:
    """只保留启用且有下次执行时间的任务，并按时间升序。"""

    with tempfile.TemporaryDirectory() as tmp:
        data = asyncio.run(plugin_main.collect_tasks(FakeContext(_jobs()), max_tasks=10))
        assert data.total == 2, data.total
        assert [entry.name for entry in data.entries] == ["早上的任务", "晚上的任务"]
        assert data.unscheduled == 1
        assert "共 2 个未来任务" in data.summary


def test_live_next_run_wins_over_stale_db() -> None:
    """排程器里的时间比数据库新，必须优先用它。"""

    jobs = [FakeJob(job_id="j1", name="任务", next_run_time=_utc(2026, 1, 1, 0, 0))]
    live = {"j1": _utc(2026, 11, 23, 7, 30)}
    data = asyncio.run(
        plugin_main.collect_tasks(FakeContext(jobs, live), timezone_name="Asia/Shanghai")
    )
    # 2026-11-23 07:30 UTC 就是北京时间同一天的 15:30，周一。
    assert data.entries[0].next_run_text == "11-23 周一 15:30", data.entries[0].next_run_text


def test_text_mode_outputs_plain_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = _make_plugin(FakeContext(_jobs()), {"output_mode": "text"})
        _patch_data_dir(plugin, tmp)
        results = asyncio.run(_run_command(plugin, FakeEvent()))
        assert len(results) == 1
        kind, payload = results[0]
        assert kind == "text"
        assert "未来任务列表" in payload
        assert "早上的任务" in payload


def test_image_mode_produces_png_and_reuses_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = _make_plugin(FakeContext(_jobs()), {"output_mode": "image"})
        _patch_data_dir(plugin, tmp)
        if not plugin._render_available:
            print("   （跳过图片用例：本机没有 Pillow 或中文字体）")
            return

        first = asyncio.run(_run_command(plugin, FakeEvent()))
        assert first[0][0] == "image", first
        image_path = Path(first[0][1])
        assert image_path.is_file()
        assert image_path.read_bytes().startswith(b"\x89PNG")
        first_mtime = image_path.stat().st_mtime_ns

        second = asyncio.run(_run_command(plugin, FakeEvent()))
        assert second[0][0] == "image"
        assert Path(second[0][1]) == image_path, "内容没变必须复用同一张图"
        assert image_path.stat().st_mtime_ns == first_mtime, "复用不应重写文件"

        cached_files = list((Path(tmp) / "image_cache").glob("*.png"))
        assert len(cached_files) == 1


def test_cache_invalidated_when_content_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = _make_plugin(FakeContext(_jobs()), {"output_mode": "image"})
        _patch_data_dir(plugin, tmp)
        if not plugin._render_available:
            return

        first = asyncio.run(_run_command(plugin, FakeEvent()))
        first_path = Path(first[0][1])

        changed = _jobs()
        changed[1].name = "改过名字的任务"
        plugin.context = FakeContext(changed)
        second = asyncio.run(_run_command(plugin, FakeEvent()))
        second_path = Path(second[0][1])
        assert second_path != first_path, "内容变了必须重新出图"
        assert second_path.is_file()


def test_unwritable_cache_still_sends_image() -> None:
    """缓存目录写不进去时，仍然要把图片发出去，而不是退化成文字。"""

    with tempfile.TemporaryDirectory() as tmp:
        plugin = _make_plugin(FakeContext(_jobs()), {"output_mode": "image"})
        _patch_data_dir(plugin, tmp)
        if not plugin._render_available:
            return

        # 让 store 恒失败，模拟磁盘满 / 只读挂载。
        plugin.image_cache.store = lambda *_args, **_kwargs: None

        results = asyncio.run(_run_command(plugin, FakeEvent()))
        assert results[0][0] == "image", results
        path = Path(results[0][1])
        assert path.is_file()
        assert path.read_bytes().startswith(b"\x89PNG")


def test_render_failure_falls_back_to_text() -> None:
    """渲染炸了也要给用户文字，而不是静默失败。"""

    with tempfile.TemporaryDirectory() as tmp:
        plugin = _make_plugin(FakeContext(_jobs()), {"output_mode": "image"})
        _patch_data_dir(plugin, tmp)
        plugin._render_available = True

        def boom(_data, _options):
            raise RuntimeError("模拟 Pillow 崩溃")

        # 只在实例上打补丁：改类属性会污染后面的用例。
        plugin._render = boom
        results = asyncio.run(_run_command(plugin, FakeEvent()))

        assert results[0][0] == "text", results
        assert "图片渲染失败" in results[0][1]
        assert "早上的任务" in results[0][1]


def test_empty_list_still_replies() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = _make_plugin(FakeContext([]), {"output_mode": "text"})
        _patch_data_dir(plugin, tmp)
        results = asyncio.run(_run_command(plugin, FakeEvent()))
        assert "暂无未来任务" in results[0][1]


def test_db_failure_is_reported_not_raised() -> None:
    class BrokenDB:
        async def list_cron_jobs(self, job_type=None):
            raise RuntimeError("数据库锁住了")

    class BrokenContext:
        db = BrokenDB()
        cron_manager = FakeCronManager({})

        def get_db(self):
            return self.db

    data = asyncio.run(plugin_main.collect_tasks(BrokenContext()))
    assert "读取定时任务失败" in data.notice
    assert data.empty


def test_only_current_session_filters() -> None:
    jobs = _jobs()
    data = asyncio.run(
        plugin_main.collect_tasks(
            FakeContext(jobs), umo="aiocqhttp:GroupMessage:123", only_current_session=True
        )
    )
    assert data.total == 2, data.total
    data_other = asyncio.run(
        plugin_main.collect_tasks(
            FakeContext(jobs), umo="aiocqhttp:GroupMessage:999", only_current_session=True
        )
    )
    assert data_other.total == 0


def test_all_tasks_shown_without_truncation() -> None:
    """任务永远全量显示；传了 max_tasks 也不许截断。"""

    jobs = [
        FakeJob(job_id=f"j{i}", name=f"任务{i}", cron_expression="0 9 * * *",
                next_run_time=_utc(2026, 11, 23, i // 2, (i % 2) * 30))
        for i in range(40)
    ]
    data = asyncio.run(plugin_main.collect_tasks(FakeContext(jobs), max_tasks=5))
    assert len(data.entries) == 40, len(data.entries)
    assert data.hidden == 0
    # 汇总里不能出现「仅显示前 N 个」这种截断提示。
    assert "仅显示前" not in data.summary


def test_admin_only_blocks_normal_user() -> None:
    """admin_only 默认开启：普通用户被拦，管理员正常。"""

    with tempfile.TemporaryDirectory() as tmp:
        plugin = _make_plugin(FakeContext(_jobs()), {"output_mode": "text"})
        _patch_data_dir(plugin, tmp)
        # config 未显式配置时也应取默认 True —— 用空配置验证
        default_plugin = _make_plugin(FakeContext(_jobs()), {"output_mode": "text"})
        _patch_data_dir(default_plugin, tmp)
        results = asyncio.run(_run_command(default_plugin, FakeEvent(admin=False)))
        assert "仅管理员可用" in results[0][1]

        results_admin = asyncio.run(_run_command(default_plugin, FakeEvent(admin=True)))
        assert "未来任务列表" in results_admin[0][1]

        # 显式关闭后普通用户可用
        open_plugin = _make_plugin(FakeContext(_jobs()), {"admin_only": False, "output_mode": "text"})
        _patch_data_dir(open_plugin, tmp)
        results_open = asyncio.run(_run_command(open_plugin, FakeEvent(admin=False)))
        assert "未来任务列表" in results_open[0][1]


def _patch_data_dir(plugin: Any, tmp: str) -> None:
    """把缓存目录指到临时目录，避免测试污染真实插件数据目录。"""

    from astrbot_plugin_task_list.cache import ImageCache

    plugin.image_cache = ImageCache(
        Path(tmp) / "image_cache",
        enabled=plugin.config.get("image_cache", True) is not False,
        max_bytes=8 * 1024 * 1024,
    )


def _run_all() -> int:
    failures = 0
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for name, func in tests:
        try:
            func()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
