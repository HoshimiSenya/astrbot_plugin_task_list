"""找一个真正能用中文的字体文件。

AstrBot 大多跑在 Linux 服务器上，不同发行版的中文字体安装路径、文件名差别很大；
开发机又常是 Windows 或 macOS，路径完全另一套。这里按
「常见固定路径 -> 字体目录递归扫描 -> fontconfig 查询」的顺序找，
找到第一个就缓存，避免每次出图都去扫一遍目录。

找不到字体时抛 RuntimeError 而不是画方块：一排豆腐块比一句明确的报错更难排查。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# 结果缓存。按「是否加粗」分开存，因为常规体和粗体往往是两个文件。
_FONT_CACHE: dict[bool, str] = {}
# 递归扫描的结果，扫一次就够，避免每次找字体都 walk 整个字体目录。
_DISCOVERED_CACHE: list[str] | None = None

# 只认这几种字体后缀；字体目录里往往还混着 bitmap、afm 等文件。
_FONT_SUFFIXES = {".ttf", ".ttc", ".otf"}

# 递归扫描时只挑名字里带这些关键字的文件，其余（西文字体）扫进来也用不上。
_CJK_KEYWORDS = (
    "notosanscjk",
    "notosanssc",
    "notoserifcjk",
    "sourcehansans",
    "sourcehanserif",
    "wqy",
    "droidsansfallback",
    "uming",
    "ukai",
    "simhei",
    "msyh",
    "simsun",
    "harmonyos",
    "pingfang",
    "hiragino",
)

_BOLD_MARKERS = ("bold", "bd", "heavy", "black", "-b.")

# 常见的 Linux 中文字体路径（Debian/Ubuntu/CentOS/Arch 各写一遍）。
_LINUX_REGULAR = [
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
    "/usr/share/fonts/adobe-source-han-sans/SourceHanSansSC-Regular.otf",
]

_LINUX_BOLD = [
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf",
] + _LINUX_REGULAR

# 用正斜杠写 Windows 路径：Python/Pillow 在 Windows 上同样认，也省得转义。
_WINDOWS_REGULAR = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/simkai.ttf",
]

_WINDOWS_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyhbd.ttf",
    "C:/Windows/Fonts/simhei.ttf",
] + _WINDOWS_REGULAR

_MAC_REGULAR = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
]

_MAC_BOLD = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
] + _MAC_REGULAR

# 递归扫描的起点：发行版把字体装在哪里基本逃不出这几处。
_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/X11/fonts",
    "~/.local/share/fonts",
    "~/.fonts",
    "C:/Windows/Fonts",
    "/System/Library/Fonts",
)


def _is_font_file(path: str) -> bool:
    """判断路径是否是一个存在的字体文件。"""

    try:
        return bool(path) and Path(path).suffix.lower() in _FONT_SUFFIXES and Path(path).is_file()
    except OSError:
        return False


def _order_by_weight(fonts: list[str], bold: bool) -> list[str]:
    """加粗请求优先返回 Bold 文件，常规请求优先返回 Regular 文件。"""

    preferred: list[str] = []
    others: list[str] = []
    for path in fonts:
        name = Path(path).name.lower()
        looks_bold = any(marker in name for marker in _BOLD_MARKERS)
        (preferred if looks_bold == bold else others).append(path)
    return preferred + others


def _discover_fonts() -> list[str]:
    """递归扫描常见字体目录，找出名字里带中文字体关键字的文件。"""

    global _DISCOVERED_CACHE
    if _DISCOVERED_CACHE is not None:
        return _DISCOVERED_CACHE

    found: list[str] = []
    seen: set[str] = set()
    for raw_dir in _FONT_DIRS:
        base = Path(os.path.expanduser(raw_dir))
        if not base.is_dir():
            continue
        try:
            for root, _dirs, files in os.walk(base):
                for filename in files:
                    lowered = filename.lower()
                    if os.path.splitext(lowered)[1] not in _FONT_SUFFIXES:
                        continue
                    if not any(keyword in lowered for keyword in _CJK_KEYWORDS):
                        continue
                    full = os.path.join(root, filename)
                    if full not in seen:
                        seen.add(full)
                        found.append(full)
        except OSError:
            # 单个目录没权限就跳过，不能因为一个目录扫不了就整体失败。
            continue

    _DISCOVERED_CACHE = found
    return found


def _fontconfig_fonts() -> list[str]:
    """用 fontconfig 查系统里登记过的中文字体。

    有些发行版把字体装在非标准目录，光靠 walk 固定路径找不到，
    fontconfig 是它们自己的索引，问它最准。
    """

    if shutil.which("fc-list") is None:
        return []
    try:
        result = subprocess.run(
            ["fc-list", ":lang=zh-cn", "-f", "%{file}\n"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    paths: list[str] = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if _is_font_file(path):
            paths.append(path)
    return paths


def find_cjk_font(bold: bool = False) -> str:
    """返回一个可用的中文字体文件绝对路径。

    Args:
        bold: 需要粗体时传 True；系统只有一种字重时两种请求会返回同一个文件。

    Returns:
        字体文件的绝对路径。

    Raises:
        RuntimeError: 系统里一个中文字体都找不到。此时应该降级成纯文字输出，
            而不是硬画一张全是方块的图。
    """

    if bold in _FONT_CACHE:
        return _FONT_CACHE[bold]

    # 1) 先看写死的常见路径，绝大多数机器在这一步就命中了。
    fixed = _WINDOWS_BOLD if bold else _WINDOWS_REGULAR
    for path in fixed:
        if _is_font_file(path):
            _FONT_CACHE[bold] = path
            return path
    for path in (_MAC_BOLD if bold else _MAC_REGULAR):
        if _is_font_file(path):
            _FONT_CACHE[bold] = path
            return path
    for path in (_LINUX_BOLD if bold else _LINUX_REGULAR):
        if _is_font_file(path):
            _FONT_CACHE[bold] = path
            return path

    # 2) 路径对不上就扫字体目录。
    for path in _order_by_weight(_discover_fonts(), bold):
        if _is_font_file(path):
            _FONT_CACHE[bold] = path
            return path

    # 3) 再问 fontconfig。
    for path in _order_by_weight(_fontconfig_fonts(), bold):
        if _is_font_file(path):
            _FONT_CACHE[bold] = path
            return path

    raise RuntimeError(
        "未找到可用的中文字体，请安装 fonts-noto-cjk（Noto Sans CJK）"
        "或 fonts-wqy-microhei（文泉驿微米黑）后重试"
    )


def clear_font_cache() -> None:
    """清空字体查找缓存。

    在运行时装了新字体、又不想重启 AstrBot 时用得上。
    """

    global _DISCOVERED_CACHE
    _FONT_CACHE.clear()
    _DISCOVERED_CACHE = None


def load_font(path: str, size: int):
    """按字号加载字体，TTC 字体集合优先挑简体中文那一款字面。

    Args:
        path: find_cjk_font 返回的路径。
        size: 字号（像素）。

    Returns:
        PIL.ImageFont.FreeTypeFont 实例。
    """

    from PIL import ImageFont

    if path.lower().endswith(".ttc"):
        for index in range(8):
            try:
                loaded = ImageFont.truetype(path, size=size, index=index)
            except OSError:
                break
            try:
                family, _style = loaded.getname()
            except Exception:  # noqa: BLE001 - 个别字体拿不到名字，忽略即可
                family = ""
            lowered = family.lower()
            if "sc" in lowered or "simplified" in lowered or "gb" in lowered:
                return loaded
    try:
        return ImageFont.truetype(path, size=size, index=0)
    except OSError:
        return ImageFont.truetype(path, size=size)
