#!/usr/bin/env python3
"""无头自检：配置持久化/合并、代理地址归一与连通性判定、下载器命名与断点去重。

不联网、不启动 Playwright、不创建 GUI。
运行：python selfcheck.py
"""
import json
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def fail(msg):
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        import config
        from sources.base import Downloader, ImageTask
    except Exception as exc:  # pragma: no cover
        fail(f"导入项目模块失败: {exc}")

    # ---------- 1. 配置读写：合并、过滤未知键、不污染默认值 ----------
    original_path = config.CONFIG_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config.CONFIG_PATH = Path(tmp) / "config.json"
            config.save_config(
                proxy="http://127.0.0.1:7890",
                keys={"unsplash": "abc", "bogus": "should-be-dropped"},
            )
            loaded = config.load_config()
            if loaded["proxy"] != "http://127.0.0.1:7890":
                fail(f"代理配置往返失败: {loaded['proxy']!r}")
            if loaded["keys"]["unsplash"] != "abc":
                fail("API Key 往返失败")
            if "bogus" in loaded["keys"] or loaded["keys"]["pexels"] != "":
                fail("未知 Key 未过滤或默认 Key 缺失")
            if config.DEFAULTS["proxy"] != "" or config.DEFAULTS["keys"]["unsplash"] != "":
                fail("load_config 污染了模块级 DEFAULTS")

            # 只含部分字段的配置文件应回退默认值
            config.CONFIG_PATH.write_text(
                json.dumps({"proxy": "socks5://127.0.0.1:1080"}), encoding="utf-8"
            )
            partial = config.load_config()
            if partial["proxy"] != "socks5://127.0.0.1:1080":
                fail("部分配置读取失败")
            if partial["keys"]["flickr"] != "" or partial["proxy_sources"]["zdopen"]["app_id"] != "":
                fail("缺失字段未回退默认值")
    finally:
        config.CONFIG_PATH = original_path

    # ---------- 2. 代理归一 + TCP 可达性 ----------
    if config._normalize(" 127.0.0.1:7890 ") != "http://127.0.0.1:7890":
        fail("_normalize 未补全 http:// 或未去空白")
    if config._normalize("socks5://127.0.0.1:1080") != "socks5://127.0.0.1:1080":
        fail("_normalize 改写了已有 scheme")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        live_url = f"http://127.0.0.1:{server.getsockname()[1]}"
        if config._check_reachable(live_url) != live_url:
            fail("存活代理端口未被判定为可达")
    finally:
        server.close()

    dead_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dead_probe.bind(("127.0.0.1", 0))
    dead_port = dead_probe.getsockname()[1]
    dead_probe.close()
    if config._check_reachable(f"http://127.0.0.1:{dead_port}") != "":
        fail("已关闭端口被误判为可达")
    if config._check_reachable("not-a-url") != "":
        fail("非法代理地址应返回空串")

    # ---------- 3. 下载器：已有文件去重 + 文件名稳定/尺寸参数 ----------
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        (output_dir / "existing.jpg").write_bytes(b"stub")
        downloader = Downloader(output_dir, retries=1)
        if "existing.jpg" not in downloader.downloaded:
            fail("断点续传未识别输出目录中的已有文件")

        task = ImageTask(url="https://cdn.example.com/pic.PNG?w=800&h=600", source="midjourney")
        first = downloader._make_filename(task)
        second = downloader._make_filename(task)
        if first != second:
            fail("同名任务的文件名不稳定")
        if not first.startswith("midjourney_") or not first.endswith(".png"):
            fail(f"文件名格式不符: {first}")
        if "_800x600" not in first:
            fail(f"URL 尺寸参数未纳入文件名: {first}")
        if downloader._guess_ext("https://x/y.WEBP") != ".webp":
            fail("扩展名识别大小写不敏感失败")
        if downloader._guess_ext("https://x/y/noext") != ".jpg":
            fail("无扩展名 URL 未回退 .jpg")
        fallbacks = downloader._fallbacks("https://x/y/pic.jpg")
        if not any(item.endswith(".png") for item in fallbacks):
            fail(f"格式回退链未包含 .png: {fallbacks}")

    print(
        "selfcheck ok: config.json 读写与默认值合并（过滤未知 Key）、"
        "代理地址归一与存活/关闭端口判定、下载器已有文件去重与含尺寸参数的文件名"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
