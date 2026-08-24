#!/usr/bin/env python3
"""
持久化配置（代理 + API Key）
=============================
把「代理地址」与「各源 API Key」存到脚本目录的 config.json，
GUI / CLI 启动时自动加载，避免每次重输。

附带本地代理自动探测：扫描常见科学上网端口，返回首个可连接的地址。

用法：
  from config import load_config, save_config, detect_proxy
  cfg = load_config()            # {"proxy": "...", "keys": {...}}
  save_config(proxy="socks5://127.0.0.1:1080",
              keys={"unsplash": "abc", "flickr": "def"})
  p = detect_proxy()             # "" 或 "socks5://127.0.0.1:1080"
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULTS: dict = {
    "proxy": "",
    "keys": {
        "unsplash": "",
        "pexels": "",
        "flickr": "",
        "wallhaven": "",
    },
    "proxy_sources": {
        "zdopen": {"app_id": "", "akey": ""},
        "zdaye": {"username": "", "password": ""},
    },
}

# 常见本地代理端口（Clash / v2rayN / SS / 系统代理 等）
PROXY_CANDIDATES: list[str] = [
    "socks5://127.0.0.1:1080",
    "http://127.0.0.1:7890",
    "http://127.0.0.1:8080",
    "http://127.0.0.1:8888",
    "socks5://127.0.0.1:10808",
    "http://127.0.0.1:10809",
    "socks5://127.0.0.1:7891",
]


def load_config() -> dict:
    """读取 config.json，缺失字段回退默认值（深拷贝避免污染 DEFAULTS）"""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data.get("proxy"), str):
                cfg["proxy"] = data["proxy"]
            if isinstance(data.get("keys"), dict):
                cfg["keys"].update({k: v for k, v in data["keys"].items()
                                    if k in DEFAULTS["keys"] and v})
            ps = data.get("proxy_sources")
            if isinstance(ps, dict):
                for svc, creds in ps.items():
                    if svc in cfg["proxy_sources"] and isinstance(creds, dict):
                        cfg["proxy_sources"][svc].update(
                            {k: v for k, v in creds.items() if v}
                        )
    except Exception:
        pass
    return cfg


def save_config(proxy: str | None = None, keys: dict | None = None) -> None:
    """合并写入 config.json（仅覆盖传入的非空项）"""
    cfg = load_config()
    if proxy:
        cfg["proxy"] = proxy.strip()
    if keys:
        for svc, val in keys.items():
            if svc in DEFAULTS["keys"] and val:
                cfg["keys"][svc] = val.strip()
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _check_reachable(url: str, timeout: float = 1.2) -> str:
    """若给定代理地址 TCP 可连则返回原串，否则返回空串。
    url 形如 socks5://127.0.0.1:1080 或 http://127.0.0.1:7890"""
    try:
        scheme, rest = url.split("://", 1)
        host, port_s = rest.rsplit(":", 1)
        port = int(port_s)
    except Exception:
        return ""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return url
    except Exception:
        return ""
    finally:
        try:
            s.close()
        except Exception:
            pass


def _proxy_from_env() -> str:
    """读取标准代理环境变量（HTTP_PROXY/HTTPS_PROXY 及其小写），返回首个可达的"""
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(key, "").strip()
        if v:
            # 仅保留 host:port 部分做连通性校验
            norm = v if "://" in v else "http://" + v
            hit = _check_reachable(norm)
            if hit:
                return norm
    return ""


def _proxy_from_system() -> str:
    """读取 Windows 系统代理（Internet Settings\\ProxyServer）。
    形如 '127.0.0.1:7890' 或 'http=127.0.0.1:7890;https=127.0.0.1:7890;socks=127.0.0.1:7891'。
    优先取 socks，其次 http/https。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        winreg.CloseKey(key)
        if not enabled or not server:
            return ""
        server = server.strip()
        # 多协议形式：scheme=host:port;...
        if "=" in server:
            parts = {}
            for seg in server.split(";"):
                if "=" in seg:
                    k, v = seg.split("=", 1)
                    parts[k.strip().lower()] = v.strip()
            for proto in ("socks", "http", "https"):
                if proto in parts:
                    return _check_reachable(_normalize(parts[proto]))
        else:
            return _check_reachable(_normalize(server))
    except Exception:
        pass
    return ""


def _normalize(endpoint: str) -> str:
    """把 '127.0.0.1:7890' 规范成 'http://127.0.0.1:7890'（端口扫描类默认用 http）"""
    endpoint = endpoint.strip()
    if "://" in endpoint:
        return endpoint
    return "http://" + endpoint


def detect_proxy(timeout: float = 1.2) -> str:
    """探测可用代理，优先级：环境变量 > Windows 系统代理 > 常见本地客户端端口。
    返回首个 TCP 可连的代理地址；无则返回空串。"""
    for fn in (_proxy_from_env, _proxy_from_system):
        hit = fn()
        if hit:
            return hit
    for p in PROXY_CANDIDATES:
        hit = _check_reachable(p, timeout)
        if hit:
            return hit
    return ""


_EP_CACHE: str | None = None  # 缓存自动探测结果，避免多源重复扫描端口


async def auto_fetch_proxy(prefer_socks: bool = False) -> str:
    """从免费代理源拉取并校验，返回首个可用代理 URL；失败返回空串。"""
    try:
        from proxy_fetcher import fetch_working_proxy
        p = await fetch_working_proxy(prefer_socks=prefer_socks)
        return p or ""
    except Exception:
        return ""


def proxy_reachable(url: str) -> bool:
    """校验代理地址 TCP 是否可连（不可达的死代理不应被使用）。"""
    return bool(_check_reachable(url))


def effective_proxy(explicit: str | None = None) -> str:
    """返回应使用的代理：显式 > 已存配置(需可达) > 自动探测。
    仅在探测到结果时缓存，避免把「暂未找到」固化，便于中途启动客户端后即时生效。
    注意：已存配置中的代理若不可达则跳过，退回自动探测 / 直连，
    避免一个死代理把所有源（含本可直连的 midjourney）卡死。"""
    if explicit:
        return explicit.strip()
    cfg = load_config()
    saved = (cfg.get("proxy") or "").strip()
    if saved and _check_reachable(saved):
        return saved
    global _EP_CACHE
    if _EP_CACHE and _check_reachable(_EP_CACHE):
        return _EP_CACHE
    p = detect_proxy()
    if p:
        _EP_CACHE = p
    return p
