"""
cut.py - 视频拼接 / 合并工具

基于联网调研（FFmpeg Concatenate 官方 wiki、mpegflow / renderio 等实践指南），
本工具把原来仅支持 moviepy compose 的单函数脚本，增强为支持多种拼接策略的工具：

  1. ffmpeg-demuxer  : 最快，要求输入文件 编码/分辨率/帧率 完全一致，零重编码（流拷贝）。
                       命令: ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp4
  2. ffmpeg-filter   : 重编码拼接，可处理参数不一致的视频，并自动统一分辨率/帧率/采样率（最稳）。
                       使用 filter_complex 的 concat 滤镜 + scale/pad/setsar/fps/aformat。
  3. moviepy-compose : moviepy 把各片段缩放到最大尺寸后拼接（默认，依赖最少）。
  4. moviepy-chain   : moviepy 串联拼接，要求尺寸一致，速度更快。

增强点（相对原脚本）：
  - 自动释放资源（with / close），避免句柄泄漏导致后续文件无法写入。
  - 缺失文件 / 空列表检查，失败给出清晰报错。
  - 可选 normalize：拼接前统一分辨率、帧率、音频采样率。
  - 多策略分发 + 命令行参数（--method / --output / --width / --height / --fps）。
  - moviepy v1/v2 兼容导入。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

ProgressCallback = Callable[[float], None]  # 进度回调，参数 frac ∈ [0, 1]


# --------------------------------------------------------------------------- #
# 依赖兼容导入
# --------------------------------------------------------------------------- #
try:  # moviepy 1.x
    from moviepy.editor import VideoFileClip, concatenate_videoclips
except Exception:  # moviepy 2.x
    from moviepy import VideoFileClip, concatenate_videoclips

from proglog import ProgressBarLogger


def _ffmpeg_path() -> str | None:
    """返回可用的 ffmpeg 可执行文件名，未安装则返回 None。"""
    return shutil.which("ffmpeg")


def _total_duration(video_paths: list[str]) -> float:
    """用 moviepy 累加各片段时长（秒），供 ffmpeg 进度估算。"""
    total = 0.0
    clips = []
    try:
        for p in video_paths:
            c = VideoFileClip(p)
            clips.append(c)
            total += getattr(c, "duration", 0) or 0
    finally:
        for c in clips:
            try:
                c.close()
            except Exception:
                pass
    return total


# --------------------------------------------------------------------------- #
# moviepy 进度日志器：把 proglog 的进度转成 0~1 的回调
# --------------------------------------------------------------------------- #
class _ProgressLogger(ProgressBarLogger):
    def __init__(self, callback: ProgressCallback, *a, **k):
        super().__init__(*a, **k)
        self._cb = callback

    def bars_callback(self, bar, attr, value, old_value=None):
        if attr == "index":
            total = self.bars[bar]["total"]
            if total:
                self._cb(min(1.0, value / total))


def _run_ffmpeg_with_progress(
    cmd: list[str],
    total_duration: float = 0.0,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """
    运行 ffmpeg 命令并（可选）解析 stderr 中的 time= 输出以报告进度。
    进度 = 当前已处理时长 / 总时长。
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    # 单独线程读取 stderr，避免管道阻塞，同时把内容缓存下来用于报错
    err_lines: list[str] = []

    def _reader():
        assert proc.stderr is not None
        for line in proc.stderr:
            err_lines.append(line)
            if progress_callback is None or not total_duration:
                continue
            m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
            if m:
                h, mm, ss = (int(m.group(1)), int(m.group(2)), float(m.group(3)))
                t = h * 3600 + mm * 60 + ss
                progress_callback(min(1.0, t / total_duration))

    import threading
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    proc.wait()
    t.join()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 执行失败 (code={proc.returncode}):\n{''.join(err_lines)}")


# --------------------------------------------------------------------------- #
# 策略 1：FFmpeg concat demuxer（零重编码，要求参数完全一致）
# --------------------------------------------------------------------------- #
def concatenate_ffmpeg_demuxer(
    video_paths: list[str],
    output_path: str,
    progress_callback: ProgressCallback | None = None,
) -> None:
    if not _ffmpeg_path():
        raise RuntimeError("未找到 ffmpeg，无法使用 demuxer 策略（请安装 ffmpeg 或改用其它策略）。")
    if not video_paths:
        raise ValueError("video_paths 为空，没有可拼接的视频。")

    list_fd, list_path = tempfile.mkstemp(prefix="cut_concat_", suffix=".txt")
    try:
        with os.fdopen(list_fd, "w", encoding="utf-8") as f:
            for p in video_paths:
                if not os.path.exists(p):
                    raise FileNotFoundError(f"输入文件不存在: {p}")
                # 转义单引号，避免路径中的引号破坏 ffmpeg 列表解析
                safe = p.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
               "-i", list_path, "-c", "copy", output_path]
        if progress_callback:
            progress_callback(0.0)
        _run_ffmpeg_with_progress(cmd, _total_duration(video_paths), progress_callback)
        if progress_callback:
            progress_callback(1.0)
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 策略 2：FFmpeg concat filter（重编码，可归一化参数，最稳）
# --------------------------------------------------------------------------- #
def concatenate_ffmpeg_filter(
    video_paths: list[str],
    output_path: str,
    width: int | None = None,
    height: int | None = None,
    fps: int = 30,
    progress_callback: ProgressCallback | None = None,
) -> None:
    if not _ffmpeg_path():
        raise RuntimeError("未找到 ffmpeg，无法使用 filter 策略（请安装 ffmpeg 或改用 moviepy 策略）。")
    n = len(video_paths)
    if n == 0:
        raise ValueError("video_paths 为空，没有可拼接的视频。")

    inputs: list[str] = []
    parts: list[str] = []
    for i, p in enumerate(video_paths):
        if not os.path.exists(p):
            raise FileNotFoundError(f"输入文件不存在: {p}")
        inputs += ["-i", p]
        # 视频：缩放并居中填充到统一分辨率，设置像素比与帧率
        scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease" if width and height \
            else "scale='iw':'ih'"
        pad = f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2" if width and height \
            else "pad='iw':'ih':0:0"
        parts.append(
            f"[{i}:v]{scale},{pad},setsar=1,fps={fps}[v{i}];"
            f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{i}];"
        )

    # concat 滤镜输入 pad 按片段交错排列：pad0=[0:v], pad1=[0:a], pad2=[1:v]...
    # 必须逐片段 [v{i}][a{i}] 交错，不能先所有视频后所有音频
    seg_maps = "".join(f"[v{i}][a{i}]" for i in range(n))
    filter_complex = "".join(parts) + f"{seg_maps}concat=n={n}:v=1:a=1[outv][outa]"

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]
    if progress_callback:
        progress_callback(0.0)
    _run_ffmpeg_with_progress(cmd, _total_duration(video_paths), progress_callback)
    if progress_callback:
        progress_callback(1.0)


# --------------------------------------------------------------------------- #
# 策略 3/4：moviepy compose / chain
# --------------------------------------------------------------------------- #
def _resize(clip, size):
    w, h = size
    try:  # moviepy 2.x
        return clip.resized((w, h))
    except Exception:  # moviepy 1.x
        return clip.resize((w, h))


def concatenate_moviepy(
    video_paths: list[str],
    output_path: str,
    method: str = "compose",
    target_size: tuple[int, int] | None = None,
    target_fps: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    if not video_paths:
        raise ValueError("video_paths 为空，没有可拼接的视频。")

    clips = []
    try:
        for p in video_paths:
            if not os.path.exists(p):
                raise FileNotFoundError(f"输入文件不存在: {p}")
            clip = VideoFileClip(p)
            if target_size is not None:
                clip = _resize(clip, target_size)
            if target_fps is not None:
                clip = clip.with_fps(target_fps)
            clips.append(clip)

        final = concatenate_videoclips(clips, method=method)
        logger = _ProgressLogger(progress_callback) if progress_callback else "bar"
        final.write_videofile(output_path, codec="libx264", audio_codec="aac", logger=logger)
    finally:
        for c in clips:
            try:
                c.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
def concatenate_videos(
    video_paths: list[str],
    output_path: str,
    method: str = "moviepy-compose",
    width: int | None = None,
    height: int | None = None,
    fps: int = 30,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """
    拼接多个视频。

    :param video_paths: 视频路径列表，按时间顺序拼接
    :param output_path: 输出文件路径
    :param method: 拼接策略
        - "ffmpeg-demuxer" : 零重编码，要求输入参数完全一致（最快）
        - "ffmpeg-filter"  : 重编码并自动归一化参数（最稳，推荐）
        - "moviepy-compose": moviepy 缩放至最大尺寸拼接（默认，依赖最少）
        - "moviepy-chain"  : moviepy 串联拼接，要求尺寸一致
    :param width/height: ffmpeg-filter 归一化目标分辨率（None 表示保持原尺寸）
    :param fps: ffmpeg-filter 归一化目标帧率
    :param progress_callback: 进度回调，frac ∈ [0, 1]
    """
    method = method.lower()
    if method == "ffmpeg-demuxer":
        concatenate_ffmpeg_demuxer(video_paths, output_path, progress_callback)
    elif method == "ffmpeg-filter":
        concatenate_ffmpeg_filter(video_paths, output_path, width, height, fps, progress_callback)
    elif method == "moviepy-compose":
        concatenate_moviepy(video_paths, output_path, method="compose",
                             target_size=(width, height) if width and height else None,
                             target_fps=fps if width is None else None,
                             progress_callback=progress_callback)
    elif method == "moviepy-chain":
        concatenate_moviepy(video_paths, output_path, method="chain",
                             target_size=(width, height) if width and height else None,
                             target_fps=fps if width is None else None,
                             progress_callback=progress_callback)
    else:
        raise ValueError(f"不支持的拼接策略: {method}")


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="视频拼接 / 合并工具")
    parser.add_argument("videos", nargs="+", help="待拼接视频文件（按顺序）")
    parser.add_argument("-o", "--output", default="merged_timeline.mp4", help="输出文件路径")
    parser.add_argument(
        "-m", "--method", default="moviepy-compose",
        choices=["ffmpeg-demuxer", "ffmpeg-filter", "moviepy-compose", "moviepy-chain"],
        help="拼接策略（默认 moviepy-compose；ffmpeg-* 需另行安装 ffmpeg）",
    )
    parser.add_argument("--width", type=int, default=None, help="归一化宽度（仅 ffmpeg-filter）")
    parser.add_argument("--height", type=int, default=None, help="归一化高度（仅 ffmpeg-filter）")
    parser.add_argument("--fps", type=int, default=30, help="归一化帧率（默认 30）")
    args = parser.parse_args(argv)

    try:
        concatenate_videos(
            args.videos, args.output,
            method=args.method, width=args.width, height=args.height, fps=args.fps,
        )
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    print(f"[完成] 已拼接 {len(args.videos)} 个视频 -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
