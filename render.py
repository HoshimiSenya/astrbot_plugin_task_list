"""用 Pillow 在本机把任务列表画成一张竖版长图。

为什么不用 AstrBot 的 T2I（HTML 转图片）服务：
1. 那套要走浏览器/远端服务，慢，而且部分部署根本没配；
2. 任务列表排版是固定结构，用 Pillow 直接画又快又稳，离线也能出图。

排版思路：所有尺寸先按「逻辑像素」算，最后统一乘 scale 再缩回去，
这样字体的抗锯齿边缘在手机上看着才不糊。

注意：图片里不放任何署名式页脚（插件名、渲染时刻之类），
标题下面只放数据本身的汇总信息。
"""

from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

from .fonts import find_cjk_font, load_font
from .models import RenderOptions, TaskEntry, TaskListData

# ────────────────────────── 版面常量（逻辑像素） ──────────────────────────

CANVAS_W = 1080
MARGIN_X = 44
HEADER_TOP = 44
CARD_GAP = 18
CARD_PAD = 26
CARD_RADIUS = 18
SECTION_GAP = 34
MIN_HEIGHT = 360

# 配色：浅色底 + 白卡片，对比度够高又不刺眼。
BG_TOP = (238, 242, 248)
BG_BOTTOM = (228, 234, 243)
CARD_BG = (255, 255, 255)
CARD_BORDER = (223, 230, 240)
INK = (32, 39, 51)
INK_SOFT = (78, 91, 110)
INK_MUTED = (128, 141, 160)
ACCENT = (47, 111, 208)
ACCENT_SOFT = (231, 240, 253)
ONCE_ACCENT = (217, 130, 43)
ONCE_SOFT = (253, 241, 228)
ERROR_INK = (198, 60, 60)
ERROR_SOFT = (253, 236, 236)
DIVIDER = (233, 238, 245)
SOFT_BG = (232, 238, 247)

# 字体档位。数字是逻辑像素，实际绘制时乘 scale。
F_TITLE = 42
F_SUMMARY = 22
F_TASK_NAME = 30
F_BADGE = 18
F_SCHEDULE = 22
F_META = 19
F_TAG = 17
F_SECTION = 30

_TRAILING_ELLIPSIS = "…"

# 来源角标的配色：(背景色, 文字色)。
# 三个来源必须一眼分得开，所以用蓝 / 绿 / 紫三种色相，而不是同一色系的深浅。
SOURCE_COLORS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "ai": ((231, 240, 253), (47, 111, 208)),        # 蓝：AI 自建
    "plugin": ((226, 245, 235), (32, 138, 90)),     # 绿：插件创建
    "user": ((240, 234, 253), (109, 68, 196)),      # 紫：用户自建
    "unknown": ((238, 241, 246), (118, 130, 148)),  # 灰：未知来源
}

# 分区标题的配色：(文字色, 强调色)
SECTION_COLORS: dict[bool, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    False: (INK, ACCENT),        # 循环提醒
    True: (INK, ONCE_ACCENT),    # 一次性提醒
}



class _Metrics:
    """量文字宽度用的一次性画布。

    Pillow 量文字必须有一张 Image 和 Draw 对象；每量一次都新建太浪费，
    这里复用同一张 8x8 的小图，只为拿到 textlength。
    """

    def __init__(self) -> None:
        self._image = Image.new("RGB", (8, 8))
        self._draw = ImageDraw.Draw(self._image)

    def width(self, text: str, font) -> int:
        if not text:
            return 0
        return int(self._draw.textlength(text, font=font))


_METRICS = _Metrics()


def _wrap(text: str, font, max_width: int, measure, max_lines: int = 2) -> list[str]:
    """按宽度折行，超过行数上限就省略。

    中文没有空格可断，只能逐字累加；遇到长英文单词时退化成逐字符断开，
    优先保证不溢出，再保证可读性。

    Args:
        measure: measure(text, font) -> 逻辑像素宽度。字体是按 scale 放大后加载的，
            直接拿设备像素去比会提前折行，所以宽度必须换算回逻辑像素再比。
    """

    raw = re.sub(r"\s+", " ", str(text or "").strip())
    if not raw or max_width <= 16:
        return []

    lines: list[str] = []
    current = ""
    for char in raw:
        candidate = current + char
        if measure(candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = char
        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines and len("".join(lines)) < len(raw):
        # 内容还有剩，最后一行补省略号。
        lines[-1] = _ellipsize(lines[-1], font, max_width, measure)

    return [line for line in lines if line]


def _ellipsize(text: str, font, max_width: int, measure) -> str:
    """把单行文字裁到指定宽度内，末尾补省略号。"""

    if measure(text, font) <= max_width:
        return text
    cropped = text
    while cropped and measure(cropped + _TRAILING_ELLIPSIS, font) > max_width:
        cropped = cropped[:-1]
    return (cropped + _TRAILING_ELLIPSIS) if cropped else ""


class _Painter:
    """把逻辑像素坐标自动乘 scale 的绘制封装。

    所有绘制方法都收逻辑坐标，缩放只在这里发生一次，
    免得每个调用点都写一遍 * scale，错一处整张图就歪了。
    """

    def __init__(self, image: Image.Image, scale: int) -> None:
        self.image = image
        self.scale = scale
        self.draw = ImageDraw.Draw(image)

    def rect(
        self,
        box: tuple[int, int, int, int],
        *,
        fill=None,
        outline=None,
        radius: int = 0,
        width: int = 1,
    ) -> None:
        x0, y0, x1, y1 = box
        scaled = tuple(int(value * self.scale) for value in (x0, y0, x1, y1))
        if radius > 0:
            self.draw.rounded_rectangle(
                scaled,
                radius=int(radius * self.scale),
                fill=fill,
                outline=outline,
                width=max(1, int(width * self.scale)),
            )
        else:
            self.draw.rectangle(
                scaled,
                fill=fill,
                outline=outline,
                width=max(1, int(width * self.scale)),
            )

    def line(self, start: tuple[int, int], end: tuple[int, int], color, width: int = 1) -> None:
        scaled_start = tuple(int(value * self.scale) for value in start)
        scaled_end = tuple(int(value * self.scale) for value in end)
        self.draw.line(
            [scaled_start, scaled_end],
            fill=color,
            width=max(1, int(width * self.scale)),
        )

    def text(
        self,
        xy: tuple[int, int],
        value: str,
        font,
        fill,
        anchor: str = "la",
    ) -> None:
        if not value:
            return
        self.draw.text(
            (int(xy[0] * self.scale), int(xy[1] * self.scale)),
            value,
            font=font,
            fill=fill,
            anchor=anchor,
        )


class _FontSet:
    """一次渲染要用到的全部字号，统一在这里加载好。"""

    def __init__(self, scale: int) -> None:
        self.scale = max(1, int(scale))
        regular = find_cjk_font(False)
        bold_path = find_cjk_font(True)

        def make(size: int, bold: bool = False):
            path = bold_path if bold else regular
            return load_font(path, max(1, int(size * scale)))

        self.title = make(F_TITLE, bold=True)
        self.summary = make(F_SUMMARY)
        self.task_name = make(F_TASK_NAME, bold=True)
        self.badge = make(F_BADGE, bold=True)
        self.schedule = make(F_SCHEDULE)
        self.meta = make(F_META)
        self.tag = make(F_TAG)
        self.section = make(F_SECTION, bold=True)

        # 标题字墨顶端相对绘制起点的下沉量（逻辑像素）。
        # 上下边距要与左右 MARGIN_X 几何对齐，就得知道中文字墨从 ascent 线往下沉了多少。
        probe = "未"
        self.title_ink_offset = max(0, int(self.title.getbbox(probe)[1]) / self.scale)

    def measure(self, text: str, font) -> int:
        """量出文字宽度并换算回逻辑像素。

        字体是按 scale 放大加载的，textlength 给的是设备像素；
        版面计算全部基于逻辑像素，不换算的话折行会提前一半，卡片也会错位。
        """

        return _METRICS.width(text, font) / max(1, self.scale)


def _background(width: int, height: int) -> Image.Image:
    """画一张从上到下微微变深的渐变底。

    纯色底看着太「塑料」，一点点渐变能让白色卡片浮起来。
    """

    top = BG_TOP
    bottom = BG_BOTTOM
    column = Image.new("RGB", (1, max(1, height)))
    pixels = column.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        pixels[0, y] = (
            int(top[0] + (bottom[0] - top[0]) * ratio),
            int(top[1] + (bottom[1] - top[1]) * ratio),
            int(top[2] + (bottom[2] - top[2]) * ratio),
        )
    return column.resize((width, height), Image.Resampling.NEAREST)


def _measure_entry(entry: TaskEntry, fonts: _FontSet, options: RenderOptions, inner_w: int) -> int:
    """预量一张卡片的高度。

    Pillow 没有「先排版再取高度」的能力，所以这里先算好高度，
    等整张画布尺寸确定后再真正绘制——否则卡片会互相压住。
    """

    height = CARD_PAD

    # 任务名实际可用宽度要扣掉右上角来源角标，否则量出来一行、画出来两行，
    # 卡片高度算少就会压住下面那张卡。
    badge_w = fonts.measure(entry.type_label, fonts.badge) + 22 if entry.type_label else 0
    name_w = max(80, inner_w - (badge_w + 16 if badge_w else 0))

    name_lines = _wrap(entry.name, fonts.task_name, name_w, fonts.measure, max_lines=2)
    height += max(1, len(name_lines)) * int(F_TASK_NAME * 1.32)

    if entry.next_run_text:
        height += 12 + int(F_SCHEDULE * 1.30)

    if options.show_description and entry.description:
        height += 10 + len(
            _wrap(entry.description, fonts.meta, inner_w, fonts.measure, max_lines=2)
        ) * int(F_META * 1.34)

    if options.show_session and entry.session_text:
        height += 8 + int(F_META * 1.34)

    if options.show_error and entry.error_text:
        height += 14 + int(F_META * 1.34)

    height += CARD_PAD
    return height


def _draw_badge(
    painter: _Painter,
    label: str,
    source: str,
    fonts: _FontSet,
    x_right: int,
    y: int,
) -> int:
    """在右上角画来源角标，返回它占的宽度（画不下时返回 0）。"""

    if not label:
        return 0
    fill, ink = SOURCE_COLORS.get(source, SOURCE_COLORS["unknown"])
    width = fonts.measure(label, fonts.badge) + 22
    height = 26
    x = x_right - width
    painter.rect((x, y, x + width, y + height), fill=fill, radius=13)
    # 用 lm 锚点把文字钉在胶囊的几何中心线上。
    # 之前按左上角定位（y+4），字号行高里 ascent/descent 的占比会把字顶偏，
    # 色块看起来就歪了；锚点对齐后色块中心 = 文字中心。
    painter.text((x + 11, y + height // 2), label, fonts.badge, ink, anchor="lm")
    return width


def _draw_entry(
    painter: _Painter,
    entry: TaskEntry,
    fonts: _FontSet,
    options: RenderOptions,
    y: int,
    inner_w: int,
    card_h: int,
) -> None:
    """画一张任务卡片。"""

    accent = ONCE_ACCENT if entry.run_once else ACCENT

    card_x0 = MARGIN_X
    card_x1 = CANVAS_W - MARGIN_X
    painter.rect(
        (card_x0, y, card_x1, y + card_h),
        fill=CARD_BG,
        outline=CARD_BORDER,
        radius=CARD_RADIUS,
        width=1,
    )
    # 卡片左侧贴边的色条：紧贴卡片外沿，整条贯穿卡片高度。
    # 关键是它必须「贴边」——一旦往里挪，就会挨着标题第一个字，
    # 反而被看成文字的一部分。贴边 + 贯穿 = 一眼就是卡片边框装饰。
    painter.rect(
        (card_x0, y + 16, card_x0 + 4, y + card_h - 16),
        fill=accent,
        radius=2,
    )

    cursor = y + CARD_PAD
    text_x = card_x0 + CARD_PAD
    text_w = inner_w

    # 来源角标先画，才知道第一行任务名还能用多宽。
    badge_w = _draw_badge(
        painter, entry.type_label, entry.source, fonts, card_x1 - CARD_PAD, y + CARD_PAD + 2
    )
    name_w = text_w - (badge_w + 16 if badge_w else 0)

    # 任务名（最多两行）
    name_lines = _wrap(entry.name, fonts.task_name, max(80, name_w), fonts.measure, max_lines=2)
    for line in name_lines:
        painter.text((text_x, cursor), line, fonts.task_name, INK)
        cursor += int(F_TASK_NAME * 1.32)

    # 时间行：只显示下次执行时间。重复规则那张卡上已经能从分区看出来
    # （一次性/循环），再印一遍「每天 09:00」既重复又把行挤乱。
    if entry.next_run_text:
        cursor += 12
        painter.text((text_x, cursor), entry.next_run_text, fonts.schedule, accent)
        cursor += int(F_SCHEDULE * 1.30)

    # 备注
    if options.show_description and entry.description:
        cursor += 10
        for line in _wrap(entry.description, fonts.meta, text_w, fonts.measure, max_lines=2):
            painter.text((text_x, cursor), line, fonts.meta, INK_SOFT)
            cursor += int(F_META * 1.34)

    # 会话
    if options.show_session and entry.session_text:
        cursor += 8
        label = "会话：" + entry.session_text
        painter.text(
            (text_x, cursor),
            _ellipsize(label, fonts.meta, text_w, fonts.measure),
            fonts.meta,
            INK_MUTED,
        )
        cursor += int(F_META * 1.34)

    # 上次失败
    if options.show_error and entry.error_text:
        cursor += 14
        err_h = int(F_META * 1.34) + 12
        painter.rect(
            (text_x - 8, cursor - 5, card_x1 - CARD_PAD + 4, cursor - 5 + err_h),
            fill=ERROR_SOFT,
            radius=9,
        )
        label = "上次失败：" + entry.error_text
        painter.text(
            (text_x + 2, cursor + 2),
            _ellipsize(label, fonts.meta, text_w - 12, fonts.measure),
            fonts.meta,
            ERROR_INK,
        )


# 分段标题条的高度。做成一整条通栏色带，和下面的白色圆角卡片形成明显的形状差异，
# 一眼就能看出「这是组」，而不是「这是某个任务的一部分」。
SECTION_HEADER_H = 58


def _draw_section_header(
    painter: _Painter,
    title: str,
    count: int,
    run_once: bool,
    fonts: _FontSet,
    y: int,
) -> int:
    """画一条分区标题带，返回它占的高度。

    设计上刻意不用「标题 + 一根竖线」：竖线和任务卡片左侧的强调条长得太像，
    容易被当成卡片装饰的一部分。这里改成「彩色方块图标 + 加粗大标题 + 右对齐数量」，
    不加任何底色块，靠排版层级而不是背景色来体现分组。
    """

    ink, accent = SECTION_COLORS[run_once]
    band_h = 42

    # 标题与数量的基线统一钉在 band_h / 2 这条中线上（lm 锚点），
    # 小方块的中心也对齐这条线 —— 三者共用一条中心轴，不再有谁歪谁。
    mid_y = y + band_h // 2

    # 不加底色块：纯靠「色块图标 + 加粗标题 + 数量」来分组。
    # 底色带会把两段切成一条条彩色横杠，和白色卡片叠在一起显得吵。
    icon_size = 14
    icon_x = MARGIN_X
    painter.rect(
        (icon_x, mid_y - icon_size // 2, icon_x + icon_size, mid_y - icon_size // 2 + icon_size),
        fill=accent,
        radius=4,
    )

    text_x = icon_x + icon_size + 12
    painter.text((text_x, mid_y), title, fonts.section, ink, anchor="lm")
    return SECTION_HEADER_H


def _draw_header(painter: _Painter, data: TaskListData, fonts: _FontSet) -> int:
    """画标题区，返回内容起始 y。"""

    # 上下边距与左右 MARGIN_X 几何一致：字墨顶端落在 y = MARGIN_X 处，
    # 绘制起点要上移字墨的下沉量（getbbox 的 top）。
    y = int(MARGIN_X - fonts.title_ink_offset)
    painter.text((MARGIN_X, y), data.title, fonts.title, INK)
    y += int(F_TITLE * 1.34)

    summary = data.summary
    if data.timezone_label:
        # 分隔符和 models.summary 里的「 · 」保持一致（半角空格），
        # 全角空格会把这一个点撑得比前面宽一倍，视觉上不对齐。
        summary = f"{summary} · 时区 {data.timezone_label}"
    painter.text((MARGIN_X, y), summary, fonts.summary, INK_MUTED)
    y += int(F_SUMMARY * 1.6)

    # 标题区下面拉一条细线，和卡片区分开。
    painter.line((MARGIN_X, y), (CANVAS_W - MARGIN_X, y), DIVIDER, 2)
    return y + 24


def _draw_empty(painter: _Painter, fonts: _FontSet, y: int) -> int:
    """空状态：画一张大白卡和一句话，而不是给一张空白图。"""

    card_h = 190
    painter.rect(
        (MARGIN_X, y, CANVAS_W - MARGIN_X, y + card_h),
        fill=CARD_BG,
        outline=CARD_BORDER,
        radius=CARD_RADIUS,
    )
    text = "现在没有待执行的未来任务"
    hint = "用 AstrBot 的定时任务功能创建任务后，这里就会列出来"
    text_w = fonts.measure(text, fonts.task_name)
    painter.text((int((CANVAS_W - text_w) // 2), y + 58), text, fonts.task_name, INK_SOFT)
    hint_w = fonts.measure(hint, fonts.meta)
    painter.text((int((CANVAS_W - hint_w) // 2), y + 108), hint, fonts.meta, INK_MUTED)
    return y + card_h


def render_task_list(data: TaskListData, options: RenderOptions) -> bytes:
    """把任务列表渲染成 PNG 图片。

    Args:
        data: 采集好的任务列表数据。
        options: 渲染开关（是否显示备注/会话、超采样倍数）。

    Returns:
        PNG 文件的二进制内容。

    Raises:
        RuntimeError: 找不到中文字体。
        Exception: Pillow 绘制过程中的任何异常，由调用方回退成文字输出。
    """

    scale = max(1, int(options.scale))
    fonts = _FontSet(scale)

    inner_w = CANVAS_W - MARGIN_X * 2 - CARD_PAD * 2

    # 一次性任务最怕错过，单独成段放最前面；两类各自按时间排好序，采集层已经排过。
    sections: list[tuple[str, list[TaskEntry], bool]] = []
    one_time = data.one_time_entries
    recurring = data.recurring_entries
    if one_time:
        sections.append(("一次性提醒", one_time, True))
    if recurring:
        sections.append(("循环提醒", recurring, False))

    # 第一遍：量高度，不确定总高就设不了画布尺寸。
    section_layouts: list[tuple[str, bool, list[tuple[TaskEntry, int]]]] = []
    content_h = 0
    for title, entries, run_once in sections:
        heights = [_measure_entry(entry, fonts, options, inner_w) for entry in entries]
        block = sum(heights) + CARD_GAP * max(0, len(entries) - 1)
        section_layouts.append((title, run_once, list(zip(entries, heights))))
        content_h += SECTION_HEADER_H + block + SECTION_GAP

    header_h = HEADER_TOP + int(F_TITLE * 1.34) + int(F_SUMMARY * 1.6) + 24

    # 底部留白 = MARGIN_X，与左右边距一样是纯几何距离（画布边 → 卡片边）。
    # 量高度时每段结尾都计入了 SECTION_GAP（段间距），但最后一段下面并没有下一段，
    # 所以总高要把多算的那个段间距扣掉。
    body_h = (190 if data.empty else content_h) - SECTION_GAP
    total_logical_h = max(MIN_HEIGHT, header_h + body_h + MARGIN_X)

    image = _background(CANVAS_W * scale, total_logical_h * scale)
    painter = _Painter(image, scale)

    y = _draw_header(painter, data, fonts)
    if data.empty:
        y = _draw_empty(painter, fonts, y)
    else:
        last_end = 0
        for title, run_once, items in section_layouts:
            y += _draw_section_header(painter, title, len(items), run_once, fonts, y)
            for entry, card_h in items:
                _draw_entry(painter, entry, fonts, options, y, inner_w, card_h)
                y += card_h
                last_end = y  # 最后一张卡片的底边，后面不再有任何内容
                y += CARD_GAP
            y += SECTION_GAP - CARD_GAP

    # 底部留白不再依赖步进推算：内容画完后按实际终点裁切画布。
    # 非空时底部 = 最后一张卡片底边 + MARGIN_X，与左右边距严格一致。
    target_h = total_logical_h if data.empty else max(MIN_HEIGHT, last_end + MARGIN_X)
    image = image.crop((0, 0, CANVAS_W * scale, target_h * scale))

    # 缩回逻辑尺寸：超采样画完再 LANCZOS 缩小，文字边缘会平滑很多。
    final = image.resize((CANVAS_W, target_h), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    final.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
