"""纯逻辑单元测试：排程描述、文本渲染、缓存指纹与配置 schema。

不依赖 AstrBot，可直接 `python tests/test_task_list.py` 运行。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# 允许直接 python tests/test_task_list.py 运行。
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from astrbot_plugin_task_list.cache import ImageCache  # noqa: E402
from astrbot_plugin_task_list.models import (  # noqa: E402
    RenderOptions,
    TaskEntry,
    TaskListData,
)
from astrbot_plugin_task_list.schedule import (  # noqa: E402
    describe_cron,
    describe_schedule,
    detect_source,
    short_session,
    source_label,
)
from astrbot_plugin_task_list.text import render_text  # noqa: E402


class FakeJob:
    """够用的 CronJob 替身：采集层只读这几个字段。"""

    def __init__(self, **kwargs) -> None:
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


def test_describe_daily_cron() -> None:
    assert describe_cron("0 9 * * *") == "每天 09:00"
    assert describe_cron("30 20 * * *") == "每天 20:30"


def test_describe_weekly_cron() -> None:
    # AstrBot 会把数字星期展开成英文缩写，两种写法都要认。
    assert describe_cron("0 20 * * 1,3") == "每周一、周三 20:00"
    assert describe_cron("0 20 * * mon,wed") == "0 20 * * mon,wed"


def test_describe_monthly_cron() -> None:
    assert describe_cron("0 8 1 * *") == "每月 1 号 08:00"


def test_describe_unknown_cron_passthrough() -> None:
    # 看不懂的表达式原样返回，不能编造描述。
    assert describe_cron("*/5 * * * *") == "*/5 * * * *"
    assert describe_cron("") == "未设置时间"


def test_describe_schedule_run_once() -> None:
    job = FakeJob(run_once=True, payload={"run_at": "2026-11-23T09:00:00+08:00"})
    text = describe_schedule(job)
    assert "仅一次" in text
    # 原始 ISO 太丑，要能转成人类读得懂的时间；解析不了时才原样保留。
    assert "11 月 23 日 09:00" in text, text


def test_describe_run_at_unparsable_passthrough() -> None:
    job = FakeJob(run_once=True, payload={"run_at": "不是时间"})
    assert "不是时间" in describe_schedule(job)


def test_annotate_timezone_only_when_different() -> None:
    from astrbot_plugin_task_list.schedule import annotate_timezone

    # 展示时区和任务时区一致，不标注，免得画面啰嗦。
    same = FakeJob(timezone="Asia/Shanghai")
    assert annotate_timezone("每天 09:00", same, "Asia/Shanghai") == "每天 09:00"
    # 任务没写时区 = 跟随系统，同样不标注。
    naive = FakeJob(timezone=None)
    assert annotate_timezone("每天 09:00", naive, "Asia/Shanghai") == "每天 09:00"
    # 不一致时必须标注，否则「下次执行」和「每天几点」会互相矛盾。
    diff = FakeJob(timezone="UTC")
    assert annotate_timezone("每天 01:00", diff, "Asia/Shanghai") == (
        "每天 01:00（任务时区 UTC）"
    )


def test_short_session_truncates() -> None:
    assert short_session("") == ""
    assert short_session("abc") == "abc"
    long_umo = "aiocqhttp:GroupMessage:" + "9" * 100
    assert len(short_session(long_umo)) <= 48
    assert short_session(long_umo).endswith("…")


def _mixed_data() -> TaskListData:
    """一次性 + 循环混合，用来验证分区顺序。"""

    return TaskListData(
        entries=[
            TaskEntry(
                name="明早叫我起床",
                source="ai",
                type_label="AI 自建",
                schedule_text="仅一次 · 11 月 23 日 07:30",
                next_run_text="11-23 周一 07:30",
                run_once=True,
            ),
            TaskEntry(
                name="每日早报",
                source="user",
                type_label="用户自建",
                schedule_text="每天 09:00",
                next_run_text="11-23 周一 09:00",
            ),
            TaskEntry(
                name="提醒我开会",
                source="user",
                type_label="用户自建",
                schedule_text="仅一次 · 11 月 24 日 20:00",
                next_run_text="11-24 周一 20:00",
                run_once=True,
            ),
        ]
    )


def _sample_data() -> TaskListData:
    return TaskListData(
        title="未来任务列表",
        total=2,
        entries=[
            TaskEntry(
                name="早报推送",
                source="ai",
                type_label="AI 自建",
                schedule_text="每天 09:00",
                next_run_text="11-23 周一 09:00",
                description="推送到主群",
            ),
            TaskEntry(
                name="一次性提醒（跑完就删）",
                source="user",
                type_label="用户自建",
                schedule_text="仅一次",
                next_run_text="11-24 周一 20:00",
                run_once=True,
                error_text="上次发送超时",
            ),
        ],
    )


def test_detect_source_basic_is_plugin() -> None:
    """job_type=basic 的任务只有插件能注册（要内存里的回调），必须判成插件。"""
    assert detect_source(FakeJob(job_type="basic", payload={})) == "plugin"
    assert detect_source(FakeJob(job_type="basic", payload={"origin": "api"})) == "plugin"


def test_detect_source_active_agent_uses_origin() -> None:
    """active_agent 任务靠 payload.origin 区分：tool=AI，api=用户。"""
    assert detect_source(FakeJob(payload={"origin": "tool"})) == "ai"
    assert detect_source(FakeJob(payload={"origin": "api"})) == "user"


def test_detect_source_missing_origin_is_unknown() -> None:
    """没有 origin 的只可能是插件自建任务，反推不出来源，标未知。"""
    assert detect_source(FakeJob(payload={})) == "unknown"
    assert detect_source(FakeJob(payload={"origin": "   "})) == "unknown"
    assert detect_source(FakeJob(payload=None)) == "unknown"


def test_source_labels() -> None:
    assert source_label("ai") == "AI 自建"
    assert source_label("plugin") == "插件内置"
    assert source_label("user") == "用户自建"
    # 没有 origin 的任务来源必然是插件，只是不知道是哪个插件，所以叫「插件自建」。
    assert source_label("unknown") == "插件自建"
    # 不认识的标识不能崩，退回同一个兜底标签。
    assert source_label("whatever") == "插件自建"


def test_sections_split_one_time_and_recurring() -> None:
    data = _sample_data()
    assert [e.name for e in data.one_time_entries] == ["一次性提醒（跑完就删）"]
    assert [e.name for e in data.recurring_entries] == ["早报推送"]


def test_text_groups_by_section() -> None:
    out = render_text(_mixed_data())
    assert "【一次性提醒】2 个" in out
    assert "【循环提醒】1 个" in out
    # 一次性必须排在循环前面，跑完就没的任务最容易被错过。
    assert out.index("一次性提醒") < out.index("循环提醒")


def test_text_omits_empty_section() -> None:
    """只有循环任务时不该出现空的「一次性提醒」标题。"""
    out = render_text(_sample_data())
    assert "【一次性提醒】" in out
    out2 = render_text(TaskListData(
        entries=[TaskEntry(name="只有循环", schedule_text="每天 08:00")]
    ))
    assert "【一次性提醒】" not in out2
    assert "【循环提醒】" in out2


def test_render_text_contains_entries() -> None:
    out = render_text(_sample_data())
    assert "未来任务列表" in out
    assert "共 2 个未来任务" in out
    assert "1. 早报推送" in out
    assert "每天 09:00" in out
    assert "上次发送超时" in out
    # 图片页脚那类「xx 生成」绝对不能出现在文本里。
    assert "生成" not in out


def test_render_text_fallback_banner() -> None:
    out = render_text(_sample_data(), fallback=True)
    assert out.startswith("（图片渲染失败")


def test_render_text_empty() -> None:
    out = render_text(TaskListData())
    assert "暂无未来任务" in out
    assert "现在没有待执行的未来任务。" in out


def test_cache_fingerprint_stable_and_sensitive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = ImageCache(tmp)
        data = _sample_data()
        options = RenderOptions()

        first = cache.fingerprint(data, options)
        second = cache.fingerprint(data, options)
        assert first == second, "同样内容必须得到同样指纹，否则缓存永远命中不了"

        data.entries[0].name = "改名了"
        assert cache.fingerprint(data, options) != first, "内容变了指纹必须变"

        data2 = _sample_data()
        assert cache.fingerprint(data2, RenderOptions(show_session=True)) != first


def test_cache_store_and_lookup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = ImageCache(tmp, enabled=True)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        path = cache.store("abc123", png)
        assert path is not None and path.is_file()
        assert cache.lookup("abc123") == path
        assert cache.lookup("missing") is None


def test_cache_disabled_never_hits() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = ImageCache(tmp, enabled=False)
        assert cache.store("abc", b"\x89PNG") is None
        assert cache.lookup("abc") is None


def test_cache_ttl_expires() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = ImageCache(tmp, enabled=True, ttl_hours=0.0001)
        path = cache.store("old", b"\x89PNG" + b"1" * 32)
        assert path is not None
        import os
        import time

        old = time.time() - 10
        os.utime(path, (old, old))
        assert cache.lookup("old") is None, "过期图片必须被判为未命中"


def test_config_schema_is_valid_json() -> None:
    schema_path = PLUGIN_DIR / "_conf_schema.json"
    raw = json.loads(schema_path.read_text(encoding="utf-8"))
    assert raw["output_mode"]["default"] == "image", "默认必须是图片模式"
    assert raw["output_mode"]["options"] == ["image", "text"]
    assert raw["image_cache"]["type"] == "bool"
    assert raw["image_cache"]["default"] is True
    assert raw["admin_only"]["default"] is True, "admin_only 默认应为开启"


def test_metadata_yaml() -> None:
    text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    assert "name: astrbot_plugin_task_list" in text
    assert "display_name:" in text


def _run_all() -> int:
    """没有 pytest 时也能跑，直接顺序执行所有 test_* 函数。"""

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
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
