"""
Midjourney Video · 源注册表
===========================
从 sources/json/*.json 加载全部源配置，统一对外提供：
  - ALL_SOURCES     : 全部源名称列表
  - build_source(name, args) : 根据名称与运行参数构造 JsonSource 实例

仅包含 midjourney_video 和 midjourney_video_url 两个源。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .base import log, ImageTask, Downloader, SourceStats  # noqa: F401
from .engine import JsonSource

JSON_DIR = Path(__file__).resolve().parent / "json"

_CONFIGS: dict = {}
ALL_SOURCES: list = []


def _load_all():
    _CONFIGS.clear()
    if not JSON_DIR.exists():
        return
    for f in sorted(JSON_DIR.glob("*.json")):
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"  源配置加载失败 {f.name}: {e}")
            continue
        if isinstance(cfg, dict) and cfg.get("name"):
            _CONFIGS[cfg["name"]] = cfg
    ALL_SOURCES.clear()
    ALL_SOURCES.extend(sorted(_CONFIGS.keys()))


_load_all()


def _build_ctx(name: str, cfg: dict, args, keys: Optional[dict]) -> dict:
    """把命令行/GUI 的 args 翻译成引擎模板 ctx 变量。"""
    def g(attr, default=""):
        return getattr(args, attr, default)

    ctx: dict = {}
    ctx["query"] = g("query", "") or ""
    ctx["max_images"] = g("max_images", 100)
    ctx["tab"] = g("tab", "video_top")
    ctx["size"] = g("size", "thumb")
    ctx["scrolls"] = g("scrolls", 20)
    ctx["headless"] = g("headless", True)

    # Midjourney 指定内容链接爬取
    ctx["content_url"] = g("content_url", "") or g("url", "") or ""
    ctx["mj_index"] = g("mj_index", 0)

    # API Key
    key = g(f"{name}_key", None) or os.getenv(f"{name.upper()}_KEY") or ""
    if not key and isinstance(keys, dict):
        key = keys.get(name, "")
    ctx["key"] = key or ""

    # URL 编码后的关键词
    ctx["query_encoded"] = quote(ctx["query"]) if ctx["query"] else ""
    return ctx


def build_source(name: str, args, keys: Optional[dict] = None):
    """根据名称与运行参数构造 JsonSource；未知名称返回 None。"""
    cfg = _CONFIGS.get(name)
    if cfg is None:
        return None
    ctx = _build_ctx(name, cfg, args, keys)
    return JsonSource(
        cfg,
        output_root=_images_dir(),
        concurrency=getattr(args, "concurrency", 5),
        proxy=getattr(args, "proxy", None),
        max_images=getattr(args, "max_images", 100),
        retries=getattr(args, "retries", 3),
        rate_limit=getattr(args, "rate_limit", 0.0),
        speed_limit=getattr(args, "speed_limit", 0.0),
        ctx=ctx,
        keys=keys,
    )


def _images_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "images"


def reload():
    """重新扫描 json 目录（便于热更新配置）。"""
    _load_all()
