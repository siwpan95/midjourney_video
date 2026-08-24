"""
多源图片爬虫 · 统一引擎
=======================
把原先分散在各源 *.py 中的「请求 → 解析 → 字段映射 → 下载」逻辑抽取为一份通用引擎，
各图片源仅需一份 JSON 配置（见 sources/json/*.json）即可被驱动。

JSON 配置约定的最小结构：

  {
    "name": "bing",
    "driver": "http",            // http | browser
    "needs_proxy": false,        // GFW 封锁需代理
    "needs_key": false,          // 需 API Key（无 key 时回退到 alt）
    "request": {
      "method": "GET",           // GET | POST
      "url": "https://...?q={query}&first={page}",
      "headers": {...},
      "params": {...},           // GET/POST 查询参数（支持 {模板} 变量）
      "body": {...},             // POST JSON body（支持 {模板} 变量）
      "skip_empty_params": true, // 模板替换后为空的参数自动丢弃
      "pagination": {...}        // 翻页策略
    },
    "parse": {
      "format": "json",          // json | html
      "list_path": "data",       // json：取列表的 dotted 路径
      "merge_keys": [...],       // json：合并若干 dict 的值为一个列表
      "stop_when_empty": true,
      "url_extract": {...},      // html：正则提取 URL 列表
      "block_extract": {...},    // html：按块提取 (url, 字段)
      "url_transform": [...]     // html：对提取出的 URL 追加变换
    },
    "item": {                    // 逐条字段映射 -> ImageTask
      "url":   {...extractor...},
      "title": {...},
      ...
    }
  }

模板变量（在 url/params/body 中可用 {var} 引用）：
  query / pages / tags / rating / limit / key / mode / order / sorting
  eff_tags / purity / nsfw_flag / browsing / picsum_query
  google_pages / pinterest_pages / tab / size / scrolls / headless
  page / cursor（翻页循环内自动注入）

extractor 形态（item 字段的值）：
  {"const": "固定值 或 {模板}"}            // 常量（支持模板）
  {"path": "a.b.0.c"}                      // 从当前条目按 dotted 路径取值
  {"template": "https://x/{id}"}           // 用条目/ctx 字段拼 URL
  {"default": [ext, ext]}                  // 取第一个非空
  {"path": "...", "fallback": ext}         // 空则回退
  {"path": "...", "filter": {"is_image": true}}  // 不满足条件则视为空
  {"path": "...", "transform": [{"op": ...}]}     // 值变换
  "纯字符串"                                // 等同于 const

transform op：
  regex_replace {pattern, repl}
  append_query {value}        // 追加查询串
  lower / upper
  to_int {default:0}

request_by_mode：按 ctx.mode 覆盖 request/parse（如 unsplash search/random）。
alt：needs_key 且无 key 时整体替换 request/parse/item（keyless 兜底）。
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import copy
import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Optional

import httpx

from .base import SourceBase, ImageTask, SourceStats, Downloader, log


# ───────────────────────────── 工具函数 ─────────────────────────────
def _subst(template: str, mapping: dict) -> str:
    """替换 {var} 占位符；未知占位符保持原样。"""
    if "{" not in template:
        return template

    def repl(m: "re.Match") -> str:
        key = m.group(1)
        if key in mapping and mapping[key] is not None:
            return str(mapping[key])
        return m.group(0)

    return re.sub(r"\{(\w+)\}", repl, template)


def _subst_any(value, mapping: dict):
    """递归对 dict/list/str 做模板替换（非字符串原样返回）。"""
    if isinstance(value, str):
        return _subst(value, mapping)
    if isinstance(value, dict):
        return {k: _subst_any(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst_any(v, mapping) for v in value]
    return value


def _get_path(obj, path: str):
    """dotted 路径取值，支持数字索引（如 assets.0.image_url）。"""
    if obj is None or not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _to_int(v, default: int = 0) -> int:
    if v is None or v == "":
        return default
    try:
        if isinstance(v, str):
            v = v.strip().replace(",", "")
        return int(float(v))
    except (ValueError, TypeError):
        return default


def _check_filter(flt: dict, value, ctx: dict) -> bool:
    if not flt:
        return True
    if "is_image" in flt and flt["is_image"]:
        if not isinstance(value, str):
            return False
        s = value.lower().split("?")[0]
        if s.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")):
            return True
        if s.startswith("https://i.redd.it/") or s.startswith("https://preview.redd.it/"):
            return True
        return False
    return True


def _apply_transforms(value, transforms: list, ctx: dict):
    for t in (transforms or []):
        op = t.get("op")
        if value is None:
            break
        if op == "regex_replace":
            if isinstance(value, str):
                value = re.sub(t.get("pattern", ""), t.get("repl", ""), value)
        elif op == "append_query":
            value = str(value) + t.get("value", "")
        elif op == "lower":
            value = str(value).lower()
        elif op == "upper":
            value = str(value).upper()
        elif op == "to_int":
            value = _to_int(value, t.get("default", 0))
    return value


def _apply_extractor(ext, item: dict, ctx: dict):
    """解析单个字段的 extractor，返回 Python 值。"""
    if ext is None:
        return ""
    if isinstance(ext, (str, int, float, bool)):
        if isinstance(ext, str) and "{" in ext:
            return _subst(ext, ctx)
        return ext
    if not isinstance(ext, dict):
        return ""

    if "const" in ext:
        v = ext["const"]
        if isinstance(v, str) and "{" in v:
            return _subst(v, ctx)
        return v
    if "template" in ext:
        flat = dict(item) if isinstance(item, dict) else {}
        return _subst(ext["template"], {**ctx, **flat})
    if "default" in ext:
        for sub in ext["default"]:
            v = _apply_extractor(sub, item, ctx)
            if v not in (None, "", []):
                return v
        return ""
    if "path" in ext:
        v = _get_path(item, ext["path"])
        if "filter" in ext and not _check_filter(ext["filter"], v, ctx):
            v = None
        if (v is None or v == "") and "fallback" in ext:
            v = _apply_extractor(ext["fallback"], item, ctx)
        if v not in (None, ""):
            v = _apply_transforms(v, ext.get("transform"), ctx)
        return v if v is not None else ""
    return ""


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ───────────────────────────── 统一源 ─────────────────────────────
class JsonSource(SourceBase):
    """由 JSON 配置驱动的图片源（http 或 browser）。"""

    def __init__(self, cfg: dict, output_root, concurrency=5, proxy=None,
                 max_images=100, retries=3, rate_limit=0.0, speed_limit=0.0,
                 ctx: Optional[dict] = None, keys: Optional[dict] = None):
        self.cfg = cfg
        self.name = cfg["name"]
        self.needs_proxy = bool(cfg.get("needs_proxy", False))
        self.needs_browser = bool(cfg.get("needs_browser", cfg.get("driver") == "browser"))
        self.ctx = ctx or {}
        self.keys = keys or {}
        # 空字符串代理视为「无代理」，否则 httpx 会抛 "Unknown scheme for proxy URL"
        self.proxy = proxy or None
        super().__init__(output_root, concurrency, proxy, max_images,
                         retries, rate_limit, speed_limit)
        self._client = None
        self._last_json = None

    # ── 公共：决定当前生效的 request/parse/item ──
    def _active(self) -> dict:
        """返回 {request, parse, item}，已处理 alt 与 request_by_mode。"""
        if self.cfg.get("needs_key") and not self.ctx.get("key"):
            alt = self.cfg.get("alt")
            if alt:
                if not getattr(self, "_alt_warned", False):
                    log.warning(
                        f"  [{self.name}] 未提供 API Key，回退到网页抓取(alt)；"
                        f"若该源已启用反爬(Anubis/Cloudflare 等)，可能返回 0 结果，"
                        f"请到「源专属设置」填写 Key。"
                    )
                    self._alt_warned = True
                return {
                    "request": alt.get("request", {}),
                    "parse": alt.get("parse", {"format": "json"}),
                    "item": alt.get("item", {}),
                }
        req = copy.deepcopy(self.cfg.get("request", {}))
        parse = copy.deepcopy(self.cfg.get("parse", {"format": "json"}))
        item = copy.deepcopy(self.cfg.get("item", {}))
        rbym = self.cfg.get("request_by_mode")
        if rbym:
            mv = self.ctx.get("mode", "")
            if mv in rbym:
                blk = rbym[mv]
                req = _deep_merge(req, blk.get("request", {}))
                parse = _deep_merge(parse, blk.get("parse", {}))
        return {"request": req, "parse": parse, "item": item}

    # ── HTTP 客户端 ──
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0, follow_redirects=True, proxy=(self.proxy or None),
                trust_env=False,  # 禁用系统代理环境变量/Windows 系统代理，代理只来自显式填写
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

    async def _request(self, req: dict, url: str, params: dict, headers: dict,
                       body: any = None):
        client = self._get_client()
        method = req.get("method", "GET").upper()
        body_val = body if body is not None else req.get("body")
        if method == "POST":
            return await client.post(url, json=body_val, params=params, headers=headers)
        return await client.get(url, params=params, headers=headers)

    async def _curl_get(self, url: str, params: dict, headers: dict):
        """用 curl 子进程拉取页面（绕过 Cloudflare 对 Python httpx 的 JA3 拦截）。

        返回 (status_code, text)。失败/超时/缺 curl 返回 (0, "")。
        适用场景：部分站点（如 pixabay）对 Python 客户端下发托管质询，
        curl 的 TLS 指纹可直接通过；JSON 配置里设 "fetch": "curl" 即可启用。
        """
        import subprocess
        from urllib.parse import urlencode
        args = [
            "curl", "-s", "-L", "--max-time", "45", "-A",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "-w", "\n__STATUS__%{http_code}",
        ]
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode(params)
        args.append(url)
        for k, v in headers.items():
            if v:
                args += ["-H", f"{k}: {v}"]
        if self.proxy:
            args += ["-x", self.proxy]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _ = await proc.communicate()
        except Exception as e:
            log.warning(f"  [{self.name}] curl 调用失败（curl 未安装？）: {e}")
            return (0, "")
        text = out.decode("utf-8", "ignore")
        if "\n__STATUS__" in text:
            body, _, tail = text.rpartition("\n__STATUS__")
            try:
                status = int(tail.strip())
            except ValueError:
                status = 0
            return (status, body)
        return (0, text)

    # ── 单 URL 抓取（供二级「详情页」抓取复用，避免重复构造模板 URL）──
    async def _fetch_url(self, url: str, active: dict, params: dict = None,
                         headers: dict = None):
        """抓取任意给定 URL（详情页 / 直链），复用 curl 或 httpx 通道。"""
        req = active["request"]
        if headers is None:
            headers = dict(req.get("headers", {}))
        headers = {k: v for k, v in headers.items() if v not in (None, "")}
        if self.cfg.get("fetch") == "curl":
            return await self._curl_get(url, params or {}, headers)
        body = _subst_any(req.get("body"), self.ctx) if req.get("body") else None
        try:
            r = await self._request(req, url, params or {}, headers, body)
        except Exception as e:
            log.warning(f"  [{self.name}] 请求失败: {e}")
            return (0, "")
        return r.status_code, r.text

    # ── 二级抓取：搜索页抽出的 item.url 实为「详情页链接」，
    #    逐条抓取详情页，再用 parse.detail 子配置抽出最终媒体 URL ──
    async def _resolve_detail_links(self, items: list, active: dict, max_images: int) -> list:
        detail_cfg = active["parse"].get("detail")
        if not detail_cfg:
            return items
        req = active["request"]
        headers = dict(req.get("headers", {}))
        delay = detail_cfg.get("delay")
        out = []
        seen_detail = set()
        for it in items:
            if len(out) >= max_images:
                break
            durl = it.get("url") if isinstance(it, dict) else str(it)
            if not durl or durl in seen_detail:
                continue
            seen_detail.add(durl)
            status, text = await self._fetch_url(durl, active, headers=headers)
            if status != 200 or not text:
                continue
            out.extend(self._extract_html(text, detail_cfg, durl))
            if delay:
                await asyncio.sleep(float(delay))
        return out

    # ── 翻页计数 / 步进 ──
    def _resolve_int(self, val, default=1) -> int:
        if val is None:
            return default
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            s = _subst(val, self.ctx).strip()
            if s == "":
                return default
            try:
                return int(float(s))
            except ValueError:
                return default
        return default

    def _page_count(self, pag: dict) -> int:
        cv = pag.get("count_var")
        if cv:
            return max(1, self._resolve_int(self.ctx.get(cv, 1), 1))
        pp = pag.get("per_page")
        if pp:
            per = self._resolve_int(pp, 1)
            if per > 0:
                return max(1, math.ceil(self.max_images / per))
        return pag.get("count", 1)

    # ── 单页抓取 + 解析 ──
    async def _fetch_page(self, active: dict, page_val=None, cursor=None,
                          override_query=None) -> list:
        req = active["request"]
        parse = active["parse"]
        mapping = dict(self.ctx)
        if page_val is not None:
            mapping["page"] = page_val
        if cursor is not None:
            mapping["cursor"] = cursor
        if override_query is not None:
            mapping["query"] = override_query

        url = _subst(req.get("url", ""), mapping)
        if not url or not url.startswith("http"):
            log.warning(f"  [{self.name}] 无效 URL: '{url[:80] if url else '(空)'}' — 请指定 --url")
            return []
        pag = req.get("pagination", {}) or {}
        params = _subst_any(req.get("params", {}), mapping)
        headers = dict(req.get("headers", {}))
        # 注入翻页参数
        if page_val is not None and pag.get("param"):
            params[pag["param"]] = page_val
        if cursor is not None and pag.get("param"):
            params[pag["param"]] = cursor
        # 丢弃空参数 / 空请求头
        if req.get("skip_empty_params"):
            params = {k: v for k, v in params.items() if v not in (None, "")}
            headers = {k: v for k, v in headers.items() if v not in (None, "")}
        # ── 实际抓取：curl 子进程（绕过 Cloudflare 对 Python httpx 的 JA3 拦截）或 httpx ──
        if self.cfg.get("fetch") == "curl":
            status, text = await self._curl_get(url, params, headers)
        else:
            body = _subst_any(req.get("body"), mapping) if req.get("body") else None
            try:
                r = await self._request(req, url, params, headers, body)
            except Exception as e:
                log.warning(f"  [{self.name}] 请求失败: {e}")
                return []
            status, text = r.status_code, r.text

        if status != 200:
            log.warning(f"  [{self.name}] HTTP {status} ({url[:70]})")
            return []

        if parse.get("format") == "json":
            try:
                self._last_json = json.loads(text)
            except Exception:
                self._last_json = None
                return []
            self._maybe_log_api_error(self._last_json)
            return self._extract_json(self._last_json, parse)
        return self._extract_html(text, parse, url)

    # ── JSON API 错误信封日志（避免静默「无任务」）──
    def _maybe_log_api_error(self, data):
        if not isinstance(data, dict):
            return
        msg = data.get("message") or data.get("error") or data.get("errorMessage")
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            msg = "; ".join(str(e) for e in errs)
        elif isinstance(errs, str) and errs:
            msg = errs
        if msg:
            log.warning(f"  [{self.name}] API 返回错误: {msg}")
            return
        if data.get("isSuccess") is False:
            code = data.get("errorCode")
            log.warning(f"  [{self.name}] API 返回 isSuccess=false"
                        + (f" (errorCode={code})" if code is not None else ""))
        elif data.get("errorCode") not in (None, 0, 1, "", "0", "1", "200"):
            log.warning(f"  [{self.name}] API 返回 errorCode={data.get('errorCode')}"
                        + (f" message={data.get('message')}" if data.get("message") else ""))

    # ── JSON 解析 ──
    def _extract_json(self, data, parse: dict) -> list:
        list_path = parse.get("list_path") or ""
        if list_path:
            items = _get_path(data, list_path)
        else:
            items = data
        if items is None:
            return []
        merge_keys = parse.get("merge_keys")
        if merge_keys:
            out = []
            src = items if isinstance(items, dict) else (data or {})
            for k in merge_keys:
                v = src.get(k) if isinstance(src, dict) else None
                if isinstance(v, dict):
                    out.extend(v.values())
                elif isinstance(v, list):
                    out.extend(v)
            items = out
        if not isinstance(items, list):
            return []
        return list(items)

    # ── HTML 解析 ──
    def _extract_html(self, html: str, parse: dict, page_url: str = "") -> list:
        ue = parse.get("url_extract")
        if ue:
            urls: list[str] = []
            for pat in ue.get("patterns", []):
                try:
                    urls.extend(re.findall(pat, html))
                except Exception as e:
                    log.warning(f"  [{self.name}] 正则错误: {e}")
            post = ue.get("post_replace") or {}
            for k, v in post.items():
                urls = [u.replace(k, v) for u in urls]
            if ue.get("url_decode"):
                from urllib.parse import unquote
                urls = [unquote(u) for u in urls]
            urls = [u.strip() for u in urls if u and u.strip()]
            # 相对 URL 解析为绝对 URL
            if ue.get("url_resolve") and page_url:
                from urllib.parse import urljoin, urlparse
                base = page_url
                # 尝试从 HTML 中提取 <base href="...">
                base_m = re.search(r'<base[^>]+href\s*=\s*["\']([^"\']+)', html, re.I)
                if base_m:
                    base = urljoin(page_url, base_m.group(1))
                urls = [urljoin(base, u) for u in urls]
            # 过滤掉 data: URI 和过短 URL
            urls = [u for u in urls if u and not u.lower().startswith("data:") and len(u) > 8]
            return [{"url": u} for u in urls]

        be = parse.get("block_extract")
        if be:
            flags = re.S if "S" in (be.get("block_flags", "") or "") else 0
            out = []
            for block in re.findall(be.get("block", ""), html, flags):
                title_m = re.search(be.get("title", r"<title>(.*?)</title>"), block, re.S)
                title = title_m.group(1).strip() if title_m else ""
                for u in re.findall(be.get("url", ""), block):
                    out.append({"url": u, "title": title})
            return out
        return []

    # ── 字段映射 → ImageTask ──
    def _build_tasks(self, raw_items: list, tasks: list, seen: set) -> list:
        item_map = self._active()["item"]
        ifilter = self.cfg.get("item_filter")
        for raw in raw_items:
            if not isinstance(raw, dict):
                raw = {"url": raw}
            # 条目级过滤（如 picsum 作者过滤）
            if ifilter:
                fv = _get_path(raw, ifilter.get("path", ""))
                wanted = self.ctx.get(ifilter.get("contains", ""), "")
                if wanted and isinstance(fv, str) and wanted.lower() not in fv.lower():
                    continue
            url = _apply_extractor(item_map.get("url", {}), raw, self.ctx)
            # 通用 URL 变换（json / html 同源；如 flickr 的 _m→_b、pexels/unsplash 升级分辨率）
            ut = self._active()["parse"].get("url_transform")
            if ut and url:
                url = _apply_transforms(url, ut, self.ctx)
            if not url or url in seen:
                continue
            seen.add(url)
            meta_ext = item_map.get("meta")
            meta = {}
            if isinstance(meta_ext, dict):
                for mk, me in meta_ext.items():
                    meta[mk] = _apply_extractor(me, raw, self.ctx)
            t = ImageTask(
                url=url, source=self.name,
                title=str(_apply_extractor(item_map.get("title", {}), raw, self.ctx)),
                author=str(_apply_extractor(item_map.get("author", {}), raw, self.ctx)),
                width=_to_int(_apply_extractor(item_map.get("width", {}), raw, self.ctx)),
                height=_to_int(_apply_extractor(item_map.get("height", {}), raw, self.ctx)),
                referer=str(_apply_extractor(item_map.get("referer", {}), raw, self.ctx)),
                meta=meta,
            )
            tasks.append(t)
            if len(tasks) >= self.max_images:
                break
        return tasks

    # ── 主流程：HTTP 源 ──
    async def fetch_tasks(self, max_images: int) -> list[ImageTask]:
        active = self._active()
        req = active["request"]
        parse = active["parse"]
        pag = req.get("pagination", {}) or {"type": "none"}
        ptype = pag.get("type", "none")

        tasks: list[ImageTask] = []
        seen: set = set()
        client = self._get_client()
        try:
            if ptype == "none":
                items = await self._fetch_page(active)
                if parse.get("detail"):
                    items = await self._resolve_detail_links(items, active, max_images)
                tasks = self._build_tasks(items, tasks, seen)
            elif ptype == "page":
                count = self._page_count(pag)
                start = self._resolve_int(pag.get("start"), 1)
                step = self._resolve_int(pag.get("step"), 1)
                for i in range(count):
                    if len(tasks) >= max_images:
                        break
                    page_val = start + i * step
                    items = await self._fetch_page(active, page_val=page_val)
                    if not items and pag.get("stop_when_empty", True):
                        break
                    if parse.get("detail"):
                        items = await self._resolve_detail_links(items, active, max_images)
                    tasks = self._build_tasks(items, tasks, seen)
                    if self._stop_page(pag, page_val):
                        break
                    # 翻页间隔（秒）：缓解部分站点（如 pixabay 视频）对高频请求的 403 限流
                    if i < count - 1:
                        delay = pag.get("delay")
                        if delay:
                            await asyncio.sleep(float(delay))
            elif ptype == "cursor":
                cursor = None
                max_pages = pag.get("max_pages", 50)
                for _ in range(max_pages):
                    if len(tasks) >= max_images:
                        break
                    items = await self._fetch_page(active, cursor=cursor)
                    if not items and pag.get("stop_when_empty", True):
                        break
                    tasks = self._build_tasks(items, tasks, seen)
                    cursor = self._next_cursor(pag)
                    if not cursor:
                        break
            elif ptype == "list":
                queries = self._list_queries(pag)
                for q in queries:
                    if len(tasks) >= max_images:
                        break
                    items = await self._fetch_page(active, override_query=q)
                    if parse.get("detail"):
                        items = await self._resolve_detail_links(items, active, max_images)
                    tasks = self._build_tasks(items, tasks, seen)
        finally:
            await client.aclose()
            self._client = None
        return tasks[:max_images]

    def _stop_page(self, pag: dict, page_val: int) -> bool:
        sp = pag.get("stop_path")
        if sp and isinstance(self._last_json, dict):
            val = _get_path(self._last_json, sp)
            if isinstance(val, (int, float)) and page_val >= val:
                return True
        return False

    def _next_cursor(self, pag: dict):
        cp = pag.get("cursor_path")
        if cp and isinstance(self._last_json, dict):
            v = _get_path(self._last_json, cp)
            return v if v else None
        return None

    def _list_queries(self, pag: dict) -> list:
        q = self.ctx.get("query", "")
        if q:
            return [q]
        return list(pag.get("default_queries", []))

    # ── 浏览器源（Midjourney 等 / 通用浏览器 HTML 抓取）──
    async def run(self) -> SourceStats:
        driver = self.cfg.get("driver")
        if driver == "browser":
            # driver:"browser" 既可能是 MJ(JSON 监听) 也可能是通用 HTML 抓取
            if self._is_browser_html():
                return await self._run_browser_html()
            return await self._run_browser()
        if driver == "browser_html":
            # 显式声明用浏览器抓 HTML 的源（如 google / pinterest，被 Cloudflare/
            # 同意页挡住，httpx 取不到内容，但真实浏览器可以通过）
            return await self._run_browser_html()
        # http 驱动源：若 needs_key 且无 key、其 alt 是 HTML 正则提取（如 unsplash），
        # 则改用浏览器抓取以绕过 Anubis/Cloudflare 这类需 JS 的验证页。
        if self._alt_wants_browser_html():
            return await self._run_browser_html()
        return await super().run()

    def _is_browser_html(self) -> bool:
        parse = self.cfg.get("parse", {})
        return bool(parse.get("format") == "html" and parse.get("url_extract"))

    def _alt_wants_browser_html(self) -> bool:
        if not (self.cfg.get("needs_key") and not self.ctx.get("key")):
            return False
        alt = self.cfg.get("alt") or {}
        parse = alt.get("parse", {})
        return bool(parse.get("format") == "html" and parse.get("url_extract"))

    async def _run_browser(self) -> SourceStats:
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            log.error(f"  [{self.name}] 浏览器驱动不可用: {e}")
            self.stats.start_time = time.time()
            self.stats.end_time = time.time()
            self._summary()
            return self.stats

        b = self.cfg.get("browser", {})
        cdn = b.get("cdn", "")
        size_suffix = b.get("size_suffix", {})
        suffix = size_suffix.get(self.ctx.get("size", "thumb"), size_suffix.get("thumb", ""))
        build = b.get("build", {})
        explore = b.get("explore_url", "")
        tab = b.get("tab", self.ctx.get("tab", "top"))  # browser 配置可覆盖默认 tab
        scrolls = self._resolve_int(self.ctx.get("scrolls"), 20)
        headless = bool(self.ctx.get("headless", True))
        variants = build.get("variants", 4)
        skip_type = b.get("skip_type")

        self.stats.start_time = time.time()
        log.info(f"[{self.name}] (浏览器) tab={tab} size={self.ctx.get('size')} max={self.max_images}")

        # 指定内容链接源：未提供链接则直接跳过（不退回 explore）
        if self.cfg.get("require_content_url"):
            if not (self.ctx.get("content_url") or "").strip():
                log.warning(
                    f"  源 [{self.name}] 未提供内容链接（content_url 为空），跳过。"
                    f"请在「源专属设置 → 目标 URL」填写 Midjourney 链接。"
                )
                self.stats.end_time = time.time()
                self._summary()
                return self.stats

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu"],
            )
            ctx_args: dict = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
                "viewport": {"width": 1920, "height": 1080},
                "locale": "en-US",
            }
            if self.proxy:
                ctx_args["proxy"] = {"server": self.proxy}
            context = await browser.new_context(**ctx_args)
            page = await context.new_page()

            # 注入 stealth 脚本（降低 Cloudflare 检测率）
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
            """)

            jobs: dict = {}
            api_hits = [0]  # 记录 API 响应命中次数

            async def on_resp(response):
                # 仅处理 midjourney 域名下的响应；从中抽取任意 job 数据
                url = str(response.url)
                if "midjourney.com" not in url:
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                api_hits[0] += 1
                if api_hits[0] <= 5:
                    log.info(f"  响应: {url[:80]} status={response.status}")
                # 从任意 JSON（列表/字典，含 /api/explore 与单 job 详情）中收集 job
                self._collect_jobs(data, jobs)

            page.on("response", on_resp)

            # ── 指定内容链接 ──
            # 若提供则把它作为目标 URL 直接进入原有爬取管线；为空则走原 explore 流程。
            # 不新增独立分支，所有行为（Cloudflare 等待 / 滚动 / 抽取 / 构建任务）与原来一致。
            content_url = (self.ctx.get("content_url") or "").strip()
            parsed_idx = None
            if content_url:
                target = content_url
                log.info(f"  指定内容链接: {target}")
                # 解析 ?index=N（1-based）→ 0-based 变体下标（仅用于限定变体，不改变爬取逻辑）
                from urllib.parse import urlparse, parse_qs
                idx = parse_qs(urlparse(content_url).query).get("index")
                if idx:
                    try:
                        parsed_idx = max(0, int(idx[0]) - 1)
                    except Exception:
                        parsed_idx = None
            else:
                target = f"{explore}?tab={tab}"

            goto_err = None
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                goto_err = e
                log.warning(f"  goto 异常: {e}")
            log.info(f"  页面标题: {await page.title()}")
            await asyncio.sleep(6)
            # 导航未成功（代理不可达 / DNS / 被墙 / 拦截）→ 直接结束，避免无意义的滚动空转
            if not page.url or page.url.startswith(("chrome-error://", "about:blank", "data:")):
                if goto_err and "PROXY" in str(goto_err).upper():
                    log.error(
                        "  代理连接失败（ERR_PROXY_CONNECTION_FAILED）：代理不可达。"
                        "请检查代理地址/端口，或确认本地代理客户端（clash/v2ray 等）已启动。"
                    )
                else:
                    log.error(
                        f"  页面未能加载（{page.url}）：可能是网络被墙或 Cloudflare 拦截，"
                        f"请检查代理 / 网络后重试。"
                    )
                self.stats.end_time = time.time()
                self._summary()
                return self.stats
            # Cloudflare challenge 检测 + 轮询等待
            cf_detected = False
            for cf_attempt in range(15):
                title = await page.title()
                if "moment" not in title.lower() and "challenge" not in title.lower():
                    break
                if not cf_detected:
                    log.warning("  Cloudflare challenge 检测到，等待自动通过...")
                    cf_detected = True
                await asyncio.sleep(4)
                # 每 3 轮尝试刷新页面
                if cf_attempt > 0 and cf_attempt % 3 == 0:
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
            if cf_detected:
                title = await page.title()
                log.info(f"  Cloudflare 等待结束，页面标题: {title}")

            # 原有滚动加载逻辑（对单条内容链接同样无害，不额外分支）
            log.info(f"  滚动加载 (最多 {scrolls} 次)…")
            for i in range(scrolls):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5)
                if len(jobs) >= self.max_images * 3:
                    log.info("    任务充足，停止滚动")
                    break
            await asyncio.sleep(3)
            log.info(f"  API 响应命中: {api_hits[0]} 次")
            log.info(f"  捕获 {len(jobs)} jobs")
            if not jobs:
                log.warning(f"  未捕获到任何 job；页面最终 URL: {page.url}")
                content = await page.content()
                log.info(f"  页面内容长度: {len(content)} 字节")
            log.info(f"  CDN URL 模板: {build.get('url_template','?')}")

            # 变体下标：GUI/CLI 的 mj_index 优先，其次取链接中的 ?index=N
            mj_index = self.ctx.get("mj_index") or 0
            try:
                mj_index = int(mj_index)
            except Exception:
                mj_index = 0
            variant_index = (mj_index - 1) if mj_index and mj_index > 0 else parsed_idx

            tasks = self._build_browser_tasks(
                jobs, cdn, suffix, build, variants, skip_type, variant_index=variant_index)
            self.stats.fetched = len(tasks)
            log.info(f"  下载任务: {len(tasks)}")

            if tasks:
                completed = await self._browser_download(page, tasks)
                self._save_meta(completed)
                failed = [self._task_to_failed(t) for t in tasks if not t.downloaded]
                if failed:
                    self._save_failed(failed)
            else:
                log.warning(f"  源 [{self.name}] 无任务")

            await browser.close()

        self.stats.end_time = time.time()
        self._summary()
        return self.stats

    async def _run_browser_html(self) -> SourceStats:
        """通用浏览器 HTML 抓取：开 Chromium 过 Anubis/Cloudflare/Consent 等验证页，
        取渲染后 HTML 跑现有 url_extract / block_extract 正则，再复用下载逻辑。

        触发方式：
          - 源配置 "driver": "browser_html"（如 google / pinterest 这类搜索页被
            Cloudflare / 同意页挡住、httpx 取不到内容，但真实浏览器可以通过的源）；
          - 或 needs_key 源未填 key、且其 alt 是 HTML 正则提取（如 unsplash）。
        支持 request.pagination 翻页（page 类型）与无限滚动（none 类型 + 可选 scroll）。
        """
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            log.error(f"  [{self.name}] 浏览器驱动不可用: {e}")
            self.stats.start_time = time.time()
            self.stats.end_time = time.time()
            self._summary()
            return self.stats

        active = self._active()
        req = active["request"]
        parse = active["parse"]
        pag = req.get("pagination", {}) or {"type": "none"}
        ptype = pag.get("type", "none")
        mapping0 = dict(self.ctx)

        base_url = _subst(req.get("url", ""), mapping0)
        if not base_url or not base_url.startswith("http"):
            log.warning(f"  [{self.name}] 无效 URL: '{base_url[:80] if base_url else '(空)'}'")
            self.stats.start_time = time.time()
            self.stats.end_time = time.time()
            self._summary()
            return self.stats

        self.stats.start_time = time.time()
        log.info(f"[{self.name}] (浏览器HTML) base={base_url[:70]} max={self.max_images} pag={ptype}")

        # 翻页计划（page 类型）
        if ptype == "page":
            count = self._page_count(pag)
            start = self._resolve_int(pag.get("start"), 1)
            step = self._resolve_int(pag.get("step"), 1)
            pages = [start + i * step for i in range(count)]
        else:
            pages = [None]

        # 滚动次数：none 类型默认多滚加载懒加载图片；page 类型少滚；
        # 也可在 request.scroll 或 ctx scrolls 里显式指定。
        if req.get("scroll") is not None:
            scrolls = self._resolve_int(req.get("scroll"), 0)
        elif ptype == "none":
            scrolls = self._resolve_int(self.ctx.get("scrolls"), 10)
        else:
            scrolls = 3

        tasks: list = []
        seen: set = set()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=bool(self.ctx.get("headless", True)),
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-gpu"],
            )
            ctx_args: dict = {
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "viewport": {"width": 1920, "height": 1080},
                "locale": "en-US",
            }
            if self.proxy:
                ctx_args["proxy"] = {"server": self.proxy}
            context = await browser.new_context(**ctx_args)
            page = await context.new_page()
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
            """)

            for pidx, pv in enumerate(pages):
                if len(tasks) >= self.max_images:
                    break
                page_map = dict(mapping0)
                if pv is not None:
                    page_map["page"] = pv
                url = _subst(req.get("url", ""), page_map)

                # 应用 request.params / 翻页参数 / skip_empty_params，拼成完整 URL
                params = _subst_any(req.get("params", {}), page_map)
                if pv is not None and pag.get("param"):
                    params[pag["param"]] = pv
                if req.get("skip_empty_params"):
                    params = {k: v for k, v in params.items()
                              if v not in (None, "")}
                if params:
                    from urllib.parse import urlencode
                    q = urlencode({k: str(v) for k, v in params.items()})
                    sep = "&" if "?" in url else "?"
                    url = url + sep + q
                # 请求头（浏览器上下文已有 UA，这里补充自定义头，如 Accept）
                headers = {k: v for k, v in req.get("headers", {}).items()
                           if v not in (None, "")}

                if not url or not url.startswith("http"):
                    log.warning(f"  [{self.name}] 无效分页 URL: '{url[:80] if url else '(空)'}'")
                    continue

                log.info(f"  第 {pidx + 1}/{len(pages)} 页: {url[:70]}")
                try:
                    # commit：响应一返回就返回，避免长时间停在 domcontentloaded/load；
                    # 真正的反爬等待交给下面的轮询循环。
                    await page.goto(
                        url, wait_until="commit", timeout=30000,
                        headers=headers if headers else None,
                    )
                except Exception as e:
                    log.warning(f"  goto 异常: {e}")

                # 等待过反爬验证页(Anubis/Cloudflare/Consent)并渲染出内容
                content = ""
                for _ in range(30):
                    await asyncio.sleep(2)
                    try:
                        content = await page.content()
                    except Exception:
                        content = ""
                    if not self._is_antibot_page(content):
                        break

                title = await page.title()
                log.info(f"  页面标题: {title[:60]}")

                if not content:
                    log.error(
                        f"  页面未能加载：可能是网络被墙/代理不可达或拦截，"
                        f"请检查代理/网络后重试。"
                    )
                    if ptype == "page":
                        break  # 第一页都加载不出，后续页无意义
                    continue

                # 滚动加载懒加载图片后取最新 HTML
                if scrolls and scrolls > 0:
                    log.info(f"  滚动加载 ({scrolls} 次)…")
                    for _ in range(scrolls):
                        try:
                            await page.evaluate(
                                "window.scrollTo(0, document.body.scrollHeight)")
                        except Exception:
                            pass
                        await asyncio.sleep(1.2)
                        if ptype == "none" and len(tasks) >= self.max_images:
                            break
                    try:
                        content = await page.content()
                    except Exception:
                        pass

                raw = self._extract_html(content, parse, page.url)
                log.info(f"  本页解析出 {len(raw)} 条候选")
                self._build_tasks(raw, tasks, seen)
                log.info(f"  累计任务: {len(tasks)}")
                if ptype == "page" and not raw and pag.get("stop_when_empty", True):
                    log.info("  本页无结果，停止翻页")
                    break
                # 翻页间隔（缓解限流）
                delay = pag.get("delay")
                if delay and pidx < len(pages) - 1:
                    await asyncio.sleep(float(delay))

            await browser.close()

        tasks = tasks[:self.max_images]
        self.stats.fetched = len(tasks)
        log.info(f"  下载任务: {len(tasks)}")
        if tasks:
            dl = Downloader(
                self.output_dir, self.concurrency, self.proxy,
                self.retries, self.rate_limit, self.speed_limit,
            )
            completed = await dl.download_batch(tasks, self.stats)
            await dl.close()
            self._save_meta(completed)
            failed = [self._task_to_failed(t) for t in tasks if not t.downloaded]
            if failed:
                self._save_failed(failed)
        else:
            log.warning(f"  源 [{self.name}] 无任务")

        self.stats.end_time = time.time()
        self._summary()
        return self.stats

    @staticmethod
    def _is_antibot_page(content: str) -> bool:
        """粗略判断当前页面是否处于反爬验证页(Anubis/Cloudflare/Consent 等)。"""
        if not content:
            return False
        low = content.lower()
        markers = (
            "not a bot", "making sure you", "just a moment", "verify you are human",
            "attention required", "checking your browser", "enable javascript and cookies",
            "performance & security by cloudflare", "why am i seeing this",
            "before you continue to google", "consent.google.com", "we use cookies",
            "are you a robot", "please verify you are a human", "cf-browser-verification",
            "ray id", "ddos-guard", "security check",
        )
        return any(m in low for m in markers)

    def _collect_jobs(self, data, jobs: dict):
        """从 API 响应 JSON 中抽取 job 对象，喂给原爬取管线。

        兼容：
          - /api/explore 直接返回的列表
          - 单条 job 详情返回的字典（或 {"job": {...}} 这类一层包装）
        仅把「像 job」的字典加入：必须有 id，且带 items 列表 / prompt / job_type /
        video / type 之一，以区别于 items 内的变体子对象，避免污染任务列表。
        """
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict):
                    self._maybe_add_job(it, jobs)
            return
        if isinstance(data, dict):
            if self._maybe_add_job(data, jobs):
                return
            # 可能被子对象包装（如 {"job": {...}}），只下探一层，不再深入 items 内部
            for v in data.values():
                if isinstance(v, dict) and self._maybe_add_job(v, jobs):
                    return

    @staticmethod
    def _maybe_add_job(it: dict, jobs: dict) -> bool:
        jid = it.get("id")
        if not (isinstance(jid, str) and jid):
            return False
        if (isinstance(it.get("items"), list) or it.get("prompt")
                or it.get("job_type") or it.get("video") or it.get("video_url")
                or it.get("type")):
            if jid not in jobs:
                jobs[jid] = it
            return True
        return False

    def _build_browser_tasks(self, jobs, cdn, suffix, build, variants, skip_type,
                             variant_index=None) -> list:
        tasks = []
        tmpl = build.get("url_template", "{cdn}/{id}/0_{vi}{suffix}")
        constructed_fallback = 0
        for it in jobs.values():
            if skip_type and it.get("type") == skip_type:
                continue
            jid = it.get("id", "")
            items = it.get("items", []) or []
            n = max(len(items), 4)
            if variant_index is not None:
                if not (0 <= variant_index < n):
                    continue
                vis = [variant_index]
            else:
                vis = range(min(n, variants))
            prompt_obj = it.get("prompt", {}) or {}
            decoded = prompt_obj.get("decodedPrompt", []) or []
            prompt_text = " ".join(s.get("content", "") for s in decoded if isinstance(s, dict))
            author = it.get("username_v2", "") or it.get("display_name", "")
            for vi in vis:
                # 优先用 API 返回的直接 URL，否则用模板构造
                item_data = items[vi] if vi < len(items) and isinstance(items[vi], dict) else {}
                url = (
                    item_data.get("image_url") or item_data.get("imageUrl") or
                    item_data.get("url") or item_data.get("path") or
                    item_data.get("image_path") or item_data.get("src") or
                    item_data.get("video_url") or item_data.get("video") or ""
                )
                if url:
                    width = _to_int(item_data.get("width", 0))
                    height = _to_int(item_data.get("height", 0))
                else:
                    url = tmpl.format(cdn=cdn, id=jid, vi=vi, suffix=suffix)
                    width = _to_int(it.get("width"))
                    height = _to_int(it.get("height"))
                    constructed_fallback += 1
                tasks.append(ImageTask(
                    url=url, source=self.name, title=prompt_text, author=author,
                    width=width, height=height,
                    meta={"job_id": jid, "variant": vi, "job_type": it.get("job_type", "")},
                ))
        if constructed_fallback > 0:
            log.info(f"  CDN URL 模板构造: {constructed_fallback}/{len(tasks)} 个")
        return tasks[:self.max_images]

    # 图片/视频后缀集合
    _MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".mp4", ".mov", ".webm", ".avi"}

    async def _browser_download(self, page, tasks: list) -> list:
        from pathlib import Path as _P
        sem = asyncio.Semaphore(self.concurrency)
        existing = {f.name for f in self.output_dir.iterdir() if f.is_file()}
        import hashlib as _hl

        async def one(t: ImageTask) -> ImageTask:
            async with sem:
                h = _hl.md5(t.url.encode()).hexdigest()[:12]
                # 从 URL 自动检测扩展名（不硬编码 .webp/.png）
                raw_ext = _P(t.url.split("?")[0]).suffix.lower()
                ext = raw_ext if raw_ext in self._MEDIA_EXT else ".png"
                fname = f"{self.name}_{h}{ext}"
                if fname in existing:
                    t.local_path = str(self.output_dir / fname)
                    t.downloaded = True
                    self.stats.skipped += 1
                    return t
                content = await self._page_fetch(page, t.url)
                if not content:
                    for fb in self._fallbacks(t.url):
                        content = await self._page_fetch(page, fb)
                        if content:
                            e = _P(fb.split("?")[0]).suffix.lower()
                            if e in self._MEDIA_EXT:
                                fname = f"{self.name}_{h}{e}"
                            break
                if content and len(content) > 300:
                    fp = self.output_dir / fname
                    fp.write_bytes(content)
                    t.local_path = str(fp)
                    t.downloaded = True
                    existing.add(fname)
                    self.stats.downloaded += 1
                    if self.stats.downloaded <= 5 or self.stats.downloaded % 50 == 0:
                        log.info(f"  [{self.stats.downloaded}] {fname}")
                else:
                    self.stats.errors += 1
                return t

        return await asyncio.gather(*[one(t) for t in tasks], return_exceptions=True)

    # 用 page.evaluate(fetch) 在已验证的浏览器 session 内下载（绕过 CDN Cloudflare）：
    _BROWSER_FETCH_JS = """async (url) => {
        try {
            const resp = await fetch(url, {
                headers: { 'Referer': 'https://www.midjourney.com/', 'Accept': '*/*' },
                redirect: 'follow',
            });
            if (!resp.ok) return {error: 'HTTP ' + resp.status};
            const buf = await resp.arrayBuffer();
            if (buf.byteLength < 300) return {error: 'too small: ' + buf.byteLength};
            // ArrayBuffer → base64
            const bytes = new Uint8Array(buf);
            let binary = '';
            for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
            return {ok: true, b64: btoa(binary), size: buf.byteLength};
        } catch(e) {
            return {error: e.message};
        }
    }"""

    @staticmethod
    async def _page_fetch(page, url: str):
        """在已认证的浏览器 page 内用 JS fetch() 下载图片，返回 bytes 或 None。"""
        try:
            result = await page.evaluate(JsonSource._BROWSER_FETCH_JS, url)
        except Exception as e:
            log.debug(f"  page.evaluate 异常 {url[:80]}: {e}")
            return None
        if isinstance(result, dict) and result.get("ok") and result.get("b64"):
            try:
                return _b64.b64decode(result["b64"])
            except Exception:
                return None
        if isinstance(result, dict) and result.get("error"):
            log.debug(f"  CDN fetch {url[:80]}: {result['error']}")
        return None

    @staticmethod
    def _fallbacks(url: str) -> list:
        base = url.split("?")[0]
        # 支持图片 + 视频扩展名
        candidates = [".png", ".webp", ".jpg", ".mp4", ".webm", ".mov"]
        return [re.sub(r"\.(png|webp|jpg|jpeg|gif|mp4|webm|mov|avi)(\?|$)", e, base)
                for e in candidates if re.sub(r"\.(png|webp|jpg|jpeg|gif|mp4|webm|mov|avi)(\?|$)", e, base) != base]

    @staticmethod
    def _task_to_failed(t: ImageTask) -> dict:
        return {
            "url": t.url, "referer": t.referer, "source": t.source,
            "title": t.title, "author": t.author,
            "width": t.width, "height": t.height, "meta": t.meta,
        }
