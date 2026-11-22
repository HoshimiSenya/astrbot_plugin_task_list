"""图片缓存：内容没有变化时复用上一次渲染好的图片，省掉重复渲染的开销。

缓存以「内容指纹」为文件名。指纹由任务列表数据、渲染开关和样式版本共同决定，
任何一项变化都会得到新的指纹，因此不会出现「内容变了却还发旧图」的情况。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .models import STYLE_VERSION, RenderOptions, TaskListData

FILE_SUFFIX = ".png"


class ImageCache:
    """按内容指纹存取 PNG 图片的本地缓存。"""

    def __init__(
        self,
        cache_dir: Path | str,
        *,
        enabled: bool = True,
        max_bytes: int = 32 * 1024 * 1024,
        ttl_hours: float = 168.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.enabled = enabled
        self.max_bytes = max(0, int(max_bytes))
        self.ttl_seconds = max(0.0, float(ttl_hours) * 3600.0)

    # ────────────────────────── 指纹 ──────────────────────────

    def fingerprint(
        self,
        data: TaskListData,
        options: RenderOptions,
        *,
        extra: str = "",
    ) -> str:
        """计算当前内容对应的指纹。

        Args:
            data: 这次要渲染的任务列表。
            options: 这次使用的渲染开关。
            extra: 额外参与指纹的字符串（例如字体文件路径），可为空。

        Returns:
            十六进制指纹字符串，可直接当文件名使用。
        """

        payload = {
            "style": STYLE_VERSION,
            "extra": extra,
            "data": self._normalize(dataclasses.asdict(data)),
            "options": self._normalize(dataclasses.asdict(options)),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]

    @staticmethod
    def _normalize(value: Any) -> Any:
        """把 dataclass/dict 转成可稳定序列化的结构。"""

        if isinstance(value, dict):
            return {str(k): ImageCache._normalize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [ImageCache._normalize(v) for v in value]
        return value

    def path_for(self, fingerprint: str) -> Path:
        """指纹对应的缓存文件路径。"""

        return self.cache_dir / f"{fingerprint}{FILE_SUFFIX}"

    # ────────────────────────── 读写 ──────────────────────────

    def lookup(self, fingerprint: str) -> Path | None:
        """命中缓存时返回图片路径，否则返回 None。"""

        if not self.enabled:
            return None
        path = self.path_for(fingerprint)
        try:
            if not path.is_file():
                return None
            if path.stat().st_size <= 0:
                path.unlink(missing_ok=True)
                return None
            if self.ttl_seconds > 0:
                age = time.time() - path.stat().st_mtime
                if age > self.ttl_seconds:
                    path.unlink(missing_ok=True)
                    return None
            # 只刷新访问时间，绝不动修改时间：命中缓存不应该看起来像"又写了一次文件"。
            try:
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            except OSError:
                pass
            return path
        except OSError:
            return None

    def store(self, fingerprint: str, png: bytes) -> Path | None:
        """写入缓存，返回落盘路径；失败时返回 None（不影响正常出图）。"""

        if not self.enabled or not png:
            return None
        path = self.path_for(fingerprint)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.cache_dir), prefix=".tmp-", suffix=FILE_SUFFIX
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(png)
                os.replace(tmp_name, path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
            self.prune()
            return path
        except OSError:
            return None

    # ────────────────────────── 清理 ──────────────────────────

    def prune(self) -> int:
        """按有效期与容量上限清理旧图片，返回删除的文件数。"""

        if not self.enabled:
            return 0
        try:
            files = [p for p in self.cache_dir.glob(f"*{FILE_SUFFIX}") if p.is_file()]
        except OSError:
            return 0

        removed = 0
        now = time.time()
        kept: list[tuple[float, int, Path]] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            if self.ttl_seconds > 0 and now - stat.st_mtime > self.ttl_seconds:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
                continue
            kept.append((stat.st_mtime, stat.st_size, path))

        if self.max_bytes <= 0:
            return removed

        total = sum(size for _mtime, size, _path in kept)
        if total <= self.max_bytes:
            return removed
        for _mtime, size, path in sorted(kept, key=lambda item: item[0]):
            if total <= self.max_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed
