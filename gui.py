#!/usr/bin/env python3
"""
Midjourney Video Downloader · 图形界面
========================================
基于 run.py 的全部功能提供 tkinter 可视化操作界面。

运行：python gui.py

功能：
  - 源抓取模式：选择 midjourney_video / midjourney_video_url，设置浏览器参数
  - 批量 URL 模式：读取插件导出的 URL 列表批量下载（协同模式）
  - 失败重投模式：读取失败清单 JSON 重新下载
  - 代理配置：手动填写 / 自动探测 / 连通性测试
  - 下载控制：并发数 / 重试次数 / 速率限制 / 速度限制
  - 实时进度条 + 统计面板 + 滚动日志
  - 视频拼接工具入口
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

# ── 项目根路径 ──
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import config as cfg_module  # noqa: E402
from sources.base import ImageTask, Downloader, SourceStats  # noqa: E402
from sources.registry import ALL_SOURCES, build_source  # noqa: E402

OUTPUT_DIR = PROJECT_DIR
IMAGES_DIR = OUTPUT_DIR / "images"

# ── 队列消息类型 ──
MSG_PROGRESS = "progress"   # (MSG_PROGRESS, float 0~1)
MSG_STATUS  = "status"      # (MSG_STATUS, str)
MSG_LOG     = "log"         # (MSG_LOG, str)
MSG_DONE    = "done"        # (MSG_DONE, dict stats)
MSG_ERROR   = "error"       # (MSG_ERROR, str)
MSG_STATS   = "stats"       # (MSG_STATS, dict)


# ====================================================================
# 自定义日志 Handler：把 logging 消息路由到 GUI 队列
# ====================================================================
class QueueLogHandler(logging.Handler):
    """将日志消息发送到 queue.Queue，供 GUI 线程消费。"""

    def __init__(self, msg_queue: queue.Queue):
        super().__init__()
        self._q = msg_queue
        self.setLevel(logging.INFO)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put((MSG_LOG, self.format(record)))
        except Exception:
            pass


# ====================================================================
# 工作线程
# ====================================================================
class WorkerThread(threading.Thread):
    """后台线程：运行 asyncio 下载任务，通过 queue.Queue 回传进度。"""

    def __init__(self, msg_queue: queue.Queue, args: dict):
        super().__init__(daemon=True)
        self._q = msg_queue
        self._args = args
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception as e:
            self._q.put((MSG_ERROR, str(e)))
        finally:
            loop.close()

    async def _run(self) -> None:
        args = self._args
        mode = args["mode"]

        if mode == "source":
            await self._do_source(args)
        elif mode == "batch_urls":
            await self._do_batch(args)
        elif mode == "retry_failed":
            await self._do_retry(args)
        else:
            self._q.put((MSG_ERROR, f"未知模式: {mode}"))

    # ── 统计信息轮询器：适用于源抓取（浏览器模式内部管理 stats）──
    @staticmethod
    async def _stats_poller(src, q: queue.Queue, stop: threading.Event, max_images: int):
        """每秒轮询 src.stats，变化时推送进度。"""
        last = (0, 0, 0, 0)
        while not stop.is_set():
            await asyncio.sleep(0.4)
            s = src.stats
            cur = (s.fetched, s.downloaded, s.skipped, s.errors)
            if cur == last:
                continue
            last = cur
            elapsed = (time.time() - s.start_time) if s.start_time else 0
            total = max(s.fetched, max_images, 1) if s.fetched > 0 else max_images
            frac = min(s.downloaded / total, 1.0) if s.fetched > 0 else 0.02
            q.put((MSG_PROGRESS, frac))
            q.put((MSG_STATS, {
                "fetched": s.fetched, "downloaded": s.downloaded,
                "skipped": s.skipped, "errors": s.errors,
                "elapsed": elapsed,
            }))

    # ── 源抓取模式 ──
    async def _do_source(self, args: dict) -> None:
        class _Args:
            pass

        a = _Args()
        a.source       = args["source"]
        a.max_images   = args.get("max_images", 100)
        a.concurrency  = args.get("concurrency", 5)
        a.proxy        = args.get("proxy") or None
        a.tab          = args.get("tab", "video_top")
        a.size         = args.get("size", "thumb")
        a.scrolls      = args.get("scrolls", 20)
        a.headless     = args.get("headless", True)
        a.content_url  = args.get("content_url") or args.get("url") or ""
        a.mj_index     = args.get("mj_index", 0)
        a.url          = args.get("url") or ""
        a.retries      = args.get("retries", 3)
        a.rate_limit   = args.get("rate_limit", 0.0)
        a.speed_limit  = args.get("speed_limit", 0.0)
        a.query        = ""
        a.retry_failed = None
        a.batch_urls   = None

        max_img = a.max_images
        self._q.put((MSG_LOG, f"[模式] 源抓取: {a.source}"))
        self._q.put((MSG_LOG, f"[参数] max={max_img} concurrency={a.concurrency} "
                              f"tab={a.tab} headless={a.headless} scrolls={a.scrolls}"))
        self._q.put((MSG_STATUS, f"启动浏览器... tab={a.tab}"))

        try:
            src = build_source(a.source, a)
        except Exception as e:
            self._q.put((MSG_ERROR, f"构建源失败: {e}"))
            return
        if src is None:
            self._q.put((MSG_ERROR, f"未知源: {a.source}"))
            return

        src.retries     = a.retries
        src.rate_limit  = a.rate_limit
        src.speed_limit = a.speed_limit

        # 启动 stats 轮询器
        poll_stop = threading.Event()
        poll_task = asyncio.create_task(
            self._stats_poller(src, self._q, poll_stop, max_img)
        )

        try:
            stats = await src.run()
            self._q.put((MSG_PROGRESS, 1.0))
            elapsed = stats.elapsed
            self._q.put((MSG_STATS, {
                "fetched": stats.fetched, "downloaded": stats.downloaded,
                "skipped": stats.skipped, "errors": stats.errors,
                "elapsed": elapsed,
            }))
            self._q.put((MSG_DONE, {
                "fetched": stats.fetched, "downloaded": stats.downloaded,
                "skipped": stats.skipped, "errors": stats.errors,
                "elapsed": round(elapsed, 1),
            }))
        except Exception as e:
            self._q.put((MSG_ERROR, f"下载异常: {e}"))
        finally:
            poll_stop.set()
            try:
                poll_task.cancel()
            except Exception:
                pass

    # ── 失败清单重投 ──
    async def _do_retry(self, args: dict) -> None:
        path = Path(args["retry_failed_path"])
        if not path.exists():
            self._q.put((MSG_ERROR, f"文件不存在: {path}"))
            return

        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("tasks", [])
        if not items:
            self._q.put((MSG_ERROR, "失败清单为空"))
            return

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
        total = len(tasks)

        self._q.put((MSG_LOG, f"[重投] 共 {total} 个失败任务"))
        self._q.put((MSG_STATUS, f"开始重投 {total} 个任务..."))

        src_name = data.get("source") or "retried"
        out_dir = IMAGES_DIR / src_name
        out_dir.mkdir(parents=True, exist_ok=True)

        concurrency = args.get("concurrency", 5)
        retries_val = args.get("retries", 3)
        rate_limit  = args.get("rate_limit", 0.0)
        speed_limit = args.get("speed_limit", 0.0)
        proxy       = args.get("proxy") or None

        dl = Downloader(out_dir, concurrency, proxy, retries_val, rate_limit, speed_limit)
        stats = SourceStats()
        stats.start_time = time.time()

        # 包装下载方法以跟踪进度
        orig = dl._download

        async def tracked(t: ImageTask, s: SourceStats) -> ImageTask:
            result = await orig(t, s)
            frac = min(s.downloaded / max(total, 1), 1.0)
            self._q.put((MSG_PROGRESS, frac))
            self._q.put((MSG_STATS, {
                "fetched": total, "downloaded": s.downloaded,
                "skipped": s.skipped, "errors": s.errors,
                "elapsed": s.elapsed,
            }))
            return result

        dl._download = tracked

        completed = await dl.download_batch(tasks, stats)
        await dl.close()
        stats.end_time = time.time()

        ok = sum(1 for t in completed if t.downloaded)
        self._q.put((MSG_PROGRESS, 1.0))
        self._q.put((MSG_DONE, {
            "fetched": total, "downloaded": ok,
            "skipped": stats.skipped, "errors": stats.errors,
            "elapsed": round(stats.elapsed, 1),
        }))

        if dl.failed:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fp = OUTPUT_DIR / "meta" / f"failed_retry_{src_name}_{ts}.json"
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps({
                "source": src_name, "timestamp": ts,
                "count": len(dl.failed), "tasks": dl.failed,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            self._q.put((MSG_LOG, f"仍有 {len(dl.failed)} 个失败 → {fp}"))

    # ── 批量 URL 下载（协同模式）──
    async def _do_batch(self, args: dict) -> None:
        path = Path(args["batch_urls_path"])
        if not path.exists():
            self._q.put((MSG_ERROR, f"文件不存在: {path}"))
            return

        text = path.read_text(encoding="utf-8")
        tasks: list[ImageTask] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("#", 1)
            url = parts[0].strip()
            vid = parts[1].strip() if len(parts) > 1 else ""
            if not url.startswith("http"):
                continue
            tasks.append(ImageTask(
                url=url, source="midjourney_video", title=vid,
                referer="https://www.midjourney.com/",
                meta={"video_id": vid},
            ))

        if not tasks:
            self._q.put((MSG_ERROR, "URL 列表为空"))
            return

        total = len(tasks)
        concurrency = args.get("concurrency", 5)
        retries_val = args.get("retries", 3)
        rate_limit  = args.get("rate_limit", 0.0)
        speed_limit = args.get("speed_limit", 0.0)
        proxy       = args.get("proxy") or None

        self._q.put((MSG_LOG, f"[协同] 批量下载 {total} 个 URL"))
        self._q.put((MSG_LOG, f"[参数] concurrency={concurrency} retries={retries_val} "
                              f"rate_limit={rate_limit} speed_limit={speed_limit}"))
        self._q.put((MSG_STATUS, f"下载中... 0/{total}"))

        out_dir = IMAGES_DIR / "midjourney_video"
        out_dir.mkdir(parents=True, exist_ok=True)

        dl = Downloader(out_dir, concurrency, proxy, retries_val, rate_limit, speed_limit)
        stats = SourceStats()
        stats.start_time = time.time()

        # 包装下载方法以跟踪进度
        orig = dl._download

        async def tracked(t: ImageTask, s: SourceStats) -> ImageTask:
            result = await orig(t, s)
            frac = min(s.downloaded / max(total, 1), 1.0)
            self._q.put((MSG_PROGRESS, frac))
            self._q.put((MSG_STATS, {
                "fetched": total, "downloaded": s.downloaded,
                "skipped": s.skipped, "errors": s.errors,
                "elapsed": s.elapsed,
            }))
            return result

        dl._download = tracked

        completed = await dl.download_batch(tasks, stats)
        await dl.close()
        stats.end_time = time.time()

        ok = sum(1 for t in completed if t.downloaded)
        self._q.put((MSG_PROGRESS, 1.0))
        self._q.put((MSG_DONE, {
            "fetched": total, "downloaded": ok,
            "skipped": stats.skipped, "errors": stats.errors,
            "elapsed": round(stats.elapsed, 1),
        }))

        if dl.failed:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fp = OUTPUT_DIR / "meta" / f"failed_batch_{ts}.json"
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps({
                "source": "batch_urls", "timestamp": ts,
                "count": len(dl.failed), "tasks": dl.failed,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            self._q.put((MSG_LOG, f"失败清单已导出: {fp}"))

        # 保存元数据
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        images = [{
            "url": t.url, "title": t.title,
            "filename": Path(t.local_path).name if t.local_path else "",
            "local": t.local_path, "meta": t.meta,
        } for t in completed if t.downloaded]
        meta_fp = OUTPUT_DIR / "meta" / f"batch_urls_{ts}.json"
        meta_fp.write_text(json.dumps({
            "source": "batch_urls", "timestamp": ts,
            "stats": {"fetched": total, "downloaded": ok,
                       "skipped": stats.skipped, "errors": stats.errors},
            "images": images,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        self._q.put((MSG_LOG, f"元数据已保存: {meta_fp}"))


# ====================================================================
# 主 GUI 类
# ====================================================================
class MidjourneyVideoGUI:
    """Midjourney Video Downloader 主界面"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Midjourney Video Downloader")
        self.root.geometry("800x700")
        self.root.resizable(True, True)
        self.root.minsize(640, 560)

        self.running: bool = False
        self.worker: Optional[WorkerThread] = None
        self.queue: queue.Queue = queue.Queue()
        self._log_handler: Optional[QueueLogHandler] = None

        # 加载持久化配置
        self._cfg = cfg_module.load_config()

        self._build_widgets()
        self._load_config_to_ui()
        self._on_mode_change()
        self._poll_queue()

    # ================================================================
    # 界面布局
    # ================================================================
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 3}
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, **pad)

        # ── 模式选择 ──
        mode_grp = ttk.LabelFrame(main, text="运行模式")
        mode_grp.pack(fill="x", pady=3)
        self.mode_var = tk.StringVar(value="source")
        ttk.Radiobutton(mode_grp, text="源抓取 (Explore)",
                        variable=self.mode_var, value="source",
                        command=self._on_mode_change).pack(side="left", padx=12, pady=4)
        ttk.Radiobutton(mode_grp, text="批量 URL (协同)",
                        variable=self.mode_var, value="batch_urls",
                        command=self._on_mode_change).pack(side="left", padx=12, pady=4)
        ttk.Radiobutton(mode_grp, text="失败重投",
                        variable=self.mode_var, value="retry_failed",
                        command=self._on_mode_change).pack(side="left", padx=12, pady=4)

        # ── 源配置面板（源抓取模式）──
        self.src_panel = ttk.LabelFrame(main, text="源配置")
        self._build_source_panel()

        # ── 文件路径面板（批量/重投模式）──
        self.file_panel = ttk.LabelFrame(main, text="文件路径")
        self._build_file_panel()

        # ── 网络配置 ──
        self.net_grp = ttk.LabelFrame(main, text="网络配置")
        self.net_grp.pack(fill="x", pady=3)

        proxy_row = ttk.Frame(self.net_grp)
        proxy_row.pack(fill="x", padx=4, pady=2)
        ttk.Label(proxy_row, text="代理:").pack(side="left")
        self.proxy_var = tk.StringVar()
        ttk.Entry(proxy_row, textvariable=self.proxy_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(proxy_row, text="自动探测", command=self._auto_detect_proxy).pack(side="left", padx=2)
        ttk.Button(proxy_row, text="连通测试", command=self._test_proxy).pack(side="left", padx=2)
        self.proxy_status_var = tk.StringVar()
        ttk.Label(proxy_row, textvariable=self.proxy_status_var, foreground="gray").pack(side="left", padx=6)

        dl_row = ttk.Frame(self.net_grp)
        dl_row.pack(fill="x", padx=4, pady=2)
        ttk.Label(dl_row, text="重试:").pack(side="left")
        self.retries_var = tk.StringVar(value="3")
        ttk.Entry(dl_row, textvariable=self.retries_var, width=4).pack(side="left", padx=2)
        ttk.Label(dl_row, text="速率限制(r/s):").pack(side="left", padx=(12, 0))
        self.rate_var = tk.StringVar(value="0")
        ttk.Entry(dl_row, textvariable=self.rate_var, width=5).pack(side="left", padx=2)
        ttk.Label(dl_row, text="速度限制(KB/s):").pack(side="left", padx=(12, 0))
        self.speed_var = tk.StringVar(value="0")
        ttk.Entry(dl_row, textvariable=self.speed_var, width=5).pack(side="left", padx=2)

        # ── 输出目录 ──
        out_grp = ttk.LabelFrame(main, text="输出目录")
        out_grp.pack(fill="x", pady=3)
        out_row = ttk.Frame(out_grp)
        out_row.pack(fill="x", padx=4, pady=4)
        self.output_var = tk.StringVar(value=str(IMAGES_DIR))
        ttk.Entry(out_row, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(out_row, text="浏览...", command=self._browse_output).pack(side="left", padx=2)
        ttk.Button(out_row, text="打开", command=self._open_output).pack(side="left", padx=2)

        # ── 操作按钮 ──
        btn_row = ttk.Frame(main)
        btn_row.pack(fill="x", pady=6)
        self.start_btn = ttk.Button(btn_row, text="开始下载", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn_row, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btn_row, text="视频拼接工具", command=self._open_cut_gui).pack(side="right", padx=4)

        # ── 进度条 ──
        self.progress = ttk.Progressbar(main, orient="horizontal", mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=4)

        # ── 统计信息 ──
        stats_row = ttk.Frame(main)
        stats_row.pack(fill="x")
        self.stats_var = tk.StringVar(value="就绪")
        ttk.Label(stats_row, textvariable=self.stats_var).pack(side="left")
        self.time_var = tk.StringVar()
        ttk.Label(stats_row, textvariable=self.time_var, foreground="gray").pack(side="right")

        # ── 日志区 ──
        log_grp = ttk.LabelFrame(main, text="日志")
        log_grp.pack(fill="both", expand=True, pady=3)
        self.log_text = tk.Text(log_grp, height=10, state="disabled", font=("Consolas", 9),
                                wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        log_scroll = ttk.Scrollbar(log_grp, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=log_scroll.set)

        # ── 状态栏 ──
        self.status_var = tk.StringVar(value="就绪 — 选择模式后点击「开始下载」")
        ttk.Label(main, textvariable=self.status_var, relief="sunken", anchor="w",
                  padding=(4, 2)).pack(fill="x", pady=(4, 0))

    def _build_source_panel(self) -> None:
        """构建源配置面板内容。"""
        row0 = ttk.Frame(self.src_panel)
        row0.pack(fill="x", padx=4, pady=2)
        ttk.Label(row0, text="源:").pack(side="left")
        self.source_var = tk.StringVar(value="midjourney_video")
        self.source_cb = ttk.Combobox(row0, textvariable=self.source_var,
                                       values=ALL_SOURCES, state="readonly", width=22)
        self.source_cb.pack(side="left", padx=4)
        ttk.Label(row0, text="Tab:").pack(side="left", padx=(12, 0))
        self.tab_var = tk.StringVar(value="video_top")
        ttk.Combobox(row0, textvariable=self.tab_var, state="readonly", width=10,
                     values=["video_top", "top", "trending", "random", "new", "hot"]
                     ).pack(side="left", padx=4)

        row1 = ttk.Frame(self.src_panel)
        row1.pack(fill="x", padx=4, pady=2)
        ttk.Label(row1, text="最大数量:").pack(side="left")
        self.max_var = tk.StringVar(value="100")
        ttk.Entry(row1, textvariable=self.max_var, width=6).pack(side="left", padx=4)
        ttk.Label(row1, text="并发数:").pack(side="left", padx=(12, 0))
        self.concurrency_var = tk.StringVar(value="5")
        ttk.Entry(row1, textvariable=self.concurrency_var, width=5).pack(side="left", padx=4)
        ttk.Label(row1, text="滚动次数:").pack(side="left", padx=(12, 0))
        self.scrolls_var = tk.StringVar(value="20")
        ttk.Entry(row1, textvariable=self.scrolls_var, width=5).pack(side="left", padx=4)
        self.headless_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="无头模式", variable=self.headless_var).pack(side="left", padx=(12, 0))

        row2 = ttk.Frame(self.src_panel)
        row2.pack(fill="x", padx=4, pady=2)
        ttk.Label(row2, text="内容链接:").pack(side="left")
        self.content_url_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.content_url_var).pack(side="left", fill="x", expand=True, padx=4)

    def _build_file_panel(self) -> None:
        """构建文件路径面板内容。"""
        row = ttk.Frame(self.file_panel)
        row.pack(fill="x", padx=4, pady=4)
        self.file_label_var = tk.StringVar(value="URL 列表文件:")
        ttk.Label(row, textvariable=self.file_label_var, width=14).pack(side="left")
        self.file_path_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.file_path_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="浏览...", command=self._browse_file).pack(side="left", padx=2)

    # ================================================================
    # 模式切换
    # ================================================================
    def _on_mode_change(self, *_args) -> None:
        mode = self.mode_var.get()
        # 先隐藏两个面板
        self.src_panel.pack_forget()
        self.file_panel.pack_forget()

        if mode == "source":
            self.src_panel.pack(fill="x", pady=3, before=self.net_grp)
            self.file_label_var.set("URL 列表文件:")
        elif mode == "batch_urls":
            self.file_panel.pack(fill="x", pady=3, before=self.net_grp)
            self.file_label_var.set("URL 列表文件:")
        elif mode == "retry_failed":
            self.file_panel.pack(fill="x", pady=3, before=self.net_grp)
            self.file_label_var.set("失败清单文件:")

    # ================================================================
    # 配置
    # ================================================================
    def _load_config_to_ui(self) -> None:
        proxy = self._cfg.get("proxy", "")
        if proxy:
            self.proxy_var.set(proxy)

    def _save_config_from_ui(self) -> None:
        proxy = self.proxy_var.get().strip()
        if proxy:
            cfg_module.save_config(proxy=proxy)

    # ================================================================
    # 回调
    # ================================================================
    def _browse_file(self) -> None:
        mode = self.mode_var.get()
        if mode == "batch_urls":
            path = filedialog.askopenfilename(
                title="选择 URL 列表文件",
                filetypes=[("文本文件", "*.txt"), ("全部", "*.*")],
            )
        else:
            path = filedialog.askopenfilename(
                title="选择失败清单文件",
                filetypes=[("JSON 文件", "*.json"), ("全部", "*.*")],
            )
        if path:
            self.file_path_var.set(path)

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_var.set(path)

    def _open_output(self) -> None:
        path = self.output_var.get().strip()
        if path and os.path.isdir(path):
            os.startfile(path)
        elif os.path.isdir(str(IMAGES_DIR)):
            os.startfile(str(IMAGES_DIR))

    def _auto_detect_proxy(self) -> None:
        self._log("[代理] 正在自动探测...")
        self.proxy_status_var.set("探测中...")
        t = threading.Thread(target=self._do_auto_detect, daemon=True)
        t.start()

    def _do_auto_detect(self) -> None:
        proxy = cfg_module.detect_proxy()
        if proxy:
            self.queue.put((MSG_LOG, f"[代理] 探测到: {proxy}"))
            self.root.after(0, lambda: self.proxy_var.set(proxy))
            self.root.after(0, lambda: self.proxy_status_var.set("已探测到"))
            self._save_config_from_ui()
        else:
            self.queue.put((MSG_LOG, "[代理] 未探测到可用代理"))
            self.root.after(0, lambda: self.proxy_status_var.set("未找到"))

    def _test_proxy(self) -> None:
        proxy = self.proxy_var.get().strip()
        if not proxy:
            self._log("[代理] 请先填写代理地址")
            return
        reachable = cfg_module.proxy_reachable(proxy)
        if reachable:
            self._log(f"[代理] {proxy} — 可达")
            self.proxy_status_var.set("可达")
        else:
            self._log(f"[代理] {proxy} — 不可达")
            self.proxy_status_var.set("不可达")

    def _open_cut_gui(self) -> None:
        cut_gui_path = PROJECT_DIR / "tools" / "cut_gui.py"
        if not cut_gui_path.exists():
            messagebox.showerror("错误", f"视频拼接工具未找到:\n{cut_gui_path}")
            return
        try:
            subprocess.Popen([sys.executable, str(cut_gui_path)],
                             cwd=str(PROJECT_DIR / "tools"))
        except Exception as e:
            messagebox.showerror("错误", f"无法启动拼接工具:\n{e}")

    # ================================================================
    # 开始 / 停止
    # ================================================================
    def _start(self) -> None:
        if self.running:
            return

        args = self._collect_args()
        if args is None:
            return

        self._save_config_from_ui()

        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.progress.config(value=0)
        self._clear_log()
        self._set_status("正在启动...")
        self.stats_var.set("准备中...")
        self.time_var.set("")

        # 安装日志 Handler
        self._log_handler = QueueLogHandler(self.queue)
        logging.getLogger("mj_scraper").addHandler(self._log_handler)

        # 启动工作线程
        self.worker = WorkerThread(self.queue, args)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
        self._log("[用户] 已请求停止...")
        self._set_status("已请求停止")
        self.stop_btn.config(state="disabled")

    def _collect_args(self) -> dict | None:
        """收集界面参数，校验后返回 dict；失败返回 None 并弹窗。"""
        mode = self.mode_var.get()
        args: dict = {"mode": mode}

        # 通用
        args["proxy"] = self.proxy_var.get().strip() or None
        try:
            args["retries"] = int(self.retries_var.get() or 3)
            args["rate_limit"] = float(self.rate_var.get() or 0)
            args["speed_limit"] = float(self.speed_var.get() or 0)
        except ValueError:
            messagebox.showwarning("参数错误", "重试/速率/速度限制必须为数字")
            return None

        if mode == "source":
            args["source"] = self.source_var.get()
            try:
                args["max_images"]  = int(self.max_var.get() or 100)
                args["concurrency"] = int(self.concurrency_var.get() or 5)
                args["scrolls"]     = int(self.scrolls_var.get() or 20)
            except ValueError:
                messagebox.showwarning("参数错误", "最大数量/并发/滚动次数必须为整数")
                return None
            args["tab"]         = self.tab_var.get()
            args["headless"]    = self.headless_var.get()
            args["content_url"] = self.content_url_var.get().strip()
            args["url"]         = self.content_url_var.get().strip()
            args["mj_index"]    = 0

        elif mode in ("batch_urls", "retry_failed"):
            path = self.file_path_var.get().strip()
            if not path:
                messagebox.showwarning("路径为空", "请先选择文件")
                return None
            if not os.path.isfile(path):
                messagebox.showwarning("文件不存在", f"文件不存在:\n{path}")
                return None
            key = "batch_urls_path" if mode == "batch_urls" else "retry_failed_path"
            args[key] = path
            try:
                args["concurrency"] = int(self.concurrency_var.get() or 5)
            except ValueError:
                messagebox.showwarning("参数错误", "并发数必须为整数")
                return None

        return args

    # ================================================================
    # 队列轮询（UI 线程）
    # ================================================================
    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == MSG_PROGRESS:
                    self.progress.config(value=int(msg[1] * 1000))
                elif kind == MSG_STATUS:
                    self._set_status(msg[1])
                elif kind == MSG_LOG:
                    self._log(msg[1])
                elif kind == MSG_STATS:
                    s = msg[1]
                    elapsed = s.get("elapsed", 0)
                    m, sec = divmod(int(elapsed), 60)
                    self.stats_var.set(
                        f"获取: {s.get('fetched', 0)}  |  "
                        f"已下载: {s.get('downloaded', 0)}  |  "
                        f"跳过: {s.get('skipped', 0)}  |  "
                        f"失败: {s.get('errors', 0)}"
                    )
                    self.time_var.set(f"用时: {m:02d}:{sec:02d}")
                elif kind == MSG_DONE:
                    self.progress.config(value=1000)
                    s = msg[1]
                    elapsed = s.get("elapsed", 0)
                    m, sec = divmod(int(elapsed), 60)
                    self.stats_var.set(
                        f"获取: {s.get('fetched', 0)}  |  "
                        f"已下载: {s.get('downloaded', 0)}  |  "
                        f"跳过: {s.get('skipped', 0)}  |  "
                        f"失败: {s.get('errors', 0)}"
                    )
                    self.time_var.set(f"总用时: {m:02d}:{sec:02d}")
                    self._set_status("完成")
                    self._log(
                        f"[完成] 总 {s.get('fetched', 0)} → "
                        f"下载 {s.get('downloaded', 0)}, "
                        f"跳过 {s.get('skipped', 0)}, "
                        f"失败 {s.get('errors', 0)}, "
                        f"耗时 {elapsed:.1f}s"
                    )
                    messagebox.showinfo(
                        "完成",
                        f"下载完成\n\n"
                        f"获取: {s.get('fetched', 0)}\n"
                        f"已下载: {s.get('downloaded', 0)}\n"
                        f"跳过: {s.get('skipped', 0)}\n"
                        f"失败: {s.get('errors', 0)}\n"
                        f"耗时: {elapsed:.1f}s",
                    )
                    self._finish()
                elif kind == MSG_ERROR:
                    self._set_status("出错")
                    self._log(f"[错误] {msg[1]}")
                    messagebox.showerror("错误", msg[1])
                    self._finish()
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._poll_queue)

    def _finish(self) -> None:
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if self._log_handler:
            logging.getLogger("mj_scraper").removeHandler(self._log_handler)
            self._log_handler = None

    # ================================================================
    # UI 小工具
    # ================================================================
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _log(self, text: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")


# ====================================================================
# 入口
# ====================================================================
def main() -> None:
    root = tk.Tk()
    MidjourneyVideoGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
