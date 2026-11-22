"""采集 AstrBot 的「未来任务」并转换成渲染层能直接用的结构。

AstrBot 的定时任务由 CronJobManager 统一管理，任务定义落在数据库 cron_jobs 表里，
每行是一个 astrbot.core.db.po.CronJob。所谓「未来任务」就是：已启用、且还有下次执行
时间的任务——一次性任务跑完会自己删掉，所以只要 enabled 且有 next_run_time 就说明它还没到点。

这里刻意只读数据库这一条路径，不去碰 APScheduler 的 Job 对象：
数据库是插件能稳定拿到的公开数据，而 APScheduler 的内部对象在不同版本里字段可能变。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger

from .models import TaskEntry, TaskListData
from .schedule import (
    annotate_timezone,
    describe_schedule,
    detect_source,
    short_session,
    source_label,
)

LOG_PREFIX = "[任务列表]"

# 备注/错误信息在卡片里放太长会把图片撑爆，这里统一截断。
DESCRIPTION_LIMIT = 60
ERROR_LIMIT = 80


def _resolve_timezone(name: str | None) -> tuple[Any, str]:
    """把配置里的时区名解析成 tzinfo。

    Returns:
        (tzinfo, 展示名)。解析失败时退回本机时区并返回空展示名，
        因为给用户看一个解析不了的时区名只会让人困惑。
    """

    text = (name or "").strip()
    if not text or text.lower() in {"local", "system", "default"}:
        return datetime.now().astimezone().tzinfo, ""
    try:
        return ZoneInfo(text), text
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(f"{LOG_PREFIX} 时区 {text} 无法识别，退回系统本地时区")
        return datetime.now().astimezone().tzinfo, ""


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """把数据库里的时间字段补上 UTC 时区。

    CronJob.next_run_time 用 DateTime(timezone=False) 存，写入时已经 astimezone(utc)，
    所以读出来是「没有时区标记的 UTC 时间」。直接当本地时间用会差好几个小时。
    """

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_next_run(value: datetime | None, tzinfo: Any) -> str:
    """把下次执行时间格式化成「11-23 周日 09:00」。"""

    if value is None:
        return ""
    try:
        local = value.astimezone(tzinfo)
    except (ValueError, OverflowError):
        local = value
    # datetime.weekday() 里 0 是周一，所以取值表是「一二三四五六日」，
    # 外层的「周」只加一次，否则会拼出「周周」。
    weekday = "一二三四五六日"[local.weekday()]
    return f"{local:%m-%d} 周{weekday} {local:%H:%M}"


def _truncate(text: str, limit: int) -> str:
    """按字符数截断，超长时补省略号。"""

    raw = " ".join((text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[: max(1, limit - 1)] + "…"


def _payload_of(job: Any) -> dict:
    """安全地取出 job.payload，保证返回 dict。"""

    payload = getattr(job, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _session_of(job: Any) -> str:
    """取出任务所属会话。只有 active_agent 任务会带 session。"""

    return str(_payload_of(job).get("session") or "").strip()


def _sync_next_run_from_scheduler(context: Any, job_id: str) -> datetime | None:
    """尽量从 APScheduler 拿实时下次执行时间。

    数据库里的 next_run_time 是异步回写的，刚创建/刚改过的任务可能还是旧值或 None，
    而 APScheduler 里的值一定是最新的。拿不到就返回 None，由调用方退回数据库值。
    """

    cron_manager = getattr(context, "cron_manager", None)
    getter = getattr(cron_manager, "get_next_run_time", None)
    if not callable(getter):
        return None
    try:
        return getter(job_id)
    except Exception:  # noqa: BLE001 - 排程器内部状态异常不该拖垮整个列表
        return None


def _build_entry(
    job: Any,
    *,
    tzinfo: Any,
    show_session: bool,
    show_description: bool,
    live_next_run: datetime | None,
    display_tz: str = "",
) -> TaskEntry:
    """把一个 CronJob 行转换成 TaskEntry。"""

    next_run = _as_aware_utc(live_next_run or getattr(job, "next_run_time", None))
    description = _truncate(str(getattr(job, "description", "") or ""), DESCRIPTION_LIMIT)
    error_text = _truncate(str(getattr(job, "last_error", "") or ""), ERROR_LIMIT)
    session = _session_of(job)

    source = detect_source(job)
    return TaskEntry(
        name=str(getattr(job, "name", "") or "未命名任务"),
        source=source,
        type_label=source_label(source),
        schedule_text=annotate_timezone(describe_schedule(job), job, display_tz),
        next_run_text=_format_next_run(next_run, tzinfo),
        session_text=short_session(session) if show_session else "",
        description=description if show_description else "",
        error_text=error_text,
        run_once=bool(getattr(job, "run_once", False)),
        job_id=str(getattr(job, "job_id", "") or ""),
    )


def _matches_session(job: Any, umo: str) -> bool:
    """判断任务是否属于指定会话。

    只有 active_agent 任务的 payload 里带 session；basic 任务没有归属会话，
    这类任务在「只看当前会话」模式下按「不属于」处理，避免混进来。
    """

    if not umo:
        return False
    return _session_of(job) == umo


async def collect_tasks(
    context: Any,
    *,
    umo: str = "",
    timezone_name: str | None = None,
    max_tasks: int = 30,
    only_current_session: bool = False,
    show_session: bool = False,
    show_description: bool = True,
) -> TaskListData:
    """读取 AstrBot 的全部未来任务。

    Args:
        context: 插件拿到的 Context，用来访问数据库与排程器。
        umo: 当前会话的 unified_msg_origin，only_current_session 时用来过滤。
        timezone_name: 展示用的时区名，空表示跟随系统本地时区。
        max_tasks: 已废弃。历史原因保留，现在总是显示全部任务。
        only_current_session: 是否只列出属于 umo 的任务。
        show_session: 是否在结果里带上会话信息。
        show_description: 是否在结果里带上任务备注。

    Returns:
        TaskListData。任何读取异常都会被吞掉并写进 notice，
        这样插件永远能给出一个可用的结果，而不是直接报错不回复。
    """

    tzinfo, tz_label = _resolve_timezone(timezone_name)
    data = TaskListData(timezone_label=tz_label, title=data_title())

    db = getattr(context, "get_db", None)
    db = db() if callable(db) else getattr(context, "db", None)
    list_jobs = getattr(db, "list_cron_jobs", None)
    if not callable(list_jobs):
        data.notice = "当前 AstrBot 版本没有可读取的定时任务接口"
        logger.warning(f"{LOG_PREFIX} context 上找不到 list_cron_jobs，无法读取任务")
        return data

    try:
        jobs = await list_jobs()
    except Exception as exc:  # noqa: BLE001 - 数据库异常不该让用户收不到回复
        data.notice = f"读取定时任务失败：{exc}"
        logger.exception(f"{LOG_PREFIX} 读取定时任务失败")
        return data

    candidates: list[tuple[datetime, TaskEntry]] = []
    unscheduled = 0
    for job in jobs or []:
        if not bool(getattr(job, "enabled", True)):
            continue
        if only_current_session and not _matches_session(job, umo):
            continue

        job_id = str(getattr(job, "job_id", "") or "")
        live_next = _sync_next_run_from_scheduler(context, job_id) if job_id else None
        next_run = _as_aware_utc(live_next or getattr(job, "next_run_time", None))
        if next_run is None:
            # 已启用但排不进去，多半是刚重启还没 sync，或 cron 表达式本身有问题。
            unscheduled += 1
            continue

        entry = _build_entry(
            job,
            tzinfo=tzinfo,
            show_session=show_session,
            show_description=show_description,
            live_next_run=next_run,
            display_tz=tz_label,
        )
        candidates.append((next_run, entry))

    candidates.sort(key=lambda item: item[0])
    data.total = len(candidates)
    data.unscheduled = unscheduled

    # 一次性把所有任务都列出来，不再截断——「另有 N 个未显示」这种提示
    # 对用户没有价值，任务数量多就该全看到。保留 max_tasks 参数只为兼容签名。
    data.entries = [entry for _next_run, entry in candidates]
    data.hidden = 0
    return data


def data_title() -> str:
    """标题单独抽出来，方便以后做多语言。"""

    from .models import TITLE

    return TITLE
