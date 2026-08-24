"""
多源图片爬虫 - 基础层
========================
定义共享的数据结构、下载器、元数据保存器。各图片源继承 SourceBase。

设计要点：
- 中国网络环境：Civitai/Lexica 被 GFW 封锁，Unsplash/Pexels/Bing 可达。
- 所有源统一实现 fetch_tasks() -> list[ImageTask]，由调度器驱动。
- 下载统一走 httpx（带代理支持）；Midjourney 因 Cloudflare 需浏览器通道，自行覆盖。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("mj_scraper")


@dataclass
class ImageTask:
    """单张图片下载任务（跨源统一结构）"""
    url: str = ""
    source: str = ""          # 来源名: midjourney / bing / unsplash ...
    title: str = ""           # prompt 或搜索词/描述
    author: str = ""          # 作者 / 出处
    width: int = 0
    height: int = 0
    local_path: str = ""      # 下载后本地路径
    downloaded: bool = False
    referer: str = ""          # 下载时用到的 Referer（绕过防盗链）
    meta: dict = field(default_factory=dict)  # 额外元数据


@dataclass
class SourceStats:
    fetched: int = 0
    downloaded: int = 0
    skipped: int = 0
    errors: int = 0
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def elapsed(self) -> float:
        return (self.end_time or time.time()) - self.start_time


class Downloader:
    """通用异步下载器（httpx，支持代理、断点续传、格式回退）"""

    # 支持的媒体扩展名（图片 + 视频 + 音频）。Pixabay 等综合性素材站也会产出音视频。
    MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp",
                  ".mp4", ".webm", ".mov", ".m4v",
                  ".mp3", ".wav", ".ogg", ".m4a")

    def __init__(self, output_dir: Path, concurrency: int = 5, proxy: Optional[str] = None,
                 retries: int = 3, rate_limit: float = 0.0, speed_limit: float = 0.0):
        self.output_dir = output_dir
        self.concurrency = concurrency
        self.proxy = proxy
        self.retries = max(1, retries)
        self.rate_limit = rate_limit      # 请求/秒，0=不限
        self.speed_limit = speed_limit    # KB/s，0=不限
        self.downloaded: set[str] = set()
        self.failed: list[dict] = []     # 失败 URL 清单（含 referer/meta，供重试导出）
        self._client: Optional[httpx.AsyncClient] = None
        # 速率限制状态
        self._rate_lock: Optional[object] = asyncio.Lock() if rate_limit > 0 else None
        self._last_call: float = 0.0
        output_dir.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    def _load_existing(self):
        for f in self.output_dir.iterdir():
            if f.is_file() and f.suffix.lower() in self.MEDIA_EXTS:
                self.downloaded.add(f.name)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                proxy=self.proxy,
                trust_env=False,  # 禁用系统代理环境变量/Windows 系统代理，代理只来自显式填写
                limits=httpx.Limits(max_connections=self.concurrency + 5),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def download_batch(self, tasks: list[ImageTask], stats: SourceStats) -> list[ImageTask]:
        sem = asyncio.Semaphore(self.concurrency)

        async def one(t: ImageTask) -> ImageTask:
            async with sem:
                return await self._download(t, stats)

        results = await asyncio.gather(*[one(t) for t in tasks], return_exceptions=True)
        out = []
        for r in results:
            if isinstance(r, ImageTask):
                out.append(r)
            else:
                stats.errors += 1
        return out

    async def _throttle_rate(self):
        """限制每秒请求数（rate_limit>0 时生效）"""
        if self._rate_lock is None:
            return
        async with self._rate_lock:
            gap = 1.0 / self.rate_limit
            now = time.monotonic()
            wait = self._last_call + gap - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def _download(self, task: ImageTask, stats: SourceStats) -> ImageTask:
        fname = self._make_filename(task)
        if fname in self.downloaded:
            task.local_path = str(self.output_dir / fname)
            task.downloaded = True
            stats.skipped += 1
            return task

        client = self._get_client()
        referer = task.referer or self._derive_referer(task.url)
        content = None
        for attempt in range(1, self.retries + 1):
            await self._throttle_rate()
            content = await self._try(client, task.url, referer)
            if not content:
                for fb in self._fallbacks(task.url):
                    content = await self._try(client, fb, referer)
                    if content:
                        ext = Path(fb.split("?")[0]).suffix.lower()
                        if ext in self.MEDIA_EXTS:
                            base = fname.rsplit(".", 1)[0]
                            fname = base + ext
                        break
            if content:
                break
            if attempt < self.retries:
                backoff = min(2 ** attempt, 10)   # 指数退避，上限 10s
                log.debug(f"  重试 {attempt}/{self.retries}（{backoff}s 后）: {task.url[:70]}")
                await asyncio.sleep(backoff)

        if content and len(content) > 300:
            fp = self.output_dir / fname
            fp.write_bytes(content)
            task.local_path = str(fp)
            task.downloaded = True
            self.downloaded.add(fname)
            stats.downloaded += 1
            if stats.downloaded <= 5 or stats.downloaded % 50 == 0:
                log.info(f"  [{stats.downloaded}] {fname}")
        else:
            stats.errors += 1
            self.failed.append({
                "url": task.url,
                "referer": referer,
                "source": task.source,
                "title": task.title,
                "author": task.author,
                "width": task.width,
                "height": task.height,
                "meta": task.meta,
            })
            log.debug(f"  失败（{self.retries}次重试后）: {task.url[:90]}")
        return task

    async def _try(self, client: httpx.AsyncClient, url: str, referer: str = "") -> Optional[bytes]:
        try:
            headers = {"Referer": referer} if referer else {}
            # 限速模式：流式读取并按 speed_limit 节流
            if self.speed_limit > 0:
                buf = bytearray()
                bytes_per_sec = self.speed_limit * 1024.0
                async with client.stream("GET", url, headers=headers) as r:
                    if r.status_code != 200:
                        return None
                    async for chunk in r.aiter_bytes(64 * 1024):
                        buf.extend(chunk)
                        await asyncio.sleep(len(chunk) / bytes_per_sec)
                return bytes(buf) if len(buf) > 300 else None
            r = await client.get(url, headers=headers)
            if r.status_code == 200 and len(r.content) > 300:
                return r.content
            return None
        except Exception:
            return None

    def _make_filename(self, task: ImageTask) -> str:
        """生成稳定文件名：源_哈希_序号.扩展名（含尺寸参数避免同名碰撞）"""
        h = hashlib.md5(task.url.encode()).hexdigest()[:12]
        ext = self._guess_ext(task.url)
        # 若 URL 带尺寸参数（如 bing tse 的 w=/h=），纳入哈希区分
        variant = ""
        if "?" in task.url:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(task.url).query)
            if "w" in q or "h" in q:
                variant = "_" + q.get("w", ["0"])[0] + "x" + q.get("h", ["0"])[0]
        return f"{task.source}_{h}{variant}{ext}"

    @staticmethod
    def _guess_ext(url: str) -> str:
        path = url.split("?")[0].lower()
        for e in Downloader.MEDIA_EXTS:
            if path.endswith(e):
                return e
        return ".jpg"

    @staticmethod
    def _fallbacks(url: str) -> list[str]:
        base = url.split("?")[0]
        alts = []
        for e in (".png", ".webp", ".jpg", ".jpeg"):
            a = re.sub(r"\.(png|webp|jpg|jpeg|gif|avif)(\?|$)", e, base)
            if a != base:
                alts.append(a)
        return alts

    @staticmethod
    def _derive_referer(url: str) -> str:
        """从图片 URL 推导 Referer 域名，绕过常见防盗链"""
        try:
            from urllib.parse import urlparse
            netloc = urlparse(url).netloc
            return f"https://{netloc}/" if netloc else ""
        except Exception:
            return ""


class SourceBase:
    """所有图片源的基类"""

    name: str = "base"
    # 是否需要浏览器（Playwright）通道
    needs_browser: bool = False
    # 是否被 GFW 封锁（需代理才能访问）
    needs_proxy: bool = False

    def __init__(
        self,
        output_root: Path,
        concurrency: int = 5,
        proxy: Optional[str] = None,
        max_images: int = 100,
        retries: int = 3,
        rate_limit: float = 0.0,
        speed_limit: float = 0.0,
    ):
        self.output_root = Path(output_root)
        self.concurrency = concurrency
        self.proxy = proxy
        self.max_images = max_images
        self.retries = retries
        self.rate_limit = rate_limit
        self.speed_limit = speed_limit
        self.output_dir = self.output_root / self.name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stats = SourceStats()

    # ── 子类必须实现 ──
    async def fetch_tasks(self, max_images: int) -> list[ImageTask]:
        raise NotImplementedError

    # ── 通用运行流程（httpx 下载）──
    async def run(self) -> SourceStats:
        self.stats.start_time = time.time()
        log.info(f"[{self.name}] 开始 (max={self.max_images}, proxy={self.proxy or '无'})")

        tasks = await self.fetch_tasks(self.max_images)
        self.stats.fetched = len(tasks)
        log.info(f"  获取任务: {len(tasks)}")

        if tasks:
            dl = Downloader(
                self.output_dir, self.concurrency, self.proxy,
                self.retries, self.rate_limit, self.speed_limit,
            )
            completed = await dl.download_batch(tasks, self.stats)
            await dl.close()
            self._save_meta(completed)
            # 失败 URL 清单导出（供 --retry-failed 重投）
            if dl.failed:
                self._save_failed(dl.failed)
        else:
            log.warning(f"  源 [{self.name}] 无任务")

        self.stats.end_time = time.time()
        self._summary()
        return self.stats

    # ── 元数据保存 ──
    def _save_meta(self, tasks: list[ImageTask]):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.output_root.parent / "meta" / f"{self.name}_{ts}.json"
        path.parent.mkdir(parents=True, exist_ok=True)

        images = []
        for t in tasks:
            if t.downloaded:
                images.append({
                    "source": t.source,
                    "url": t.url,
                    "title": t.title,
                    "author": t.author,
                    "width": t.width,
                    "height": t.height,
                    "filename": Path(t.local_path).name if t.local_path else "",
                    "local": t.local_path,
                    "meta": t.meta,
                })

        data = {
            "source": self.name,
            "timestamp": ts,
            "stats": asdict(self.stats),
            "images": images,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info(f"  元数据: {path} ({len(images)} imgs)")

    def _save_failed(self, failed: list[dict]):
        """导出失败 URL 清单到 meta/failed_<source>_<ts>.json，供 --retry-failed 重投"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.output_root.parent / "meta" / f"failed_{self.name}_{ts}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "source": self.name,
            "timestamp": ts,
            "count": len(failed),
            "tasks": failed,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.warning(f"  失败清单已导出: {path} ({len(failed)} 个 URL，可用 --retry-failed 重投)")

    def _summary(self):
        s = self.stats
        log.info(
            f"[{self.name}] 完成 {s.elapsed:.1f}s | "
            f"获取 {s.fetched} | 下载 {s.downloaded} | "
            f"跳过 {s.skipped} | 失败 {s.errors}"
        )
