# midjourney_video

> 仓库：<https://gitee.com/siwpan/midjourney_video> ｜ 许可：MIT（见 [LICENSE](LICENSE)）

Midjourney 视频下载工具 —— 用 Playwright 浏览器抓取 Midjourney Explore 页面视频并下载，附 CLI 统一启动器与 GUI。

## 语言 / 类型
- Python（Playwright 网络爬虫 + GUI + CLI）

## 目录要点
- `run.py` —— CLI 统一启动器（`midjourney_video` / `midjourney_video_url` 两种源，支持重试/限速/断点续传/批量 URL）
- `gui.py` —— 图形界面（插件面板，可导出 URL 列表）
- `config.py` —— 配置；`config.json` —— 配置数据
- `sources/` —— 抓取源实现；`tools/` —— 工具脚本
- `images/` / `meta/` / `midjourney_video/` —— 下载输出目录

## 运行方式与依赖
- 运行：`python run.py --source midjourney_video --max-images 50`
- 依赖：`playwright`（需安装浏览器内核）、`httpx`、`httpx[socks]`、`python-socks`。详见 `requirements.txt`。

## 测试状态
- `py_compile` 语法检查通过（9 个 .py 文件）。
- 需联网 + Playwright 浏览器，本次仅编译未运行。
