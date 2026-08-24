#!/usr/bin/env python3
"""
Midjourney Video 下载工具 · 统一启动器
======================================
从 Midjourney Explore 页面抓取视频并下载。

支持源 (2 个):
  midjourney_video      Midjourney Explore 视频 (Playwright 浏览器, tab=video_top)
  midjourney_video_url  Midjourney 指定链接视频下载

下载控制:
  --retries N        下载失败重试次数 (默认 3, 指数退避)
  --rate-limit N     请求速率上限 (请求/秒, 0=不限)
  --speed-limit N    下载速度上限 (KB/s, 0=不限)

用法：
  python run.py --source midjourney_video --max-images 50
  python run.py --source midjourney_video_url --url "https://www.midjourney.com/jobs/<uuid>?index=2"
  python run.py --source midjourney_video --headless false
  python run.py --source midjourney_video_url --content-url "https://..." --mj-index 1

协同模式（插件浏览收集 → CLI 批量下载）：
  1. 插件面板点击「导出URL列表」→ 保存 urls_xxx.txt
  2. CLI 读取该文件批量下载（带重试/限速/断点续传）：
     python run.py --batch-urls urls_20260718_143000.txt --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import config  # 持久化代理 / API Key (NOQA)

# 确保能 import sources 包
sys.path.insert(0, str(Path(__file__).parent))

from sources.base import log, ImageTask, Downloader, SourceStats  # noqa: E402
from sources.registry import ALL_SOURCES, build_source  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent
IMAGES_DIR = OUTPUT_DIR / "images"


async def run_source(name: str, args) -> dict:
    src = build_source(name, args)
    if src is None:
        return {name: "unknown"}

    # 下载控制参数
    src.retries = args.retries
    src.rate_limit = args.rate_limit
    src.speed_limit = args.speed_limit

    try:
        stats = await src.run()
        return {name: {
            "fetched": stats.fetched, "downloaded": stats.downloaded,
            "skipped": stats.skipped, "errors": stats.errors,
            "elapsed": round(stats.elapsed, 1),
        }}
    except Exception as e:
        log.error(f"源 [{name}] 异常: {e}")
        return {name: f"error: {e}"}


async def retry_failed(path: Path, args) -> dict:
    """读取失败清单 JSON，直接重投下载（不重新 fetch_tasks）"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("tasks", [])
    if not items:
        log.warning(f"失败清单为空: {path}")
        return {"retry": "empty list"}

    tasks: list[ImageTask] = []
    for it in items:
        tasks.append(ImageTask(
            url=it.get("url", ""),
            source=it.get("source", data.get("source", "")),
            title=it.get("title", ""),
            author=it.get("author", ""),
            width=it.get("width", 0),
            height=it.get("height", 0),
            referer=it.get("referer", ""),
            meta=it.get("meta", {}) or {},
        ))
    tasks = [t for t in tasks if t.url]

    src_name = data.get("source") or "retried"
    out_dir = IMAGES_DIR / src_name
    out_dir.mkdir(parents=True, exist_ok=True)

    dl = Downloader(out_dir, args.concurrency, args.proxy,
                    args.retries, args.rate_limit, args.speed_limit)
    stats = SourceStats()
    stats.start_time = time.time()
    completed = await dl.download_batch(tasks, stats)
    await dl.close()
    stats.end_time = time.time()

    ok = sum(1 for t in completed if t.downloaded)
    log.info(f"重试完成: 总 {len(tasks)} | 成功 {ok} | 失败 {stats.errors} | {stats.elapsed:.1f}s")
    if dl.failed:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = OUTPUT_DIR / "meta" / f"failed_retry_{src_name}_{ts}.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({
            "source": src_name, "timestamp": ts,
            "count": len(dl.failed), "tasks": dl.failed,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        log.warning(f"仍有 {len(dl.failed)} 个失败，已导出: {fp}")
    return {"retry": {"total": len(tasks), "downloaded": ok, "errors": stats.errors}}


async def batch_download_urls(path: Path, args) -> dict:
    """从插件导出的 URL 列表文件批量下载 CDN 直链视频。

    文件格式（每行一条）：
      https://cdn.midjourney.com/video/abc123/0.mp4  # abc123
    支持 # 注释和空行。"""
    text = path.read_text(encoding="utf-8")
    tasks: list[ImageTask] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 解析 "url  # video_id" 格式
        parts = line.split("#", 1)
        url = parts[0].strip()
        vid = parts[1].strip() if len(parts) > 1 else ""
        if not url.startswith("http"):
            continue
        tasks.append(ImageTask(
            url=url,
            source="midjourney_video",
            title=vid,
            referer="https://www.midjourney.com/",
            meta={"video_id": vid},
        ))

    if not tasks:
        log.warning(f"URL 列表为空: {path}")
        return {"batch": "empty"}

    out_dir = IMAGES_DIR / "midjourney_video"
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"批量下载模式 | URL 列表: {path.name} ({len(tasks)} 个)")
    log.info(f"输出目录: {out_dir}")
    log.info(f"并发={args.concurrency} 重试={args.retries} 限速={args.rate_limit}rps/{args.speed_limit}KBps")

    dl = Downloader(out_dir, args.concurrency, args.proxy,
                    args.retries, args.rate_limit, args.speed_limit)
    stats = SourceStats()
    stats.start_time = time.time()
    completed = await dl.download_batch(tasks, stats)
    await dl.close()
    stats.end_time = time.time()

    ok = sum(1 for t in completed if t.downloaded)
    log.info(f"批量下载完成: 总 {len(tasks)} | 成功 {ok} | 跳过 {stats.skipped} | 失败 {stats.errors} | {stats.elapsed:.1f}s")

    # 导出失败清单供重投
    if dl.failed:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = OUTPUT_DIR / "meta" / f"failed_batch_{ts}.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({
            "source": "batch_urls", "timestamp": ts,
            "count": len(dl.failed), "tasks": dl.failed,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        log.warning(f"失败清单已导出: {fp} (可用 --retry-failed 重投)")

    # 保存下载元数据
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    images = [{
        "url": t.url, "title": t.title, "filename": Path(t.local_path).name if t.local_path else "",
        "local": t.local_path, "meta": t.meta,
    } for t in completed if t.downloaded]
    meta_fp = OUTPUT_DIR / "meta" / f"batch_urls_{ts}.json"
    meta_fp.write_text(json.dumps({
        "source": "batch_urls", "timestamp": ts,
        "stats": {"fetched": len(tasks), "downloaded": ok, "skipped": stats.skipped, "errors": stats.errors},
        "images": images,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"元数据已保存: {meta_fp}")

    return {"batch": {"total": len(tasks), "downloaded": ok, "skipped": stats.skipped, "errors": stats.errors}}


async def main():
    ap = argparse.ArgumentParser(
        description="Midjourney Video 下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--source", default="midjourney_video",
                    choices=ALL_SOURCES,
                    help="视频源 (默认 midjourney_video)")
    ap.add_argument("--max-images", type=int, default=100, help="每源最大下载数")
    ap.add_argument("--concurrency", type=int, default=5, help="并发下载数")
    ap.add_argument("--proxy", default=None, help="代理 (如 socks5://127.0.0.1:1080)")

    # Midjourney 浏览器参数
    ap.add_argument("--tab", default="video_top", choices=["video_top", "top", "trending", "random", "new", "hot"])
    ap.add_argument("--size", default="thumb", choices=["thumb", "medium", "original", "original_webp"])
    ap.add_argument("--scrolls", type=int, default=20)
    ap.add_argument("--headless", type=lambda x: x.lower() != "false", default=True)
    ap.add_argument("--content-url", default="", help="指定 Midjourney 内容链接")
    ap.add_argument("--mj-index", type=int, default=0, help="指定变体 (1-4，0=全部)，配合 --content-url")
    ap.add_argument("--url", default="", help="指定 Midjourney 内容链接 (同 --content-url)")

    # 下载器：重试 / 限速
    ap.add_argument("--retries", type=int, default=3, help="下载失败重试次数")
    ap.add_argument("--rate-limit", type=float, default=0.0, help="请求速率上限(请求/秒, 0=不限)")
    ap.add_argument("--speed-limit", type=float, default=0.0, help="下载速度上限(KB/s, 0=不限)")
    ap.add_argument("--retry-failed", default=None,
                    help="重投失败清单 JSON (meta/failed_<source>_<ts>.json) 中的 URL")
    ap.add_argument("--batch-urls", default=None,
                    help="从插件导出的 URL 列表文件批量下载 (协同模式)")

    args = ap.parse_args()

    # 日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(OUTPUT_DIR / "scraper.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # ── 失败清单重投模式 ──
    if args.retry_failed:
        log.info("=" * 64)
        log.info(f"重投模式 | 清单: {args.retry_failed}")
        log.info("=" * 64)
        res = await retry_failed(Path(args.retry_failed), args)
        log.info(f"重投结果: {res}")
        return

    # ── 协同模式：从插件导出的 URL 列表批量下载 ──
    if args.batch_urls:
        log.info("=" * 64)
        log.info(f"协同模式 | 插件URL列表: {args.batch_urls}")
        log.info("=" * 64)
        res = await batch_download_urls(Path(args.batch_urls), args)
        log.info(f"结果: {res}")
        return

    sources = [args.source]

    log.info("=" * 64)
    log.info(f"Midjourney Video 下载 | 源={sources} | 每源 max={args.max_images}")
    log.info(f"输出根: {IMAGES_DIR}")
    log.info("=" * 64)

    t0 = time.time()
    results = {}

    for name in sources:
        results.update(await run_source(name, args))

    log.info("=" * 64)
    log.info("总览:")
    for name, info in results.items():
        log.info(f"  {name:20} {info}")
    log.info(f"总耗时: {time.time()-t0:.1f}s")
    log.info("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
