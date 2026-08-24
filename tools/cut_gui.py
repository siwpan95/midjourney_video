"""
cut_gui.py - 视频拼接工具（GUI 版，带进度条）

基于 cut.py 的多策略拼接能力，提供图形界面：
  - 视频列表：添加 / 移除 / 上移 / 下移（决定拼接顺序）
  - 拼接策略：ffmpeg-demuxer / ffmpeg-filter / moviepy-compose / moviepy-chain
  - 归一化参数：分辨率(width×height)、帧率(fps)
  - 输出路径选择
  - 进度条 + 实时状态 + 日志
  - 后台线程执行，界面不卡顿

依赖：tkinter（Python 自带）、cut.py 同目录下的 concatenate_videos。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# 同目录导入核心拼接函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cut import concatenate_videos  # noqa: E402


# 进度/消息通过队列从工作线程传回 UI 线程
MSG_PROGRESS = "progress"   # (MSG_PROGRESS, frac 0~1)
MSG_STATUS = "status"       # (MSG_STATUS, text)
MSG_LOG = "log"             # (MSG_LOG, text)
MSG_DONE = "done"           # (MSG_DONE, output_path | None on error)
MSG_ERROR = "error"         # (MSG_ERROR, text)


class ConcatGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("视频拼接工具 (cut_gui)")
        self.root.geometry("720x560")
        self.root.resizable(True, True)

        self.video_paths: list[str] = []
        self.running = False
        self.queue: "queue.Queue[tuple]" = queue.Queue()

        self._build_widgets()
        self._poll_queue()

    # ------------------------------------------------------------------ #
    # 界面布局
    # ------------------------------------------------------------------ #
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True, **pad)

        # ---- 视频列表 ----
        list_frame = ttk.LabelFrame(frm, text="视频列表（从上到下 = 拼接顺序）")
        list_frame.pack(fill="both", expand=True, pady=4)

        self.listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED, height=10)
        self.listbox.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        scroll.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=scroll.set)

        btn_col = ttk.Frame(list_frame)
        btn_col.pack(side="right", fill="y", padx=4)
        ttk.Button(btn_col, text="添加", command=self._add_files).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="移除", command=self._remove).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="上移", command=lambda: self._move(-1)).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="下移", command=lambda: self._move(1)).pack(fill="x", pady=2)

        # ---- 参数行 ----
        opt = ttk.LabelFrame(frm, text="拼接参数")
        opt.pack(fill="x", pady=4)

        ttk.Label(opt, text="策略:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.method_var = tk.StringVar(value="moviepy-compose")
        self.method_cb = ttk.Combobox(
            opt, textvariable=self.method_var, state="readonly",
            values=["moviepy-compose", "moviepy-chain", "ffmpeg-filter", "ffmpeg-demuxer"],
        )
        self.method_cb.grid(row=0, column=1, sticky="w", padx=4, pady=4)
        self.method_cb.bind("<<ComboboxSelected>>", lambda e: self._sync_method_hint())

        self.method_hint = ttk.Label(opt, text="", foreground="gray")
        self.method_hint.grid(row=0, column=2, columnspan=3, sticky="w", padx=4)

        ttk.Label(opt, text="宽(px):").grid(row=1, column=0, sticky="e", padx=4)
        self.width_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.width_var, width=8).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(opt, text="高(px):").grid(row=1, column=2, sticky="e", padx=4)
        self.height_var = tk.StringVar()
        ttk.Entry(opt, textvariable=self.height_var, width=8).grid(row=1, column=3, sticky="w", padx=4)
        ttk.Label(opt, text="帧率(fps):").grid(row=1, column=4, sticky="e", padx=4)
        self.fps_var = tk.StringVar(value="30")
        ttk.Entry(opt, textvariable=self.fps_var, width=6).grid(row=1, column=5, sticky="w", padx=4)

        # ---- 输出 ----
        out = ttk.LabelFrame(frm, text="输出")
        out.pack(fill="x", pady=4)
        self.output_var = tk.StringVar(value=os.path.join(os.getcwd(), "merged_timeline.mp4"))
        ttk.Entry(out, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=4, pady=4)
        ttk.Button(out, text="浏览...", command=self._browse_output).pack(side="right", padx=4)

        # ---- 进度与状态 ----
        self.progress = ttk.Progressbar(frm, orient="horizontal", mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=6)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(frm, textvariable=self.status_var).pack(anchor="w")

        self.log = tk.Text(frm, height=6, state="disabled")
        self.log.pack(fill="both", expand=True, pady=4)

        # ---- 开始 ----
        self.start_btn = ttk.Button(frm, text="开始拼接", command=self._start)
        self.start_btn.pack(pady=6)

        self._sync_method_hint()
        self._log("提示：默认 moviepy-compose 无需 ffmpeg；ffmpeg-* 策略需另行安装 ffmpeg。")

    # ------------------------------------------------------------------ #
    # 交互回调
    # ------------------------------------------------------------------ #
    def _sync_method_hint(self) -> None:
        hints = {
            "moviepy-compose": "moviepy 缩放至最大尺寸拼接（默认，无需 ffmpeg）",
            "moviepy-chain": "moviepy 串联拼接，要求各片段尺寸一致",
            "ffmpeg-filter": "重编码并自动归一化参数（最稳，需 ffmpeg）",
            "ffmpeg-demuxer": "零重编码拼接，要求各片段参数完全一致（需 ffmpeg）",
        }
        self.method_hint.config(text=hints.get(self.method_var.get(), ""))

    def _add_files(self) -> None:
        files = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[("视频", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.m4v"), ("全部", "*.*")],
        )
        for f in files:
            if f not in self.video_paths:
                self.video_paths.append(f)
                self.listbox.insert("end", os.path.basename(f))
        self._log(f"已添加 {len(files)} 个文件，当前共 {len(self.video_paths)} 个。")

    def _remove(self) -> None:
        for idx in reversed(self.listbox.curselection()):
            self.listbox.delete(idx)
            del self.video_paths[idx]

    def _move(self, delta: int) -> None:
        sel = list(self.listbox.curselection())
        if not sel:
            return
        for idx in sel:
            nxt = idx + delta
            if 0 <= nxt < len(self.video_paths):
                self.video_paths[idx], self.video_paths[nxt] = self.video_paths[nxt], self.video_paths[idx]
                self.listbox.delete(idx)
                self.listbox.insert(nxt, os.path.basename(self.video_paths[nxt]))
                self.listbox.selection_set(nxt)
                idx = nxt

    def _browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存输出视频", defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv"), ("AVI", "*.avi"), ("全部", "*.*")],
        )
        if path:
            self.output_var.set(path)

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def _start(self) -> None:
        if self.running:
            return
        if not self.video_paths:
            messagebox.showwarning("提示", "请先添加至少一个视频文件。")
            return
        output = self.output_var.get().strip()
        if not output:
            messagebox.showwarning("提示", "请指定输出文件路径。")
            return

        method = self.method_var.get()
        width = self._int(self.width_var.get())
        height = self._int(self.height_var.get())
        if (width is None) != (height is None):
            messagebox.showwarning("提示", "宽度和高度需同时填写或同时留空。")
            return
        fps = self._int(self.fps_var.get()) or 30

        out_dir = os.path.dirname(output)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        self.running = True
        self.start_btn.config(state="disabled")
        self.progress.config(value=0)
        self._set_status(f"正在拼接（{method}）...")

        args = dict(
            video_paths=list(self.video_paths),
            output_path=output,
            method=method,
            width=width,
            height=height,
            fps=fps,
        )
        t = threading.Thread(target=self._worker, args=(args,), daemon=True)
        t.start()

    @staticmethod
    def _int(s: str) -> int | None:
        s = (s or "").strip()
        return int(s) if s else None

    def _worker(self, args: dict) -> None:
        def progress_callback(frac: float) -> None:
            self.queue.put((MSG_PROGRESS, frac))

        try:
            self.queue.put((MSG_LOG, f"开始拼接 {len(args['video_paths'])} 个视频 -> {args['output_path']}"))
            concatenate_videos(**args, progress_callback=progress_callback)
            self.queue.put((MSG_DONE, args["output_path"]))
        except Exception as e:  # noqa: BLE001
            self.queue.put((MSG_ERROR, str(e)))

    # ------------------------------------------------------------------ #
    # 队列轮询（UI 线程）
    # ------------------------------------------------------------------ #
    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == MSG_PROGRESS:
                    self.progress.config(value=int(msg[1] * 1000))
                    self._set_status(f"拼接中... {msg[1]*100:.0f}%")
                elif kind == MSG_STATUS:
                    self._set_status(msg[1])
                elif kind == MSG_LOG:
                    self._log(msg[1])
                elif kind == MSG_DONE:
                    self.progress.config(value=1000)
                    self._set_status("完成")
                    self._log(f"完成 -> {msg[1]}")
                    messagebox.showinfo("完成", f"视频已拼接完成：\n{msg[1]}")
                    self._finish()
                elif kind == MSG_ERROR:
                    self._set_status("失败")
                    self._log(f"[错误] {msg[1]}")
                    messagebox.showerror("失败", msg[1])
                    self._finish()
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._poll_queue)

    def _finish(self) -> None:
        self.running = False
        self.start_btn.config(state="normal")

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def main() -> None:
    root = tk.Tk()
    ConcatGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
