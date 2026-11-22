"""AstrBot 任务列表插件：一条 /任务列表 命令，把未来定时任务画成图。

设计上有三条硬约束，都能在代码里找到对应实现：

1. 只注册 /任务列表 一个命令，不加别名、不加子指令、不加 LLM 工具，
   免得把别的东西塞进用户面前。
2. 图片优先：默认输出图片，配置里可以切成纯文字；渲染失败时自动回退文字，
   用户永远拿得到结果，不会因为 Pillow 出问题就石沉大海。
3. 内容没变就不重画：用任务列表数据 + 渲染开关 + 样式版本算内容指纹，
   指纹相同直接复用上次的 PNG，省掉一次完整渲染。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, AsyncGenerator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .cache import ImageCache
from .models import RenderOptions, TaskListData
from .tasks import collect_tasks
from .text import render_text

LOG_PREFIX = "[任务列表]"
PLUGIN_DISPLAY_NAME = "任务列表"

# 只有这一个命令。修改这里就等于增加命令，别改。
COMMAND_NAME = "任务列表"

DEFAULT_CACHE_MAX_MB = 32
DEFAULT_CACHE_TTL_HOURS = 168
DEFAULT_SCALE = 2


def _config_bool(config: Any, key: str, default: bool) -> bool:
    """读取布尔配置。AstrBot 的 WebUI 可能把值存成字符串，这里统一兜住。"""

    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
    return bool(default if value is None else value)


def _config_int(config: Any, key: str, default: int) -> int:
    """读取整数配置，非法值退回默认值。"""

    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_str(config: Any, key: str, default: str = "") -> str:
    """读取字符串配置，None 视为默认值。"""

    value = config.get(key, default)
    return default if value is None else str(value)


def _plugin_data_dir() -> Path:
    """插件自己的数据目录，用来缓存图片。

    优先用 AstrBot 官方的 plugin_data 目录（升级/重装插件不会丢），
    老版本没有这个工具函数时退回插件目录下的 data/。
    """

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_task_list"
    except Exception:  # noqa: BLE001 - 老版本 AstrBot 没有这个模块
        base = Path(__file__).resolve().parent / "data"
    return base / "image_cache"


class TaskListPlugin(Star):
    """把 AstrBot 的未来定时任务渲染成图片（或文字）发出去。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: Any = config or {}

        self.cache_max_mb = max(0, _config_int(self.config, "cache_max_mb", DEFAULT_CACHE_MAX_MB))
        self.cache_ttl_hours = max(0, _config_int(self.config, "cache_ttl_hours", DEFAULT_CACHE_TTL_HOURS))

        self.image_cache = ImageCache(
            _plugin_data_dir(),
            enabled=_config_bool(self.config, "image_cache", True),
            max_bytes=self.cache_max_mb * 1024 * 1024,
            ttl_hours=float(self.cache_ttl_hours),
        )

        # 渲染层依赖 Pillow 和字体。两者都可能在精简系统上缺失，
        # 这里先探一次，缺了就整场走文字，避免每次命令都抛异常刷日志。
        self._render_available = self._probe_render()
        if not self._render_available:
            logger.warning(f"{LOG_PREFIX} Pillow 或中文字体不可用，将以文字模式输出")

    # ────────────────────────── 命令 ──────────────────────────

    @filter.command(COMMAND_NAME)
    async def cmd_task_list(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, None]:
        """查看 AstrBot 的未来定时任务列表。"""

        event.stop_event()

        wants_image = _config_str(self.config, "output_mode", "image").strip().lower() != "text"
        if _config_bool(self.config, "admin_only", True) and not event.is_admin():
            yield event.plain_result("这个命令仅管理员可用。")
            return

        data = await self._collect(event)
        options = self._render_options()

        if wants_image and self._render_available:
            image_path = await self._render_cached(data, options)
            if image_path is not None:
                yield event.image_result(str(image_path))
                return
            logger.warning(f"{LOG_PREFIX} 图片渲染失败，本次改为文字输出")
            yield event.plain_result(render_text(data, fallback=True))
            return

        yield event.plain_result(render_text(data))

    # ────────────────────────── 采集 ──────────────────────────

    async def _collect(self, event: AstrMessageEvent) -> TaskListData:
        """采集任务；任何意外都转成一句提示，不让用户干等。"""

        try:
            return await collect_tasks(
                self.context,
                umo=str(getattr(event, "unified_msg_origin", "") or ""),
                timezone_name=_config_str(self.config, "timezone", "") or None,
                only_current_session=_config_bool(self.config, "only_current_session", False),
                show_session=_config_bool(self.config, "show_session", False),
                show_description=_config_bool(self.config, "show_description", True),
            )
        except Exception as exc:  # noqa: BLE001 - 采集失败也要给用户一个回复
            logger.exception(f"{LOG_PREFIX} 采集任务列表时发生未预期的错误")
            return TaskListData(title="未来任务列表", notice=f"读取任务失败：{exc}")

    def _render_options(self) -> RenderOptions:
        """把插件配置转成渲染开关。

        这些开关会参与缓存指纹，所以改了配置自然会重新出图，不会发出旧样式的图片。
        """

        return RenderOptions(
            scale=max(1, _config_int(self.config, "render_scale", DEFAULT_SCALE)),
            show_session=_config_bool(self.config, "show_session", False),
            show_description=_config_bool(self.config, "show_description", True),
            show_error=True,
        )

    # ────────────────────────── 渲染 ──────────────────────────

    def _probe_render(self) -> bool:
        """探测 Pillow 与中文字体是否就绪。"""

        try:
            import PIL  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        try:
            from .fonts import find_cjk_font

            find_cjk_font(False)
            find_cjk_font(True)
        except Exception as exc:  # noqa: BLE001 - 字体缺失属于可预期的降级场景
            logger.info(f"{LOG_PREFIX} 未找到可用中文字体：{exc}")
            return False
        return True

    async def _render_cached(
        self, data: TaskListData, options: RenderOptions
    ) -> Path | None:
        """按内容指纹取图：命中就复用，没命中就渲染并写回缓存。"""

        fingerprint = self.image_cache.fingerprint(data, options, extra=self._font_signature())
        cached = self.image_cache.lookup(fingerprint)
        if cached is not None:
            logger.debug(f"{LOG_PREFIX} 命中图片缓存，复用 {cached.name}")
            return cached

        try:
            png = await asyncio.to_thread(self._render, data, options)
        except Exception:  # noqa: BLE001 - 渲染失败的兜底由调用方负责
            logger.exception(f"{LOG_PREFIX} 渲染任务列表图片失败")
            return None

        stored = self.image_cache.store(fingerprint, png)
        if stored is not None:
            return stored

        # 缓存写不进去（磁盘满/权限问题）时，仍然要能把图发出去：
        # 换到系统临时目录再写一次，那是最后一道可写的地方。
        try:
            import tempfile

            fd, temp_path = tempfile.mkstemp(
                prefix="astrbot_task_list_", suffix=".png"
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(png)
            return Path(temp_path)
        except OSError:
            logger.exception(f"{LOG_PREFIX} 图片写入失败")
            return None

    @staticmethod
    def _render(data: TaskListData, options: RenderOptions) -> bytes:
        """同步渲染入口，放到线程里跑，避免阻塞事件循环。"""

        from .render import render_task_list

        return render_task_list(data, options)

    def _font_signature(self) -> str:
        """把字体文件路径带上指纹。

        换了字体图就完全不一样，不带进指纹会出现「换了字体还发旧图」的怪现象。
        """

        try:
            from .fonts import find_cjk_font

            return "|".join((find_cjk_font(False), find_cjk_font(True)))
        except Exception:  # noqa: BLE001
            return ""

    async def terminate(self) -> None:
        """插件被停用时顺手清一次缓存，别把过期图片一直留在磁盘上。"""

        try:
            self.image_cache.prune()
        except Exception:  # noqa: BLE001 - 清理失败不影响卸载
            logger.debug(f"{LOG_PREFIX} 退出时清理图片缓存失败", exc_info=True)
