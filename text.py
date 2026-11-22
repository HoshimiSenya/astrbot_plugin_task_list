"""图片不可用时的文字输出。

文字版本和图片版本共用同一份 TaskListData，分区也保持一致：
一次性任务在前、循环任务在后，这样两种输出模式下用户看到的结构是同一个。
"""

from __future__ import annotations

from .models import TaskEntry, TaskListData


def _format_entry(index: int, entry: TaskEntry) -> list[str]:
    """把一个任务渲染成若干行。"""

    head = f"{index}. {entry.name}"
    if entry.type_label:
        head += f"［{entry.type_label}］"
    lines = [head]

    when_parts: list[str] = []
    if entry.next_run_text:
        when_parts.append(entry.next_run_text)
    if entry.schedule_text:
        when_parts.append(entry.schedule_text)
    if when_parts:
        lines.append("   " + " · ".join(when_parts))

    if entry.session_text:
        lines.append(f"   会话：{entry.session_text}")
    if entry.description:
        lines.append(f"   备注：{entry.description}")
    if entry.error_text:
        lines.append(f"   上次失败：{entry.error_text}")
    return lines


def render_text(data: TaskListData, *, fallback: bool = False) -> str:
    """把任务列表渲染成纯文本。

    Args:
        data: 采集好的任务列表。
        fallback: 是否因为图片渲染失败而回退。回退时首行加一句说明，
            避免用户以为插件本来就只有纯文字。

    Returns:
        可直接作为纯文本消息发送的字符串。
    """

    lines: list[str] = []
    if fallback:
        lines.append("（图片渲染失败，已回退为文字输出）")
    lines.append(data.title)
    lines.append(data.summary)
    if data.timezone_label:
        lines.append(f"时区：{data.timezone_label}")
    lines.append("")

    if data.empty:
        lines.append("现在没有待执行的未来任务。")
    else:
        # 和图片模式一样分两段；空了的那段直接不出现，不留一个光秃秃的标题。
        sections: list[tuple[str, list[TaskEntry]]] = []
        one_time = data.one_time_entries
        recurring = data.recurring_entries
        if one_time:
            sections.append(("一次性提醒", one_time))
        if recurring:
            sections.append(("循环提醒", recurring))

        for title, entries in sections:
            lines.append(f"【{title}】{len(entries)} 个")
            for index, entry in enumerate(entries, start=1):
                lines.extend(_format_entry(index, entry))
                lines.append("")

    if data.notice:
        lines.append(f"提示：{data.notice}")
    return "\n".join(lines).rstrip()
