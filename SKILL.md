---
name: midjourney-video
description: Maintain midjourney_video downloader (本 SKILL.md 所在仓库根). Playwright-based Midjourney video downloader.
agent_created: true
---

# midjourney_video 维护

## 概述
Playwright 抓取 Midjourney Explore 页面视频下载：CLI 统一启动器 + GUI，重试/限速/断点续传/批量 URL。

## 结构
- run.py：CLI（midjourney_video / midjourney_video_url 两种源）；gui.py：GUI
- sources/：抓取源；config.py + config.json
- images/ meta/ midjourney_video/：下载输出（资产目录，建议本地保留不进版本库）

## 常用命令
- F:\Python314\python.exe run.py --source midjourney_video --max-images 50

## 已知陷阱
- 需 Playwright 浏览器内核 + 联网，离线仅能 py_compile
