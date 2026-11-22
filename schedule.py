"""把 AstrBot 任务的重复规则翻译成人类看得懂的中文。

AstrBot 的 CronJob 有两种排程方式：

* 周期任务：cron_expression 是标准 5 段 crontab（分 时 日 月 周）；
* 一次性任务：run_once=True，真正的执行时刻存在 payload["run_at"]，
  老数据可能退化成直接放在 cron_expression 里的 ISO 时间。

这里只做「描述」，不参与排序和计算下次时间——那些交给 APScheduler 与 tasks.py，
免得两边算法不一致，出现「写的是 9 点、实际 10 点跑」这种自相矛盾。
"""

from __future__ import annotations

from typing import Any

# 星期编号沿用 cron 习惯：0 和 7 都是周日。
_WEEKDAY_NAMES: dict[int, str] = {
    0: "周日",
    7: "周日",
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
}

_RANGE_SEP = "-"
_STEP_SEP = "/"
_LIST_SEP = ","


def _expand_field(field: str) -> tuple[list[int] | None, int, bool]:
    """把一个 cron 字段拆成 (取值集合, 步长, 是否通配)。

    返回值的第一项为 None 表示无法解析（例如 MON-FRI 这种英文星期写法），
    调用方会退回直接展示原始表达式，而不是硬编一个可能出错的描述。
    """

    raw = (field or "").strip().lower()
    if not raw:
        return None, 1, False
    if raw == "*":
        return None, 1, True

    values: list[int] = []
    step = 1
    for part in raw.split(_LIST_SEP):
        part = part.strip()
        if not part:
            continue
        if _STEP_SEP in part:
            head, _, step_text = part.partition(_STEP_SEP)
            try:
                step = int(step_text)
            except ValueError:
                return None, 1, False
        else:
            head = part
        if head in ("*", ""):
            return None, step, True
        if _RANGE_SEP in head:
            start_text, _, end_text = head.partition(_RANGE_SEP)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                return None, 1, False
            if start > end:
                return None, 1, False
            values.extend(range(start, end + 1, max(1, step)))
            continue
        try:
            values.append(int(head))
        except ValueError:
            return None, 1, False
    return (sorted(set(values)) or None), step, False


def _format_time_of_day(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _describe_daily(hour: str, minute: str, step_hour: int) -> str:
    """每天类规则的描述。"""

    hours, _h_step, h_star = _expand_field(hour)
    minutes, _m_step, m_star = _expand_field(minute)
    if h_star or not hours or m_star or not minutes:
        return ""
    if step_hour > 1:
        return f"每 {step_hour} 小时的 {minutes[0]:02d} 分"
    if len(hours) == 1 and len(minutes) == 1:
        return f"每天 {_format_time_of_day(hours[0], minutes[0])}"
    if len(minutes) == 1:
        listed = "、".join(f"{value:02d}" for value in hours)
        return f"每天 {listed} 点的 {minutes[0]:02d} 分"
    listed = "、".join(_format_time_of_day(h, m) for h in hours for m in minutes)
    return f"每天 {listed}"


def _describe_weekly(day_of_week: str, hour: str, minute: str) -> str:
    """每周类规则的描述。"""

    days, _d_step, d_star = _expand_field(day_of_week)
    hours, _h_step, h_star = _expand_field(hour)
    minutes, _m_step, m_star = _expand_field(minute)
    if d_star or not days or h_star or not hours or m_star or not minutes:
        return ""
    names = [_WEEKDAY_NAMES.get(day, "") for day in days]
    if not all(names):
        return ""
    if len(hours) == 1 and len(minutes) == 1:
        return f"每{'、'.join(names)} {_format_time_of_day(hours[0], minutes[0])}"
    return f"每{'、'.join(names)}"


def _describe_monthly(day: str, hour: str, minute: str) -> str:
    """每月类规则的描述。"""

    days, _d_step, d_star = _expand_field(day)
    hours, _h_step, h_star = _expand_field(hour)
    minutes, _m_step, m_star = _expand_field(minute)
    if d_star or not days or h_star or not hours or m_star or not minutes:
        return ""
    if len(hours) == 1 and len(minutes) == 1:
        listed = "、".join(f"{value} 号" for value in days)
        return f"每月 {listed} {_format_time_of_day(hours[0], minutes[0])}"
    return "每月 " + "、".join(f"{value} 号" for value in days)


def describe_cron(expression: str | None) -> str:
    """把 5 段 crontab 表达式翻译成中文；看不懂时原样返回。"""

    raw = (expression or "").strip()
    if not raw:
        return "未设置时间"
    parts = raw.split()
    if len(parts) != 5:
        return raw
    minute, hour, day, month, day_of_week = parts

    _hours, hour_step, _h_star = _expand_field(hour)
    if day == "*" and month == "*" and day_of_week == "*":
        daily = _describe_daily(hour, minute, hour_step)
        if daily:
            return daily
    if day == "*" and month == "*" and day_of_week != "*":
        weekly = _describe_weekly(day_of_week, hour, minute)
        if weekly:
            return weekly
    if day != "*" and month == "*" and day_of_week == "*":
        monthly = _describe_monthly(day, hour, minute)
        if monthly:
            return monthly
    # 分钟级 / 更复杂的组合：保留原始表达式，至少不会误导。
    return raw


def _friendly_run_at(raw: str) -> str:
    """把 ISO 时间字符串收拾成「11 月 24 日 20:00」。

    原始 ISO（2026-11-24T20:00:00+08:00）直接印在图片里又长又难读，
    能解析就换成人类写法，解析不了就原样返回——不能因为格式不认识就把时间弄丢。
    """

    from datetime import datetime

    text = raw.strip()
    if not text:
        return ""
    candidate = text.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    return f"{moment.month} 月 {moment.day} 日 {moment:%H:%M}"


def describe_run_at(run_at: Any) -> str:
    """把一次性任务的时间点描述成中文。"""

    if run_at is None:
        return "仅一次"
    friendly = _friendly_run_at(str(run_at))
    return f"仅一次 · {friendly}" if friendly else "仅一次"


def annotate_timezone(schedule_text: str, job: Any, display_tz: str) -> str:
    """当任务自身的时区和展示时区不一致时，给重复规则补一句时区标注。

    cron 表达式是按任务自己的时区解释的，而卡片上的「下次执行」是按展示时区换算的。
    两者不同时，不标注就会出现「下次执行 09:00 · 每天 01:00」这种看起来自相矛盾的组合，
    用户会以为插件算错了。
    """

    job_tz = str(getattr(job, "timezone", "") or "").strip()
    if not job_tz or not schedule_text:
        return schedule_text
    if display_tz and job_tz == display_tz:
        return schedule_text
    return f"{schedule_text}（任务时区 {job_tz}）"


def describe_schedule(job: Any) -> str:
    """根据 AstrBot 的 CronJob 生成重复规则描述。

    Args:
        job: astrbot.core.cron 的 CronJob 对象；只用它的
            run_once / cron_expression / payload 三个字段，
            所以测试时传一个同名属性的假对象也能跑。

    Returns:
        一句中文描述，例如「每天 09:00」「每周一、周三 20:00」「仅一次」。
    """

    payload = getattr(job, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    run_once = bool(getattr(job, "run_once", False))
    expression = getattr(job, "cron_expression", None)

    if run_once:
        return describe_run_at(payload.get("run_at") or expression)
    return describe_cron(expression)


def detect_source(job: Any) -> str:
    """判断任务是谁建的，返回 ai / plugin / user / unknown。

    这几类在 AstrBot 里能确凿区分，判断依据全部来自公开字段，不靠猜：

    1. job_type == "basic" -> 插件内置（插件代码里静态注册的回调任务）。
       basic 任务到点要执行一个注册在内存里的 Python 回调（见 CronJobManager._run_basic_job），
       只有加载在进程里的插件代码能注册这种回调，外部无从创建。
    2. 否则看 payload 里的 origin（这类任务 job_type 都是 active_agent）：
       * "tool" -> AI 自建：内置的 future_task 工具在 payload 里写死 origin=tool；
       * "api"  -> 用户自建：WebUI 面板建任务时写死 origin=api。
    3. 其余情况 -> unknown（展示为「插件自建」）。

    origin 不是后加的字段：cron 功能 v4.14.0 一落地，上面两条官方创建路径就都写死了 origin
    （v4.14.0 的 cron_tools.py 写 origin="tool"，dashboard/routes/cron.py 写 origin="api"）。
    所以 AstrBot 自己建的任务永远能分清，不存在「升级前的老任务没有 origin」。

    真正会落到 unknown 的是第三条路：插件直接调 CronJobManager.add_active_job(payload=...)
    自建任务。这是个公开 API，payload 由调用方完全自定义，可以不带 origin。
    能确定的只有「来源是插件」，不知道具体是哪个插件，所以标「插件自建」。
    """

    if str(getattr(job, "job_type", "") or "") == "basic":
        return "plugin"

    payload = getattr(job, "payload", None)
    origin = ""
    if isinstance(payload, dict):
        origin = str(payload.get("origin") or "").strip().lower()

    if origin == "tool":
        return "ai"
    if origin == "api":
        return "user"
    return "unknown"


def source_label(source: str) -> str:
    """把来源标识翻成角标文字。"""

    from .models import SOURCE_LABELS

    return SOURCE_LABELS.get(source, SOURCE_LABELS["unknown"])


def short_session(umo: str, limit: int = 48) -> str:
    """把 umo 收拾得短一点，只用于展示。

    umo 形如 aiocqhttp:GroupMessage:123456，原样打印又长又难读，
    这里只做截断，不做任何猜测性改写——猜错会话比截断更糟。
    """

    text = (umo or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"
