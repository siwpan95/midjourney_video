"""
多源图片爬虫 - sources 包
========================
各图片源现已统一为 JSON 配置（见 ./json/*.json），由 engine.JsonSource 驱动。
构建入口：from sources.registry import ALL_SOURCES, build_source
"""

from .base import log, ImageTask, Downloader, SourceStats, SourceBase  # noqa: F401
from .engine import JsonSource  # noqa: F401
from .registry import ALL_SOURCES, build_source, reload  # noqa: F401

__all__ = [
    "log", "ImageTask", "Downloader", "SourceStats", "SourceBase",
    "JsonSource", "ALL_SOURCES", "build_source", "reload",
]
