"""任务列表插件的数据模型。

这个模块只放纯数据结构和展示用的派生属性，既不依赖 AstrBot，也不依赖 Pillow，
方便脱离框架单独测试。采集层（tasks.py）负责把 AstrBot 的任务对象转换成这里的
结构，渲染层（render.py）只读这里的字段，两边互不耦合。
"""

from __future__ import annotations

from dataclasses import dataclass, field

TITLE = "未来任务列表"
"""图片与文字输出共用的标题。"""

STYLE_VERSION = "2"
"""渲染样式版本。改动配色/排版后递增，可以让旧缓存图片自动失效。"""

# 任务来源 -> 展示用的角标文字。
#
# 这几个值不是拍脑袋定的，对应 AstrBot 里能确凿区分出来的三种创建途径：
#   * basic        到点执行 Python 回调，handler 必须注册在内存里，只有插件代码做得到；
#   * origin=tool  AI 通过内置 future_task 工具建的任务；
#   * origin=api   WebUI 面板里手动填表建的任务。
# 这三条是 AstrBot 自己的创建路径，都有确定标识。剩下没标识的只可能来自插件直接调
# CronJobManager.add_active_job(payload=...) 自建（payload 由调用方自定义，可以不带 origin），
# 来源必然是「插件」，只是不知道是哪个插件，所以标「插件自建」。
SOURCE_LABELS: dict[str, str] = {
    "ai": "AI 自建",
    "plugin": "插件内置",
    "user": "用户自建",
    "unknown": "插件自建",
}

SECTION_ONE_TIME = "一次性提醒"
SECTION_RECURRING = "循环提醒"


@dataclass(slots=True)
class TaskEntry:
    """一个未来任务需要的全部展示信息。

    时间、重复规则这类需要时区换算的字段，已经由采集层格式化成字符串，
    渲染层不再做任何时间计算，避免两边算出不一样的结果。
    """

    name: str
    """任务名。"""

    source: str = "unknown"
    """任务来源标识：ai / plugin / user / unknown。"""

    type_label: str = "未知来源"
    """来源角标文字，由 SOURCE_LABELS 映射得到。"""

    schedule_text: str = ""
    """重复规则的中文描述，例如「每天 09:00」「仅一次」。"""

    next_run_text: str = ""
    """下次执行时间的展示文本，例如「11-23 周一 09:00」。"""

    session_text: str = ""
    """任务所属会话（umo），未启用展示时为空。"""

    description: str = ""
    """任务备注，可能为空。"""

    error_text: str = ""
    """最近一次执行的错误信息，没有失败时为空。"""

    run_once: bool = False
    """是否为一次性任务，决定它落在「一次性提醒」还是「循环提醒」分区。"""

    job_id: str = ""
    """AstrBot 内部的任务 ID，仅用于排查问题。"""


@dataclass(slots=True)
class TaskListData:
    """一次查询得到的完整任务列表。"""

    entries: list[TaskEntry] = field(default_factory=list)
    title: str = TITLE
    total: int = 0
    """未来任务总数（可能大于 entries 的长度）。"""

    hidden: int = 0
    """因为数量上限被隐藏的任务数。"""

    unscheduled: int = 0
    """已启用但当前没有下次运行时间的任务数（例如刚重启还没排程）。"""

    timezone_label: str = ""
    """展示用的时区名，例如「Asia/Shanghai」；为空表示跟随系统本地时间。"""

    notice: str = ""
    """采集过程中的提示信息（例如读取任务失败的原因），可能为空。"""

    @property
    def empty(self) -> bool:
        """是否没有任何可展示的未来任务。"""

        return not self.entries

    @property
    def one_time_entries(self) -> list[TaskEntry]:
        """一次性任务。跑完就没了，最怕错过，所以排在前面。"""

        return [entry for entry in self.entries if entry.run_once]

    @property
    def recurring_entries(self) -> list[TaskEntry]:
        """循环任务。"""

        return [entry for entry in self.entries if not entry.run_once]

    @property
    def summary(self) -> str:
        """标题下方的一句话汇总，图片与文字输出共用同一份措辞。"""

        if self.total <= 0:
            parts = ["暂无未来任务"]
        else:
            parts = [f"共 {self.total} 个未来任务"]
            shown = len(self.entries)
            if self.hidden > 0 and shown:
                parts.append(f"仅显示前 {shown} 个")
        if self.unscheduled > 0:
            parts.append(f"{self.unscheduled} 个任务已启用但暂未排程")
        return " · ".join(parts)


@dataclass(slots=True)
class RenderOptions:
    """渲染开关。由插件配置决定，同时参与缓存指纹计算。"""

    scale: int = 2
    """超采样倍数：先按 scale 倍尺寸画，再用 LANCZOS 缩回，文字边缘更干净。"""

    show_session: bool = False
    """是否在任务卡片里显示所属会话。"""

    show_description: bool = True
    """是否显示任务备注。"""

    show_error: bool = True
    """是否显示最近一次执行的错误信息。"""
