#!/usr/bin/env python3
"""吸气声弱化工具 — GUI entrypoint (DSP lives in breath/)."""
import os
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import librosa
import numpy as np
import soundfile as sf
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib import rcParams

from breath import (
    VERSION,
    HOP_LENGTH,
    LEFT_APPEND_MS,
    RIGHT_APPEND_MS,
    MIN_MANUAL_DRAG_SEC,
    MIN_RESIZE_DRAG_SEC,
    PLAYHEAD_DRAW_INTERVAL_MS,
    HALF_TIME_MATCH_TOLERANCE_SEC,
    DEFAULT_DETECT_PARAMS,
    process_breath,
    load_app_config,
    save_app_config,
    event_log,
    format_diagnostics_text,
    build_output_path,
    write_output_mp3,
    load_actual_output_audio,
    render_output_audio,
    merge_time_ranges,
    subtract_time_ranges,
    time_ranges_to_samples,
    is_half_time_sample_segment,
    half_time_overlaps_in_range,
    split_segment_by_half_time,
    audio_buffers_share_content,
    finalize_rendered_output,
    merge_sample_segments,
)

# Backward-compatible private aliases (tests / older call sites).
_render_output_audio = render_output_audio
_finalize_rendered_output = finalize_rendered_output
_audio_buffers_share_content = audio_buffers_share_content
_merge_time_ranges = merge_time_ranges
_subtract_time_ranges = subtract_time_ranges
_time_ranges_to_samples = time_ranges_to_samples
_is_half_time_sample_segment = is_half_time_sample_segment
_half_time_overlaps_in_range = half_time_overlaps_in_range
_split_segment_by_half_time = split_segment_by_half_time
_merge_sample_segments = merge_sample_segments
_load_app_config = load_app_config
_save_app_config = save_app_config
_event_log = event_log
_format_diagnostics_text = format_diagnostics_text
_build_output_path = build_output_path
_write_output_mp3 = write_output_mp3
_load_actual_output_audio = load_actual_output_audio

rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False


class ColorButton(tk.Label):
    def __init__(self, master, text, bg, fg="white", command=None, **kwargs):
        super().__init__(master, text=text, bg="#d9d9d9", fg="#a3a3a3", padx=12, pady=4, relief="flat", cursor="arrow", **kwargs)
        self.normal_bg = bg
        self.normal_fg = fg
        self.command = command
        self._state = tk.DISABLED
        
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_click(self, e):
        if self._state == tk.DISABLED:
            return
        self.config(bg="#aaaaaa", state=tk.NORMAL)
        self.after(100, lambda: self.config(bg=self.normal_bg, state=tk.NORMAL) if self._state != tk.DISABLED else None)
        if self.command:
            self.command()
            
    def _on_enter(self, e):
        pass
            
    def _on_leave(self, e):
        pass

    def config(self, **kwargs):
        if "state" in kwargs:
            st = kwargs["state"]
            self._state = st
            if st == tk.DISABLED:
                super().config(bg="#d9d9d9", fg="#a3a3a3", cursor="arrow")
            else:
                super().config(bg=self.normal_bg, fg=self.normal_fg, cursor="hand2")
            kwargs.pop("state")
        if kwargs:
            super().config(**kwargs)

class BreathReducerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"吸气声弱化工具 v{VERSION}")
        self.root.geometry(f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
        self.root.resizable(True, True)
        self.app_config = load_app_config()

        self.input_path = ""
        self.output_path = ""
        self.source_audio = None
        self.limited_source_audio = None
        self.limited_playback_audio = None
        self.output_audio = None
        self.output_display_audio = None
        self.source_playback_audio = None
        self.output_playback_audio = None
        self.output_headroom_gain = 1.0
        self.output_timeline_segments = []
        self.output_has_post_processing = False
        # Single generation counter for process / rewrite / export-refresh / select_file.
        # Any new op bumps op_token so in-flight applies from other ops are dropped.
        self.op_token = 0
        self.busy_token = None
        self.is_busy = False
        self.segments_dirty = False
        # Path that current in-memory buffers belong to (last successful process).
        self.loaded_input_path = ""
        self.last_playback_anchor_sec = None
        self.last_playback_target = None
        self.sr = None
        self.segments = []
        self.auto_segments = []
        self.manual_segments = []
        self.selected_segment_index = None
        self.last_diagnostics = None
        self.player_process = None
        self.playback_temp_path = None
        self.playback_job = None
        self.playback_start_wall_time = None
        self.playback_start_audio_time = None
        self.playback_duration = None
        self.playback_plot_kind = "source"
        self.is_paused = False
        self.debug_text = tk.StringVar(value="诊断：未处理")
        self.peak_reject_var = tk.StringVar(value=str(self.app_config.get("peak_reject", 3)))
        self.percentile_reject_var = tk.StringVar(value=str(self.app_config.get("percentile_reject", 20)))
        self.voice_floor_var = tk.StringVar(value=str(self.app_config.get("voice_floor", 2)))
        self.left_append_ms_var = tk.StringVar(value=str(self.app_config.get("left_append_ms", LEFT_APPEND_MS)))
        self.right_append_ms_var = tk.StringVar(value=str(self.app_config.get("right_append_ms", RIGHT_APPEND_MS)))
        self.min_segment_len_ms_var = tk.StringVar(value=str(self.app_config.get("min_segment_length_ms", 0.0)))
        self.export_bitrate_var = tk.StringVar(value=str(int(self.app_config.get("export_bitrate_kbps", 128))))
        self.active_plot = "source"
        self.selected_time_sec = None
        self.selection_mode = False
        self.pick_detected_segment_mode = False
        self.half_time_mode = False
        self.picked_detected_segments = []
        self.selected_ranges = []
        self.half_time_ranges = []
        self.range_edit_mode = None
        self.drag_start_sec = None
        self.drag_plot_kind = None
        self.resize_segment_index = None
        self.resize_edge = None
        self.resize_preview_time = None
        self.pending_resize_index = None
        self.pending_resize_edge = None
        self.pending_resize_press_time = None
        # press 阶段消费了 half_time 操作时置 True，release 检查后清零，防止 release 继续走普通逻辑
        self._half_time_consumed = False
        self.current_view_start = 0.0
        self.current_view_duration = 8.0
        self._syncing_scrollbars = False
        self.source_playhead_line = None
        self.output_playhead_line = None
        self.last_playhead_draw_ms = None

        self._build_controls()
        self._build_plot()
        self.root.after(150, self.bring_to_front)

    def _build_controls(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill=tk.X)

        self.select_file_btn = ttk.Button(top, text="选取文件", command=self.select_file)
        self.select_file_btn.grid(row=0, column=0, sticky="w", pady=(10, 0))
        self.file_label = ttk.Label(top, text="未选择文件", foreground="gray")
        self.file_label.grid(row=0, column=1, columnspan=2, sticky="w", padx=(8, 12))

        ttk.Label(top, text="衰减强度：").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.atten_slider = tk.Scale(top, from_=10, to=30, orient=tk.HORIZONTAL, length=220)
        self.atten_slider.set(int(self.app_config.get("atten_db", 30)))
        self.atten_slider.grid(row=1, column=1, sticky="w", pady=(10, 0))

        ttk.Label(top, text="检测灵敏度：").grid(row=1, column=2, sticky="w", pady=(10, 0))
        self.sensitivity_slider = tk.Scale(top, from_=1, to=10, orient=tk.HORIZONTAL, length=220)
        self.sensitivity_slider.set(int(self.app_config.get("sensitivity", 10)))
        self.sensitivity_slider.grid(row=1, column=3, sticky="w", pady=(10, 0))

        ttk.Label(top, text="吸气最大峰值：").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.peak_reject_entry = ttk.Entry(top, textvariable=self.peak_reject_var, width=10)
        self.peak_reject_entry.grid(row=2, column=1, sticky="w", pady=(10, 0))
        ttk.Label(top, text="按 0-100 输入峰值上限；超过就更像正常人声", foreground="gray").grid(row=2, column=1, sticky="e", padx=(0, 90), pady=(10, 0))

        ttk.Label(top, text="吸气最大整体音量：").grid(row=2, column=2, sticky="w", pady=(10, 0))
        self.percentile_reject_entry = ttk.Entry(top, textvariable=self.percentile_reject_var, width=10)
        self.percentile_reject_entry.grid(row=2, column=3, sticky="w", pady=(10, 0))
        ttk.Label(top, text="按 0-100 输入整体音量上限；超过就更像整段人声", foreground="gray").grid(row=2, column=3, sticky="e", padx=(0, 90), pady=(10, 0))

        ttk.Label(top, text="人声下限：").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.voice_floor_entry = ttk.Entry(top, textvariable=self.voice_floor_var, width=10)
        self.voice_floor_entry.grid(row=3, column=1, sticky="w", pady=(10, 0))
        ttk.Label(top, text="按 0-100 输入，支持小数；低于此下限均按吸气处理", foreground="gray").grid(row=3, column=1, sticky="e", padx=(0, 50), pady=(10, 0))
        ttk.Label(top, text="最短吸气声片段(毫秒)：").grid(row=3, column=2, sticky="w", pady=(10, 0))
        self.min_segment_len_entry = ttk.Entry(top, textvariable=self.min_segment_len_ms_var, width=10)
        self.min_segment_len_entry.grid(row=3, column=3, sticky="w", pady=(10, 0))

        ttk.Label(top, text="向左附加(毫秒)：").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.left_append_entry = ttk.Entry(top, textvariable=self.left_append_ms_var, width=10)
        self.left_append_entry.grid(row=4, column=1, sticky="w", pady=(10, 0))
        ttk.Label(top, text="向右附加(毫秒)：").grid(row=4, column=2, sticky="w", pady=(10, 0))
        self.right_append_entry = ttk.Entry(top, textvariable=self.right_append_ms_var, width=10)
        self.right_append_entry.grid(row=4, column=3, sticky="w", pady=(10, 0))

        ttk.Label(top, text="导出码率：").grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.export_bitrate_combo = ttk.Combobox(
            top,
            textvariable=self.export_bitrate_var,
            values=["64", "128", "192", "256", "320"],
            width=8,
            state="readonly",
        )
        self.export_bitrate_combo.grid(row=5, column=1, sticky="w", pady=(10, 0))


        buttons = ttk.Frame(top)
        buttons.grid(row=7, column=0, columnspan=4, sticky="w", pady=(12, 0))
        self.process_btn = ttk.Button(buttons, text="重新处理当前文件", command=self.run_process, state=tk.DISABLED)
        self.process_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.export_btn = ttk.Button(buttons, text="导出", command=self.export_output_file, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=8)
        self.play_active_source_btn = ttk.Button(buttons, text="播放原文件", command=lambda: self.toggle_active_playback(False), state=tk.DISABLED)
        self.play_active_source_btn.pack(side=tk.LEFT, padx=8)
        self.play_active_output_btn = ColorButton(buttons, text="播放输出文件", command=lambda: self.toggle_active_playback(True), bg="#4CAF50")
        self.play_active_output_btn.pack(side=tk.LEFT, padx=8)
        self.half_time_btn = ColorButton(buttons, text="区间时间减半", command=self.toggle_half_time_mode, bg="#9C27B0")
        self.half_time_btn.pack(side=tk.LEFT, padx=8)
        self.select_range_btn = ttk.Button(buttons, text="手动选择区间", command=lambda: self.toggle_range_edit_mode("add"), state=tk.DISABLED)
        self.select_range_btn.pack(side=tk.LEFT, padx=8)
        self.cancel_range_btn = ColorButton(buttons, text="取消选择", command=lambda: self.toggle_range_edit_mode("remove"), bg="#F44336")
        self.cancel_range_btn.pack(side=tk.LEFT, padx=8)
        self.selection_mode_btn = ttk.Button(buttons, text="开启区间选择", command=self.toggle_selection_mode, state=tk.DISABLED)
        self.selection_mode_btn.pack(side=tk.LEFT, padx=8)
        self.pick_segment_btn = ttk.Button(buttons, text="选中处理片段", command=self.toggle_pick_detected_segment_mode, state=tk.DISABLED)
        self.pick_segment_btn.pack(side=tk.LEFT, padx=8)
        self.export_segments_btn = ttk.Button(buttons, text="导出区间", command=self.export_effective_segments, state=tk.DISABLED)
        self.export_segments_btn.pack(side=tk.LEFT, padx=8)
        self.clear_selection_btn = ttk.Button(buttons, text="清空选区", command=self.clear_selected_ranges, state=tk.DISABLED)
        self.clear_selection_btn.pack(side=tk.LEFT, padx=8)
        self.zoom_in_btn = ttk.Button(buttons, text="放大比例", command=lambda: self.adjust_zoom(0.5), state=tk.DISABLED)
        self.zoom_in_btn.pack(side=tk.LEFT, padx=8)
        self.zoom_out_btn = ttk.Button(buttons, text="缩小比例", command=lambda: self.adjust_zoom(2.0), state=tk.DISABLED)
        self.zoom_out_btn.pack(side=tk.LEFT, padx=8)
        self.reset_zoom_btn = ttk.Button(buttons, text="重置比例", command=self.reset_zoom, state=tk.DISABLED)
        self.reset_zoom_btn.pack(side=tk.LEFT, padx=8)

        self.status_label = ttk.Label(top, text="状态：等待操作", foreground="blue")
        self.status_label.grid(row=8, column=0, columnspan=4, sticky="w", pady=(12, 0))

        self.diagnostic_button = tk.Button(
            top,
            textvariable=self.debug_text,
            command=self.copy_diagnostics,
            anchor="w",
            justify=tk.LEFT,
            relief=tk.FLAT,
            fg="#1f4e79",
            wraplength=1120,
            cursor="hand2",
        )
        self.diagnostic_button.grid(row=9, column=0, columnspan=4, sticky="we", pady=(8, 0))

    def _build_plot(self):
        plot_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        plot_frame.pack(fill=tk.BOTH, expand=True)

        source_frame = ttk.Frame(plot_frame)
        source_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        output_frame = ttk.Frame(plot_frame)
        output_frame.pack(fill=tk.BOTH, expand=True)

        self.figure_source = Figure(figsize=(11, 3.2), dpi=100)
        self.ax_source = self.figure_source.add_subplot(111)
        self.figure_source.tight_layout(pad=2.0)
        self.canvas_source = FigureCanvasTkAgg(self.figure_source, master=source_frame)
        self.canvas_source.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas_source.mpl_connect("button_press_event", lambda event: self.on_plot_press(event, "source"))
        self.canvas_source.mpl_connect("button_release_event", lambda event: self.on_plot_release(event, "source"))
        self.canvas_source.mpl_connect("motion_notify_event", lambda event: self.on_plot_motion(event, "source"))

        self.source_scroll = tk.Scale(
            source_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            showvalue=False,
            command=lambda value: self.on_scroll("source", value),
            state=tk.DISABLED,
        )
        self.source_scroll.pack(fill=tk.X)
        self.source_scroll.bind("<Button-1>", lambda event: self.on_scroll_click(event, self.source_scroll))

        self.figure_output = Figure(figsize=(11, 3.2), dpi=100)
        self.ax_output = self.figure_output.add_subplot(111)
        self.figure_output.tight_layout(pad=2.0)
        self.canvas_output = FigureCanvasTkAgg(self.figure_output, master=output_frame)
        self.canvas_output.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas_output.mpl_connect("button_press_event", lambda event: self.on_plot_press(event, "output"))
        self.canvas_output.mpl_connect("button_release_event", lambda event: self.on_plot_release(event, "output"))
        self.canvas_output.mpl_connect("motion_notify_event", lambda event: self.on_plot_motion(event, "output"))

        self.output_scroll = tk.Scale(
            output_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            showvalue=False,
            command=lambda value: self.on_scroll("output", value),
            state=tk.DISABLED,
        )
        self.output_scroll.pack(fill=tk.X)
        self.output_scroll.bind("<Button-1>", lambda event: self.on_scroll_click(event, self.output_scroll))

        self._draw_placeholder()

    def _draw_placeholder(self):
        self.source_playhead_line = None
        self.output_playhead_line = None
        for ax, canvas, title in (
            (self.ax_source, self.canvas_source, "源文件音量谱"),
            (self.ax_output, self.canvas_output, "输出文件音量谱"),
        ):
            ax.clear()
            ax.set_title(title)
            ax.set_xlabel("")
            ax.set_ylabel("")
            ax.text(0.5, 0.5, "处理后显示音量谱与吸气片段", transform=ax.transAxes, ha="center", va="center", color="gray")
            canvas.draw_idle()

    def _bump_op_token(self):
        """Invalidate every in-flight process/rewrite/export-refresh apply."""
        self.op_token += 1
        return self.op_token

    def _release_busy_if_token(self, token, status_text=None, status_color="orange"):
        """Clear busy only if this apply still owns the busy slot (or no owner recorded)."""
        if not self.is_busy:
            if status_text is not None:
                self.status_label.config(text=status_text, foreground=status_color)
            return
        if self.busy_token is None or self.busy_token == token:
            self._set_busy(False, status_text, status_color)
            self.busy_token = None
        elif status_text is not None:
            # Superseded op: do not flip busy off under a newer owner.
            pass

    def _restore_loaded_path_ui(self, status_text, status_color="red"):
        """Align input_path/label with last successfully loaded buffers."""
        if self.loaded_input_path:
            self.input_path = self.loaded_input_path
            self.file_label.config(text=os.path.basename(self.loaded_input_path), foreground="green")
        else:
            self.input_path = ""
            self.file_label.config(text="未选择文件", foreground="gray")
        self.status_label.config(text=status_text, foreground=status_color)
        self._set_busy(False, status_text, status_color)

    def select_file(self):
        if self.is_busy:
            messagebox.showinfo("提示", "当前正在处理，请稍候再选取文件")
            return
        path = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频文件", "*.wav *.mp3 *.m4a *.flac"), ("所有文件", "*.*")],
        )
        if not path:
            return
        previous_path = self.loaded_input_path or self.input_path
        previous_label = self.file_label.cget("text")
        previous_label_color = self.file_label.cget("foreground")
        # Cancel residual async applies before switching path.
        self._bump_op_token()
        self._clear_interaction_state()
        self.input_path = path
        self.file_label.config(text=os.path.basename(path), foreground="green")
        self.process_btn.config(state=tk.NORMAL)
        self.status_label.config(text="状态：已选择文件，正在自动处理...", foreground="orange")
        self.root.update_idletasks()
        started = self.run_process(force=True, previous_path_for_rollback=previous_path, previous_label_for_rollback=(previous_label, previous_label_color))
        if not started:
            # Roll back to last successful path so export cannot pair new name with old audio.
            if previous_path:
                self.input_path = previous_path
                self.file_label.config(text=previous_label, foreground=previous_label_color)
                self.status_label.config(text="状态：未开始处理，仍使用上一成功文件", foreground="blue")
            else:
                self.input_path = ""
                self.file_label.config(text="未选择文件", foreground="gray")
                self.status_label.config(text="状态：未开始处理", foreground="blue")

    def _set_busy(self, busy, status_text=None, status_color="orange", busy_token=None):
        self.is_busy = bool(busy)
        if busy:
            self.busy_token = busy_token if busy_token is not None else self.op_token
        else:
            self.busy_token = None
        if status_text is not None:
            self.status_label.config(text=status_text, foreground=status_color)
        # Selecting another file while busy desyncs path vs buffers — keep disabled.
        self.select_file_btn.config(state=tk.DISABLED if busy else tk.NORMAL)
        process_state = tk.DISABLED if busy or not self.input_path else tk.NORMAL
        self.process_btn.config(state=process_state)
        has_audio = self.source_audio is not None and not busy
        # Export only when path matches successfully loaded buffers.
        path_ok = bool(self.loaded_input_path) and self.input_path == self.loaded_input_path
        has_output = self.output_audio is not None and path_ok and not busy
        has_segments = bool(self.segments) and not busy
        click_play_state = tk.NORMAL if has_audio else tk.DISABLED
        export_state = tk.NORMAL if has_output else tk.DISABLED
        segment_state = tk.NORMAL if has_segments else tk.DISABLED
        self.export_btn.config(state=export_state)
        self.half_time_btn.config(state=segment_state)
        self.select_range_btn.config(state=click_play_state)
        self.cancel_range_btn.config(state=click_play_state)
        self.selection_mode_btn.config(state=click_play_state)
        self.pick_segment_btn.config(state=click_play_state)
        self.export_segments_btn.config(state=segment_state)
        self.clear_selection_btn.config(state=click_play_state)
        self.play_active_source_btn.config(state=click_play_state)
        self.play_active_output_btn.config(state=click_play_state)
        zoom_state = click_play_state
        self.zoom_in_btn.config(state=zoom_state)
        self.zoom_out_btn.config(state=zoom_state)
        self.reset_zoom_btn.config(state=zoom_state)
        self.source_scroll.config(state=zoom_state)
        self.output_scroll.config(state=zoom_state)

    def _apply_process_result(self, result, previous_selected_time, previous_view_start, previous_view_duration, previous_active_plot, source_path):
        self.source_audio = result["source_audio"]
        self.limited_source_audio = result.get("limited_source_audio")
        self.limited_playback_audio = result.get("limited_playback_audio")
        self.output_audio = result["output_audio"]
        self.output_display_audio = result.get("output_display_audio", result["output_audio"])
        self.source_playback_audio = result.get("source_playback_audio", result["source_audio"])
        self.output_playback_audio = result.get("output_playback_audio", result["output_audio"])
        self.output_headroom_gain = float(result.get("output_headroom_gain", 1.0))
        self.output_has_post_processing = True
        self.output_timeline_segments = result.get("output_timeline_segments", [])
        self.sr = result["sr"]
        self.segments = result["segments"]
        self.output_path = result["output_path"]
        self.last_diagnostics = result["diagnostics"]
        self.input_path = source_path
        self.loaded_input_path = source_path
        self.file_label.config(text=os.path.basename(source_path), foreground="green")
        self.selected_segment_index = 0 if self.segments else None
        self.selected_time_sec = (self.segments[0][0] / self.sr) if self.segments else 0.0
        self.auto_segments = [
            (start / self.sr, end / self.sr)
            for start, end in result.get("auto_segments", result["segments"])
        ]
        self.manual_segments = []
        self.selection_mode = False
        self.pick_detected_segment_mode = False
        self.half_time_mode = False
        self.picked_detected_segments = []
        self.selected_ranges = []
        self.half_time_ranges = []
        self.segments_dirty = False
        # Drop any mid-drag / mid-resize state from the previous session.
        self._clear_interaction_state()
        self.range_edit_mode = None
        total_duration = len(self.source_audio) / self.sr if self.source_audio is not None else 0.0
        self.current_view_duration = min(previous_view_duration, total_duration) if total_duration else previous_view_duration
        if total_duration:
            view_duration = min(max(0.8, self.current_view_duration), total_duration)
            max_start = max(0.0, total_duration - view_duration)
            self.current_view_duration = view_duration
            self.current_view_start = min(max(previous_view_start, 0.0), max_start)
            if previous_selected_time is not None:
                self.selected_time_sec = min(max(previous_selected_time, 0.0), total_duration)
        else:
            self.current_view_start = 0.0
            self.current_view_duration = previous_view_duration
            self.selected_time_sec = previous_selected_time
        self.active_plot = previous_active_plot

        self.debug_text.set(format_diagnostics_text(self.last_diagnostics, len(self.segments)))
        self._save_current_config()
        self._set_busy(False, "状态：处理完成，当前显示为内存输出；点击“导出”才会写入磁盘", "green")
        self._update_selection_buttons()
        self._update_play_toggle_buttons()
        self.refresh_plots()

    def run_process(self, force=False, previous_path_for_rollback=None, previous_label_for_rollback=None):
        """Start full reprocess. Returns True if a worker was started."""
        if not self.input_path:
            messagebox.showwarning("提示", "请先选择音频文件")
            return False
        if self.is_busy and not force:
            return False

        has_manual_edits = bool(
            self.segments_dirty
            or self.manual_segments
            or self.half_time_ranges
            or self.selected_ranges
            or self.picked_detected_segments
        )
        if has_manual_edits and not force:
            if not messagebox.askyesno("确认重新处理", "重新处理将清空手动区间、半速标记、边界调整和选区，是否继续？"):
                return False

        previous_selected_time = float(self.selected_time_sec) if self.selected_time_sec is not None else None
        previous_view_start = float(self.current_view_start)
        previous_view_duration = float(self.current_view_duration)
        previous_active_plot = self.active_plot
        rollback_path = previous_path_for_rollback if previous_path_for_rollback is not None else self.loaded_input_path
        rollback_label = previous_label_for_rollback

        self._stop_player()
        self.source_playhead_line = None
        self.output_playhead_line = None
        self._clear_interaction_state()

        try:
            peak_reject_threshold = np.clip(float(self.peak_reject_var.get()), 0.0, 100.0) / 100.0
            percentile_reject_threshold = np.clip(float(self.percentile_reject_var.get()), 0.0, 100.0) / 100.0
            voice_floor_threshold = np.clip(float(self.voice_floor_var.get()), 0.0, 100.0) / 100.0
            left_append_ms = max(0.0, float(self.left_append_ms_var.get()))
            right_append_ms = max(0.0, float(self.right_append_ms_var.get()))
            bitrate_kbps = int(np.clip(int(self.export_bitrate_var.get()), 64, 320))
            min_segment_length_ms = max(0.0, float(self.min_segment_len_ms_var.get()))
        except ValueError:
            messagebox.showwarning("提示", "吸气最大峰值、吸气最大整体音量、人声下限、最短吸气声片段、向左附加、向右附加和导出码率都需要填写数字")
            return False

        self.peak_reject_var.set(f"{peak_reject_threshold * 100:.2f}".rstrip("0").rstrip("."))
        self.percentile_reject_var.set(f"{percentile_reject_threshold * 100:.2f}".rstrip("0").rstrip("."))
        self.voice_floor_var.set(f"{voice_floor_threshold * 100:.2f}".rstrip("0").rstrip("."))
        self.min_segment_len_ms_var.set(f"{min_segment_length_ms:.2f}".rstrip("0").rstrip("."))
        self.left_append_ms_var.set(f"{left_append_ms:.2f}".rstrip("0").rstrip("."))
        self.right_append_ms_var.set(f"{right_append_ms:.2f}".rstrip("0").rstrip("."))
        self.export_bitrate_var.set(str(bitrate_kbps))

        input_path = self.input_path
        atten_db = self.atten_slider.get()
        sensitivity = self.sensitivity_slider.get()

        def work():
            return process_breath(
                input_path,
                atten_db,
                sensitivity,
                peak_reject_threshold,
                percentile_reject_threshold,
                voice_floor_threshold,
                left_append_ms,
                right_append_ms,
                min_segment_length_ms,
            )

        def on_ok(result, token):
            self._apply_process_result(
                result,
                previous_selected_time,
                previous_view_start,
                previous_view_duration,
                previous_active_plot,
                input_path,
            )

        def on_err(error):
            # Roll path back to last successful load so export cannot mix names.
            if rollback_path:
                self.input_path = rollback_path
                if rollback_label:
                    self.file_label.config(text=rollback_label[0], foreground=rollback_label[1])
                else:
                    self.file_label.config(text=os.path.basename(rollback_path), foreground="green")
                self.status_label.config(
                    text=f"状态：处理失败，已恢复为 {os.path.basename(rollback_path)}",
                    foreground="red",
                )
            elif self.loaded_input_path:
                self.input_path = self.loaded_input_path
                self.file_label.config(text=os.path.basename(self.loaded_input_path), foreground="green")
                self.status_label.config(
                    text=f"状态：处理失败，仍使用 {os.path.basename(self.loaded_input_path)}",
                    foreground="red",
                )
            else:
                self.input_path = ""
                self.file_label.config(text="未选择文件", foreground="gray")
                self.status_label.config(text="状态：处理失败，未加载有效文件", foreground="red")
            messagebox.showerror("错误", f"处理失败：{error}")

        self._run_bg_job(
            work,
            on_ok,
            on_error=on_err,
            busy_text="状态：正在识别并生成内存预览...",
        )
        return True

    def _compute_envelope(self, audio):
        if audio is None or self.sr is None:
            return np.asarray([], dtype=np.float32), np.asarray([], dtype=np.float32)
        envelope = librosa.feature.rms(y=audio, frame_length=2048, hop_length=HOP_LENGTH)[0]
        times = librosa.times_like(envelope, sr=self.sr, hop_length=HOP_LENGTH)
        return times, envelope

    def _map_source_sample_to_output_sample(self, source_sample):
        if self.output_audio is None or self.sr is None:
            return 0
        source_sample = int(np.clip(source_sample, 0, len(self.source_audio) if self.source_audio is not None else source_sample))
        if not self.output_timeline_segments:
            return int(np.clip(source_sample, 0, len(self.output_audio)))
        for src_start, src_end, out_start, out_end in self.output_timeline_segments:
            if source_sample <= src_end:
                src_len = max(1, src_end - src_start)
                out_len = max(1, out_end - out_start)
                ratio = np.clip((source_sample - src_start) / src_len, 0.0, 1.0)
                return int(np.clip(round(out_start + ratio * out_len), 0, len(self.output_audio)))
        return len(self.output_audio)

    def _map_output_sample_to_source_sample(self, output_sample):
        if self.source_audio is None or self.sr is None:
            return 0
        output_sample = int(np.clip(output_sample, 0, len(self.output_audio) if self.output_audio is not None else output_sample))
        if not self.output_timeline_segments:
            return int(np.clip(output_sample, 0, len(self.source_audio)))
        for src_start, src_end, out_start, out_end in self.output_timeline_segments:
            if output_sample <= out_end:
                out_len = max(1, out_end - out_start)
                src_len = max(1, src_end - src_start)
                ratio = np.clip((output_sample - out_start) / out_len, 0.0, 1.0)
                return int(np.clip(round(src_start + ratio * src_len), 0, len(self.source_audio)))
        return len(self.source_audio)

    def _map_output_positions_to_source_times(self, positions):
        positions = np.asarray(positions, dtype=np.float32)
        if self.sr is None:
            return positions
        if not self.output_timeline_segments:
            return positions / self.sr
        source_positions = np.empty_like(positions, dtype=np.float32)
        last_filled = np.zeros_like(positions, dtype=bool)
        for idx, (src_start, src_end, out_start, out_end) in enumerate(self.output_timeline_segments):
            if idx == len(self.output_timeline_segments) - 1:
                mask = positions >= out_start
            else:
                mask = (positions >= out_start) & (positions < out_end)
            if not np.any(mask):
                continue
            out_len = max(1, out_end - out_start)
            src_len = max(1, src_end - src_start)
            ratio = np.clip((positions[mask] - out_start) / out_len, 0.0, 1.0)
            source_positions[mask] = src_start + ratio * src_len
            last_filled[mask] = True
        if not np.all(last_filled):
            source_positions[~last_filled] = positions[~last_filled]
        return source_positions / self.sr

    def _draw_wave_envelope(self, ax, audio, title, active=False, plot_kind="source"):
        ax.clear()
        if audio is None or self.sr is None:
            ax.set_title(title)
            return
        times, envelope = self._compute_envelope(audio)
        if plot_kind == "output" and len(times):
            reference_len = len(self.output_audio) if self.output_audio is not None else len(audio)
            audio_len = max(1, len(audio))
            position_scale = reference_len / audio_len
            positions = np.arange(len(times), dtype=np.float32) * HOP_LENGTH * position_scale
            times = self._map_output_positions_to_source_times(positions)
            duration = len(self.source_audio) / self.sr if self.source_audio is not None else (len(audio) / self.sr)
        else:
            duration = len(audio) / self.sr
        ax.plot(times, envelope, color="#2d6cdf", linewidth=1.2)
        ax.fill_between(times, 0, envelope, color="#8cb7ff", alpha=0.35)
        ax.set_title(f"{title}{'  [当前选择]' if active else ''}")
        ax.set_xlabel("")
        ax.set_ylabel("")
        ax.set_ylim(bottom=0)

        try:
            peak_line = np.clip(float(self.peak_reject_var.get()), 0.0, 100.0) / 100.0
            percentile_line = np.clip(float(self.percentile_reject_var.get()), 0.0, 100.0) / 100.0
            voice_floor_line = np.clip(float(self.voice_floor_var.get()), 0.0, 100.0) / 100.0
        except ValueError:
            peak_line = 0.10
            percentile_line = 0.20
            voice_floor_line = 0.0
        if len(envelope):
            ax.axhline(
                peak_line,
                color="#ff8a00",
                linestyle="--",
                linewidth=1.1,
                alpha=0.85,
            )
            ax.axhline(
                percentile_line,
                color="#ffd400",
                linestyle="--",
                linewidth=1.1,
                alpha=0.85,
            )
            if voice_floor_line > 0:
                ax.axhline(
                    voice_floor_line,
                    color="#ffffff",
                    linestyle="--",
                    linewidth=1.1,
                    alpha=0.95,
                )

        merged_visible_segments = [(start / self.sr, end / self.sr) for start, end in self.segments]

        for index, (start_sec, end_sec) in enumerate(merged_visible_segments):
            has_half = self._segment_is_half_time(start_sec, end_sec)
            edge_color = (
                "#c084fc" if has_half
                else ("#ff7f50" if index in self.picked_detected_segments else "#00cc55")
            )
            fill_alpha = (
                0.30 if index == self.selected_segment_index
                else (0.22 if index in self.picked_detected_segments else 0.18)
            )
            selected_edge = "#f7ff00" if index == self.selected_segment_index else edge_color
            # Base breath segment always green; half-time overlays only intersecting subranges.
            ax.axvspan(start_sec, end_sec, color="#00ff66", alpha=fill_alpha, ec=selected_edge, lw=2)
            if has_half and self.half_time_ranges:
                for hs, he in self.half_time_ranges:
                    ov_s = max(float(hs), float(start_sec))
                    ov_e = min(float(he), float(end_sec))
                    if ov_e > ov_s:
                        ax.axvspan(ov_s, ov_e, color="#a855f7", alpha=0.40, ec="#c084fc", lw=1.5)

        for start_sec, end_sec in self.selected_ranges:
            ax.axvspan(start_sec, end_sec, color="#5aa9ff", alpha=0.22, ec="#1f6feb", lw=2)

        playhead_line = None
        if self.selected_time_sec is not None:
            playhead_line = ax.axvline(self.selected_time_sec, color="#ff5a36", linewidth=1.5, linestyle="--")
        if ax is self.ax_source:
            self.source_playhead_line = playhead_line
        elif ax is self.ax_output:
            self.output_playhead_line = playhead_line

        if (self.range_edit_mode or self.selection_mode) and self.drag_start_sec is not None:
            drag_color = "#1f6feb" if self.range_edit_mode == "add" else "#d7263d"
            if self.selection_mode:
                drag_color = "#8a2be2"
            ax.axvline(self.drag_start_sec, color=drag_color, linewidth=1.2, linestyle=":")
        if self.resize_segment_index is not None and self.resize_preview_time is not None:
            ax.axvline(self.resize_preview_time, color="#ff4fd8", linewidth=1.3, linestyle=":")

        if duration > 0:
            view_duration = min(self.current_view_duration, duration)
            max_start = max(0.0, duration - view_duration)
            self.current_view_start = min(max(self.current_view_start, 0.0), max_start)
            ax.set_xlim(self.current_view_start, self.current_view_start + view_duration)

    def refresh_plots(self):
        self._draw_wave_envelope(self.ax_source, self.source_audio, "源文件音量谱", active=self.active_plot == "source", plot_kind="source")
        output_plot_audio = self.output_display_audio if self.output_display_audio is not None else self.output_audio
        self._draw_wave_envelope(self.ax_output, output_plot_audio, "输出文件音量谱", active=self.active_plot == "output", plot_kind="output")
        self.canvas_source.draw()
        self.canvas_output.draw()
        self.canvas_source.flush_events()
        self.canvas_output.flush_events()
        self.last_playhead_draw_ms = None
        self.sync_scrollbars()

    def sync_scrollbars(self):
        if self.source_audio is None or self.sr is None:
            return
        total_duration = len(self.source_audio) / self.sr
        max_start = max(0.0, total_duration - self.current_view_duration)
        value = 0 if max_start <= 0 else (self.current_view_start / max_start) * 100
        self._syncing_scrollbars = True
        try:
            self.source_scroll.set(value)
            self.output_scroll.set(value)
        finally:
            self._syncing_scrollbars = False

    def _update_playhead_display(self, follow_playback=False, force_refresh=False):
        if self.source_audio is None or self.sr is None:
            return
        if self.selected_time_sec is None:
            return

        total_duration = len(self.source_audio) / self.sr
        view_duration = min(self.current_view_duration, total_duration) if total_duration > 0 else self.current_view_duration
        max_start = max(0.0, total_duration - view_duration)
        needs_refresh = force_refresh or self.source_playhead_line is None or self.output_playhead_line is None

        if follow_playback and total_duration > 0:
            left = self.current_view_start
            right = left + view_duration
            margin = min(max(0.08, view_duration * 0.08), max(0.08, view_duration / 4))
            new_view_start = self.current_view_start
            if self.selected_time_sec < left + margin:
                new_view_start = self.selected_time_sec - margin
            elif self.selected_time_sec > right - margin:
                lead_ratio = 0.35
                new_view_start = self.selected_time_sec - view_duration * lead_ratio
            new_view_start = min(max(new_view_start, 0.0), max_start)
            if abs(new_view_start - self.current_view_start) > 1e-6:
                self.current_view_start = new_view_start
                needs_refresh = True

        if needs_refresh:
            self.refresh_plots()
            return

        x_value = [self.selected_time_sec, self.selected_time_sec]
        self.source_playhead_line.set_xdata(x_value)
        self.output_playhead_line.set_xdata(x_value)
        now_ms = int(self.root.winfo_toplevel().tk.call("clock", "milliseconds"))
        should_draw = (
            force_refresh
            or self.last_playhead_draw_ms is None
            or (now_ms - self.last_playhead_draw_ms) >= PLAYHEAD_DRAW_INTERVAL_MS
        )
        if should_draw:
            self.canvas_source.draw_idle()
            self.canvas_output.draw_idle()
            self.last_playhead_draw_ms = now_ms

    def on_scroll(self, _which, value):
        if self._syncing_scrollbars:
            return
        if self.source_audio is None or self.sr is None:
            return
        total_duration = len(self.source_audio) / self.sr
        max_start = max(0.0, total_duration - self.current_view_duration)
        if max_start <= 0:
            self.current_view_start = 0.0
        else:
            self.current_view_start = (float(value) / 100.0) * max_start
        self.refresh_plots()

    def on_scroll_click(self, event, scale):
        width = max(1, scale.winfo_width())
        fraction = min(max(event.x / width, 0.0), 1.0)
        value = fraction * 100.0
        scale.set(value)
        self.on_scroll(None, value)
        return "break"

    def adjust_zoom(self, factor):
        if self.source_audio is None or self.sr is None:
            return
        total_duration = len(self.source_audio) / self.sr
        new_duration = np.clip(self.current_view_duration * factor, 0.8, max(0.8, total_duration))
        if self.selected_segment_index is not None and self.segments:
            start, end = self.segments[self.selected_segment_index]
            center = ((start + end) / 2) / self.sr
            self.current_view_start = center - new_duration / 2
        self.current_view_duration = float(new_duration)
        self.refresh_plots()

    def reset_zoom(self):
        if self.source_audio is None or self.sr is None:
            return
        total_duration = len(self.source_audio) / self.sr
        self.current_view_start = 0.0
        self.current_view_duration = min(8.0, total_duration) if total_duration else 8.0
        self.refresh_plots()

    # ──────────────────────────────────────────────────────────────────────
    #  鼠标事件三件套：press / motion / release
    #
    #  核心设计原则：
    #    • half_time_mode 在 press 时原子完成（命中即生效+退出模式），
    #      motion 和 release 都直接 return，绝不进入其他任何路径。
    #    • resize 的 pending→active 提升只在 motion 里发生，
    #      release 里仅处理已激活的 resize，不再有 fallback 到 click 的歧义。
    # ──────────────────────────────────────────────────────────────────────

    def on_plot_press(self, event, plot_kind):
        if self.sr is None:
            return
        if self.is_busy:
            return

        event_log(f"PRESS  plot={plot_kind} x={event.xdata} half={self.half_time_mode} resize_pending={self.pending_resize_index} resize_active={self.resize_segment_index}")

        # ── 减半模式：press 进入时立即关闭模式，然后原子处理本次点击 ──
        if self.half_time_mode:
            self.half_time_mode = False          # 立即关闭，无论后续命中与否都不再接受新点击
            self._half_time_consumed = True      # 告知 motion/release 本次周期已被减半消费
            self._update_selection_buttons()
            if event.xdata is not None:
                self.status_label.config(text="状态：正在处理，请稍候...", foreground="orange")
                # 1. 立即应用减半范围的变更
                worked = self._apply_half_time_at_time(float(event.xdata), plot_kind)
                # 2. 立即重绘，这样画布上的区间可以立刻变紫
                self.refresh_plots()
                # Prefer idle tasks only — root.update() re-enters the event loop
                # before busy/token protect the follow-on rewrite.
                self.root.update_idletasks()

                # 3. 后台重算音频，避免卡死 UI
                if worked:
                    self._rewrite_output_from_current_segments(
                        async_mode=True,
                        status_on_done="状态：处理完成，已退出减半模式",
                    )
            else:
                self.status_label.config(text="状态：已退出减半模式", foreground="blue")
                self.refresh_plots()
            return

        if event.xdata is None:
            return

        # ── 强制清理可能因为 Mac 环境下 release 丢失导致残留的 active resize 状态 ──
        if self.resize_segment_index is not None:
            self.resize_segment_index = None
            self.resize_edge = None
            self.resize_preview_time = None
            self.pending_resize_index = None
            self.pending_resize_edge = None
            self.pending_resize_press_time = None

        # ── resize handle 检测（pending 阶段：等待 motion 确认拖动意图）──
        self.pending_resize_index = None
        self.pending_resize_edge = None
        self.pending_resize_press_time = None
        resize_hit = self._find_resize_handle(float(event.xdata))
        if (resize_hit is not None
                and not self.selection_mode
                and self.range_edit_mode is None
                and not self.pick_detected_segment_mode):
            self.active_plot = plot_kind
            self.pending_resize_index, self.pending_resize_edge = resize_hit
            self.pending_resize_press_time = float(event.xdata)
            self.selected_segment_index = self.pending_resize_index
            self.selected_time_sec = float(event.xdata)
            self.refresh_plots()
            return

        # ── 手动区间添加模式 ──
        if self.range_edit_mode == "add":
            self.active_plot = plot_kind
            self.drag_start_sec = float(event.xdata)
            self.drag_plot_kind = plot_kind
            self.selected_time_sec = float(event.xdata)
            self.status_label.config(
                text=f"状态：开始选择处理区间，起点 {self.drag_start_sec:.2f}s",
                foreground="purple",
            )
            self.refresh_plots()
            return

        # ── 区间选择模式 ──
        if self.selection_mode:
            self.active_plot = plot_kind
            self.drag_start_sec = float(event.xdata)
            self.drag_plot_kind = plot_kind
            self.selected_time_sec = float(event.xdata)
            self.status_label.config(
                text=f"状态：开始选择{('源文件' if plot_kind == 'source' else '输出文件')}区间，起点 {self.drag_start_sec:.2f}s",
                foreground="purple",
            )
            self.refresh_plots()
            return

        self.on_plot_click(event, plot_kind)

    def on_plot_motion(self, event, plot_kind):
        if event.xdata is None or self.sr is None:
            return
        if self.is_busy:
            return

        # ── 减半模式（或本次 press 已被减半消费）：motion 期间完全忽略 ──
        if self.half_time_mode or self._half_time_consumed:
            return

        event_log(f"MOTION plot={plot_kind} x={event.xdata:.3f} pending={self.pending_resize_index} active_resize={self.resize_segment_index}")

        # ── pending → active resize 提升 ──
        if self.pending_resize_index is not None and self.pending_resize_press_time is not None:
            moved_sec = abs(float(event.xdata) - self.pending_resize_press_time)
            if moved_sec >= MIN_RESIZE_DRAG_SEC:
                self.active_plot = plot_kind
                self.resize_segment_index = self.pending_resize_index
                self.resize_edge = self.pending_resize_edge
                self.resize_preview_time = float(event.xdata)
                self.selected_segment_index = self.resize_segment_index
                self.selected_time_sec = float(event.xdata)
                self.pending_resize_index = None
                self.pending_resize_edge = None
                self.pending_resize_press_time = None
                self.status_label.config(
                    text=f"状态：拖动调整绿色片段{'左侧' if self.resize_edge == 'start' else '右侧'}边界",
                    foreground="purple",
                )
                self._update_playhead_display(force_refresh=True)
            return

        # ── active resize 跟随 ──
        if self.resize_segment_index is not None:
            self.active_plot = plot_kind
            self.resize_preview_time = float(event.xdata)
            self.selected_time_sec = float(event.xdata)
            # Throttle full redraw during drag (was force-refresh every motion event).
            now_ms = int(self.root.winfo_toplevel().tk.call("clock", "milliseconds"))
            if self.last_playhead_draw_ms is None or (now_ms - self.last_playhead_draw_ms) >= PLAYHEAD_DRAW_INTERVAL_MS:
                self._update_playhead_display(force_refresh=True)
                self.last_playhead_draw_ms = now_ms
            else:
                self._update_playhead_display(force_refresh=False)

    def on_plot_release(self, event, plot_kind):
        if self.sr is None:
            return
        # Hard-block all plot completion while busy so mid-drag cannot schedule rewrite
        # over a concurrent process/export.
        if self.is_busy:
            self._clear_interaction_state()
            self.drag_start_sec = None
            self.drag_plot_kind = None
            return

        event_log(f"RELEASE plot={plot_kind} x={event.xdata} half={self.half_time_mode} pending={self.pending_resize_index} active_resize={self.resize_segment_index}")

        # ── 减半模式的 press 已消费本次点击：release 直接跳过所有逻辑 ──
        if self._half_time_consumed:
            self._half_time_consumed = False
            return

        # ── active resize 提交 ──
        if self.resize_segment_index is not None:
            new_time_sec = float(event.xdata) if event.xdata is not None else self.resize_preview_time
            segment_idx = self.resize_segment_index
            edge = self.resize_edge

            self.resize_segment_index = None
            self.resize_edge = None
            self.resize_preview_time = None
            self.pending_resize_index = None
            self.pending_resize_edge = None
            self.pending_resize_press_time = None

            if new_time_sec is not None:
                self.status_label.config(text="状态：正在计算调整后的区间，请稍候...", foreground="orange")
                self.root.update_idletasks()
                token = self.op_token

                def do_resize(captured_token=token, idx=segment_idx, ed=edge, t=new_time_sec):
                    if captured_token != self.op_token or self.is_busy:
                        return
                    self._apply_segment_resize(idx, ed, t)

                self.root.after(10, do_resize)
            return

        # ── pending resize 未达到拖动阈值 → 退化为普通点击 ──
        pending_resize_index = self.pending_resize_index
        self.pending_resize_index = None
        self.pending_resize_edge = None
        self.pending_resize_press_time = None
        if pending_resize_index is not None and event.xdata is not None:
            self.on_plot_click(event, plot_kind)
            return

        # ── drag 拖拽区间结束 ──
        if (self.range_edit_mode != "add" and not self.selection_mode) or self.drag_start_sec is None:
            return
        if event.xdata is None:
            self.drag_start_sec = None
            self.drag_plot_kind = None
            self.refresh_plots()
            return

        start_sec = min(self.drag_start_sec, float(event.xdata))
        end_sec = max(self.drag_start_sec, float(event.xdata))
        drag_duration = abs(end_sec - start_sec)
        self.drag_start_sec = None
        self.drag_plot_kind = None

        if self.range_edit_mode == "add" and drag_duration < MIN_MANUAL_DRAG_SEC:
            self.range_edit_mode = None
            self.status_label.config(text="状态：已取消本次手动选择", foreground="blue")
            self._update_selection_buttons()
            self.refresh_plots()
            return

        if drag_duration < 0.005:
            end_sec = min(
                start_sec + 0.005,
                len(self.source_audio) / self.sr if self.source_audio is not None else start_sec + 0.005,
            )

        self.selected_time_sec = start_sec
        if self.range_edit_mode == "add":
            self._apply_range_edit(start_sec, end_sec)
            return

        self.selected_ranges.append((start_sec, end_sec))
        self.selected_ranges.sort(key=lambda item: item[0])
        self._update_selection_buttons()
        self.status_label.config(
            text=f"状态：已添加选区 {start_sec:.2f}s - {end_sec:.2f}s，可继续拖拽选择下一段",
            foreground="purple",
        )
        self.refresh_plots()

    def on_plot_click(self, event, plot_kind):
        if event.xdata is None or self.sr is None:
            return

        self.active_plot = plot_kind
        clicked_time = float(event.xdata)
        self.selected_time_sec = clicked_time
        best_index = None
        best_distance = float("inf")
        near_tolerance = min(1.20, max(0.12, self.current_view_duration * 0.035))
        for index, (start, end) in enumerate(self.segments):
            start_sec = start / self.sr
            end_sec = end / self.sr
            if start_sec <= clicked_time <= end_sec:
                best_index = index
                break
            distance = min(abs(clicked_time - start_sec), abs(clicked_time - end_sec))
            if distance < best_distance and distance <= near_tolerance:
                best_index = index
                best_distance = distance

        if self.range_edit_mode == "remove":
            target_range = self._find_clicked_effective_range(clicked_time)
            if target_range is None:
                self.range_edit_mode = None
                self.status_label.config(text="状态：未点中现有处理区间，本次取消已退出", foreground="blue")
                self._update_selection_buttons()
                self.refresh_plots()
                return
            _, _, start_sec, end_sec = target_range
            sample_start = int(round(start_sec * self.sr))
            sample_end = int(round(end_sec * self.sr))
            self.selected_segment_index = None
            for index, (start, end) in enumerate(self.segments):
                if start == sample_start and end == sample_end:
                    self.selected_segment_index = index
                    break
            self.selected_time_sec = clicked_time
            self._apply_range_edit(start_sec, end_sec)
            return

        if self.pick_detected_segment_mode:
            if best_index is None:
                self.status_label.config(text="状态：未点中绿色处理片段，请再试一次", foreground="purple")
                return
            if best_index not in self.picked_detected_segments:
                self.picked_detected_segments.append(best_index)
            start, end = self.segments[best_index]
            self.selected_segment_index = best_index
            self.status_label.config(
                text=f"状态：已加入处理片段 {int(round(start / self.sr * 1000))}-{int(round(end / self.sr * 1000))}，继续点绿色片段或点“选择完成”",
                foreground="purple",
            )
            self.current_view_start = clicked_time - self.current_view_duration / 2
            self.refresh_plots()
            return

        if self.half_time_mode:
            # 这条路径理论上不会被触发（press 已处理），保留作安全冗余
            # 行为与 _apply_half_time_at_time 完全一致：命中或未命中都退出
            self._apply_half_time_at_time(clicked_time, plot_kind)
            self.refresh_plots()  # 确保颜色更新
            return

        if best_index is None:
            self.selected_segment_index = None
            self.status_label.config(
                text=f"状态：已选中{('源文件' if plot_kind == 'source' else '输出文件')} {clicked_time:.2f}s，从该处开始播放",
                foreground="blue",
            )
            self.current_view_start = clicked_time - self.current_view_duration / 2
            self.refresh_plots()
            return

        self.selected_segment_index = best_index
        start, end = self.segments[best_index]
        self.status_label.config(
            text=f"状态：已选中{('源文件' if plot_kind == 'source' else '输出文件')} {clicked_time:.2f}s，所在片段 {best_index + 1}，范围 {start / self.sr:.2f}s - {end / self.sr:.2f}s",
            foreground="blue",
        )
        self.current_view_start = clicked_time - self.current_view_duration / 2
        self.refresh_plots()

    def bring_to_front(self):
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
            self.root.after(500, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _update_selection_buttons(self):
        if self.selection_mode:
            self.selection_mode_btn.config(text="输出选中时间")
        else:
            self.selection_mode_btn.config(text="开启区间选择")

        if self.pick_detected_segment_mode:
            self.pick_segment_btn.config(text="选择完成")
        else:
            self.pick_segment_btn.config(text="选中处理片段")

        if self.half_time_mode:
            self.half_time_btn.config(text="单次减半中")
        else:
            self.half_time_btn.config(text="区间时间减半")

        if self.range_edit_mode == "add":
            self.select_range_btn.config(text="等待选择")
            self.cancel_range_btn.config(text="取消选择")
        elif self.range_edit_mode == "remove":
            self.select_range_btn.config(text="手动选择区间")
            self.cancel_range_btn.config(text="等待选择")
        else:
            self.select_range_btn.config(text="手动选择区间")
            self.cancel_range_btn.config(text="取消选择")

    def _clear_interaction_state(self):
        self.drag_start_sec = None
        self.drag_plot_kind = None
        self.pending_resize_index = None
        self.pending_resize_edge = None
        self.pending_resize_press_time = None
        self.resize_segment_index = None
        self.resize_edge = None
        self.resize_preview_time = None
        self._half_time_consumed = False

    def _apply_half_time_at_time(self, clicked_time, plot_kind):
        """减半模式下的点击处理。状态变更后由调用方负责 refresh_plots。"""
        if self.sr is None or not self.segments:
            return False

        clicked_time = float(clicked_time)
        best_index = None
        best_distance = float("inf")
        near_tolerance = min(0.40, max(0.12, self.current_view_duration * 0.03))

        # 第一优先：点在区间内部
        for index, (start, end) in enumerate(self.segments):
            start_sec = start / self.sr
            end_sec = end / self.sr
            if start_sec <= clicked_time <= end_sec:
                best_index = index
                break
            distance = min(abs(clicked_time - start_sec), abs(clicked_time - end_sec))
            if distance < best_distance and distance <= near_tolerance:
                best_index = index
                best_distance = distance

        # 第二优先：中心点近邻
        if best_index is None:
            closest_index = None
            closest_distance = float("inf")
            for index, (start, end) in enumerate(self.segments):
                start_sec = start / self.sr
                end_sec = end / self.sr
                center_sec = (start_sec + end_sec) * 0.5
                distance = abs(clicked_time - center_sec)
                if distance < closest_distance:
                    closest_distance = distance
                    closest_index = index
            if closest_index is not None and closest_distance <= near_tolerance:
                best_index = closest_index

        if best_index is None:
            self.status_label.config(text="状态：未点中绿色处理片段，已退出减半模式", foreground="blue")
            return False

        self.active_plot = plot_kind
        self.selected_time_sec = clicked_time
        start, end = self.segments[best_index]
        start_sec = start / self.sr
        end_sec = end / self.sr

        if self._segment_is_half_time(start_sec, end_sec):
            # Cancel half-time on any overlapping portion of this effective segment.
            self.half_time_ranges = subtract_time_ranges(self.half_time_ranges, [(start_sec, end_sec)])
            action_text = "已取消"
        else:
            self.half_time_ranges = merge_time_ranges(self.half_time_ranges + [(start_sec, end_sec)], min_gap_sec=0.0)
            action_text = "已设为"

        self.selected_segment_index = best_index
        self.segments_dirty = True
        self._normalize_half_time_ranges()
        self.status_label.config(
            text=f"状态：{action_text}紫色时间减半区间 {int(round(start_sec * 1000))}-{int(round(end_sec * 1000))}，已退出减半模式",
            foreground="purple",
        )
        return True

    def _save_current_config(self):
        self.app_config = {
            "atten_db": int(self.atten_slider.get()),
            "sensitivity": int(self.sensitivity_slider.get()),
            "export_bitrate_kbps": int(self.export_bitrate_var.get() or 128),
            "peak_reject": float(self.peak_reject_var.get() or 0),
            "percentile_reject": float(self.percentile_reject_var.get() or 0),
            "voice_floor": float(self.voice_floor_var.get() or 0),
            "min_segment_length_ms": float(self.min_segment_len_ms_var.get() or 0),
            "left_append_ms": float(self.left_append_ms_var.get() or 0),
            "right_append_ms": float(self.right_append_ms_var.get() or 0),
        }
        save_app_config(self.app_config)

    def _segment_is_half_time(self, start_sec, end_sec):
        if self.sr is None:
            return False
        start_sample = int(round(float(start_sec) * self.sr))
        end_sample = int(round(float(end_sec) * self.sr))
        half_samples = time_ranges_to_samples(self.half_time_ranges, self.sr, max(end_sample, 1))
        return is_half_time_sample_segment(start_sample, end_sample, half_samples, self.sr)

    def _normalize_half_time_ranges(self):
        """Keep half-time ranges clipped to current effective segments (intersection, not exact match)."""
        if self.sr is None or not self.segments:
            self.half_time_ranges = []
            return
        effective_ranges = [(start / self.sr, end / self.sr) for start, end in self.segments]
        clipped = []
        for half_start, half_end in self.half_time_ranges:
            for seg_start, seg_end in effective_ranges:
                overlap_start = max(float(half_start), float(seg_start))
                overlap_end = min(float(half_end), float(seg_end))
                if overlap_end > overlap_start:
                    clipped.append((overlap_start, overlap_end))
        self.half_time_ranges = merge_time_ranges(clipped, min_gap_sec=0.0)

    def _compute_rewrite_outputs(self, segments, half_time_ranges, atten_db, base_source, base_playback, sr, source_len):
        """CPU-heavy rewrite path; safe to call off the UI thread with snapshots."""
        half_time_segments = time_ranges_to_samples(half_time_ranges, sr, source_len)
        output_audio, timeline = render_output_audio(
            base_source,
            sr,
            segments,
            atten_db=atten_db,
            half_time_segments=half_time_segments,
        )
        if audio_buffers_share_content(base_source, base_playback):
            output_playback = output_audio
        else:
            output_playback, _ = render_output_audio(
                base_playback,
                sr,
                segments,
                atten_db=atten_db,
                half_time_segments=half_time_segments,
            )
        return {
            "output_audio": output_audio,
            "output_playback_audio": output_playback,
            "output_timeline_segments": timeline,
        }

    def _run_bg_job(self, work_fn, on_success, on_error=None, busy_text="状态：处理中...", busy_color="orange"):
        """Run work_fn in a daemon thread; apply result on UI thread with op_token/busy ownership."""
        token = self._bump_op_token()
        self._set_busy(True, busy_text, busy_color, busy_token=token)

        def worker():
            try:
                result = work_fn()
                error = None
            except Exception as exc:
                result = None
                error = exc

            def apply():
                if token != self.op_token:
                    self._release_busy_if_token(token)
                    return
                if error is not None:
                    self._release_busy_if_token(token, "状态：处理失败", "red")
                    if on_error is not None:
                        on_error(error)
                    else:
                        messagebox.showerror("错误", str(error))
                    return
                on_success(result, token)

            try:
                self.root.after(0, apply)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()
        return token

    def _rewrite_output_from_current_segments(self, async_mode=False, status_on_done=None):
        if self.source_audio is None or self.sr is None:
            return
        self._stop_player()
        segments = list(self.segments)
        half_time_ranges = list(self.half_time_ranges)
        atten_db = self.atten_slider.get()
        sr = self.sr
        source_len = len(self.source_audio)
        base_source = self.limited_source_audio if self.limited_source_audio is not None else self.source_audio
        base_playback = self.limited_playback_audio if self.limited_playback_audio is not None else (
            self.source_playback_audio if self.source_playback_audio is not None else self.source_audio
        )

        if not async_mode:
            result = self._compute_rewrite_outputs(
                segments, half_time_ranges, atten_db, base_source, base_playback, sr, source_len
            )
            if result is None:
                return
            self.output_audio = result["output_audio"]
            self.output_playback_audio = result["output_playback_audio"]
            self.output_timeline_segments = result["output_timeline_segments"]
            self.output_display_audio = self.output_audio
            self.output_has_post_processing = True
            return

        def work():
            return self._compute_rewrite_outputs(
                segments, half_time_ranges, atten_db, base_source, base_playback, sr, source_len
            )

        def on_ok(result, token):
            if result is None:
                self._release_busy_if_token(token)
                return
            self.output_audio = result["output_audio"]
            self.output_playback_audio = result["output_playback_audio"]
            self.output_timeline_segments = result["output_timeline_segments"]
            self.output_display_audio = self.output_audio
            self.output_has_post_processing = True
            done_text = status_on_done or "状态：输出已更新"
            self._release_busy_if_token(token, done_text, "blue")
            self._update_selection_buttons()
            self.refresh_plots()

        def on_err(error):
            messagebox.showerror("错误", str(error))

        self._run_bg_job(work, on_ok, on_error=on_err, busy_text="状态：正在重算输出，请稍候...")

    def _schedule_actual_output_refresh(self, output_file, parent_token=None):
        # Prefer parent export token so we do not invent a new generation while idle.
        token = parent_token if parent_token is not None else self._bump_op_token()

        def worker():
            try:
                actual_audio, _ = load_actual_output_audio(output_file)
            except Exception:
                return

            def apply_result():
                if token != self.op_token:
                    return
                self.output_display_audio = actual_audio
                self.refresh_plots()
                self.status_label.config(
                    text=f"状态：导出完成，真实 MP3 图谱已刷新：{os.path.basename(output_file)}",
                    foreground="green",
                )

            try:
                self.root.after(0, apply_result)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def export_output_file(self):
        if self.output_audio is None or self.output_playback_audio is None or self.sr is None or not self.input_path:
            self.status_label.config(text="状态：当前没有可导出的输出", foreground="blue")
            return
        if self.is_busy:
            return
        if not self.loaded_input_path or self.input_path != self.loaded_input_path:
            messagebox.showwarning("提示", "当前显示的音频与所选文件不一致，请先成功处理后再导出")
            return
        try:
            bitrate_kbps = int(np.clip(int(self.export_bitrate_var.get()), 64, 320))
        except ValueError:
            messagebox.showwarning("提示", "导出码率需要填写数字")
            return
        self.export_bitrate_var.set(str(bitrate_kbps))

        export_playback = np.asarray(self.output_playback_audio, dtype=np.float32)
        export_input_path = self.loaded_input_path
        self.output_path = str(build_output_path(export_input_path))
        output_file = Path(self.output_path)
        sr = self.sr

        def work():
            temp_out = output_file.with_name(f".{output_file.name}.partial")
            try:
                if temp_out.exists():
                    temp_out.unlink()
                write_output_mp3(export_playback, sr, temp_out, bitrate_kbps=bitrate_kbps)
                temp_out.replace(output_file)
                return str(output_file)
            except Exception:
                if temp_out.exists():
                    try:
                        temp_out.unlink()
                    except OSError:
                        pass
                raise

        def on_ok(result, token):
            self._release_busy_if_token(
                token,
                f"状态：已导出 {os.path.basename(self.output_path)}，真实 MP3 图谱将在空闲时刷新",
                "green",
            )
            self._schedule_actual_output_refresh(output_file, parent_token=token)

        def on_err(error):
            messagebox.showerror("错误", str(error))

        self._run_bg_job(
            work,
            on_ok,
            on_error=on_err,
            busy_text="状态：正在导出 MP3...",
        )

    def export_effective_segments(self):
        if self.sr is None or not self.segments:
            self.status_label.config(text="状态：当前没有可导出的区间", foreground="blue")
            return
        text = ",".join(
            f"{int(round(start / self.sr * 1000))}-{int(round(end / self.sr * 1000))}"
            for start, end in self.segments
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.status_label.config(text=f"状态：已复制全部处理区间：{text}", foreground="blue")

    def toggle_half_time_mode(self):
        if self.sr is None or not self.segments:
            return
        if self.is_busy:
            return
        self.half_time_mode = not self.half_time_mode
        self._clear_interaction_state()
        if self.half_time_mode:
            self.selection_mode = False
            self.pick_detected_segment_mode = False
            self.range_edit_mode = None
            self.status_label.config(text="状态：单次减半已开启，点击一个绿色区间即可变紫并自动退出", foreground="purple")
        else:
            self.status_label.config(text="状态：已退出区间时间减半模式", foreground="blue")
        self._update_selection_buttons()
        self.refresh_plots()

    def toggle_selection_mode(self):
        if self.is_busy:
            return
        if not self.selection_mode:
            self.selection_mode = True
            self.pick_detected_segment_mode = False
            self.half_time_mode = False
            self.range_edit_mode = None
            self.picked_detected_segments = []
            self._clear_interaction_state()
            self.status_label.config(
                text="状态：区间选择模式已开启，拖拽鼠标可选多段；完成后点“输出选中时间”",
                foreground="purple",
            )
        else:
            self.selection_mode = False
            self._clear_interaction_state()
            text = self._format_selected_time_ranges()
            if text:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.root.update_idletasks()
                self.status_label.config(text=f"状态：已复制并清空选区：{text}", foreground="blue")
            else:
                self.status_label.config(text="状态：当前没有选区可输出", foreground="blue")
            self.selected_ranges = []
        self._update_selection_buttons()
        self.refresh_plots()

    def toggle_pick_detected_segment_mode(self):
        if self.is_busy:
            return
        if not self.pick_detected_segment_mode:
            self.pick_detected_segment_mode = True
            self.selection_mode = False
            self.half_time_mode = False
            self.range_edit_mode = None
            self.picked_detected_segments = []
            self._clear_interaction_state()
            self.status_label.config(
                text="状态：选中处理片段模式已开启，点击绿色片段可累计选择，完成后再点“选择完成”",
                foreground="purple",
            )
        else:
            self.pick_detected_segment_mode = False
            if self.picked_detected_segments and self.sr is not None:
                items = []
                for idx in self.picked_detected_segments:
                    if 0 <= idx < len(self.segments):
                        start, end = self.segments[idx]
                        items.append(f"{int(round(start / self.sr * 1000))}-{int(round(end / self.sr * 1000))}")
                text = ",".join(items)
                if text:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(text)
                    self.root.update_idletasks()
                    self.status_label.config(text=f"状态：已复制所选处理片段到剪贴板：{text}", foreground="purple")
                else:
                    self.status_label.config(text="状态：未选中任何处理片段", foreground="blue")
            else:
                self.status_label.config(text="状态：未选中任何处理片段", foreground="blue")
        self._update_selection_buttons()
        self.refresh_plots()

    def clear_selected_ranges(self):
        self.selected_ranges = []
        self._clear_interaction_state()
        self.status_label.config(text="状态：已清空所有选区", foreground="blue")
        self._update_selection_buttons()
        self.refresh_plots()

    def _format_selected_time_ranges(self):
        if not self.selected_ranges:
            return ""
        parts = []
        for start_sec, end_sec in self.selected_ranges:
            start_ms = int(round(start_sec * 1000))
            end_ms = int(round(end_sec * 1000))
            parts.append(f"{start_ms}-{end_ms}")
        return ",".join(parts)

    def toggle_range_edit_mode(self, mode):
        if self.source_audio is None or self.sr is None:
            return
        if self.is_busy:
            return
        if self.range_edit_mode == mode:
            self.range_edit_mode = None
            self._clear_interaction_state()
            self.status_label.config(text="状态：已退出区间编辑模式", foreground="blue")
        else:
            self.range_edit_mode = mode
            self.selection_mode = False
            self.pick_detected_segment_mode = False
            self.half_time_mode = False
            self._clear_interaction_state()
            if mode == "add":
                self.status_label.config(text="状态：等待手动选择，拖拽鼠标后会立即补充处理区间", foreground="purple")
            else:
                self.status_label.config(text="状态：等待取消选择，点击一个已存在的绿色区间即可取消", foreground="red")
        self._update_selection_buttons()
        self.refresh_plots()

    def _rebuild_effective_segments(self, rewrite_output=True, async_rewrite=True, status_on_done=None):
        if self.source_audio is None or self.sr is None:
            return
        effective_ranges = merge_time_ranges(self.auto_segments + self.manual_segments, min_gap_sec=0.002)
        self.segments = time_ranges_to_samples(effective_ranges, self.sr, len(self.source_audio))
        self._normalize_half_time_ranges()

        if self.selected_segment_index is not None and self.selected_segment_index >= len(self.segments):
            self.selected_segment_index = len(self.segments) - 1 if self.segments else None
        if self.selected_segment_index is None and self.segments:
            self.selected_segment_index = 0

        if rewrite_output:
            self._rewrite_output_from_current_segments(
                async_mode=async_rewrite,
                status_on_done=status_on_done,
            )

    def _find_clicked_effective_range(self, clicked_time):
        candidates = []
        click_slop = 0.060
        for index, (start, end) in enumerate(self.segments):
            start_sec = start / self.sr
            end_sec = end / self.sr
            if start_sec <= clicked_time <= end_sec:
                candidates.append((0, 0.0, "effective", index, start_sec, end_sec))
            elif start_sec - click_slop <= clicked_time <= end_sec + click_slop:
                distance = min(abs(clicked_time - start_sec), abs(clicked_time - end_sec))
                candidates.append((0, distance, "effective", index, start_sec, end_sec))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, segment_kind, segment_index, start_sec, end_sec = candidates[0]
        return (segment_kind, segment_index, start_sec, end_sec)

    def _find_resize_handle(self, clicked_time):
        if self.sr is None or not self.segments:
            return None
        tolerance = min(0.12, max(0.03, self.current_view_duration * 0.015))
        best = None
        best_distance = float("inf")
        for index, (start, end) in enumerate(self.segments):
            start_sec = start / self.sr
            end_sec = end / self.sr
            for edge_name, edge_time in (("start", start_sec), ("end", end_sec)):
                distance = abs(clicked_time - edge_time)
                if distance <= tolerance and distance < best_distance:
                    best = (index, edge_name)
                    best_distance = distance
        return best

    def _replace_effective_segments(self, ranges_sec, status_on_done=None):
        merged = merge_time_ranges(ranges_sec, min_gap_sec=0.002)
        self.auto_segments = list(merged)
        self.manual_segments = []
        self.segments_dirty = True
        self._rebuild_effective_segments(rewrite_output=True, async_rewrite=True, status_on_done=status_on_done)

    def _map_half_time_on_resize(self, old_start, old_end, new_start, new_end):
        """Remap half-time intervals intersecting [old_start, old_end] onto [new_start, new_end].

        Only the overlapping portion of each half range is moved; non-overlapping
        half ranges are left untouched. Does not expand partial half coverage to
        the full new segment.
        """
        old_start = float(old_start)
        old_end = float(old_end)
        new_start = float(new_start)
        new_end = float(new_end)
        old_len = old_end - old_start
        new_len = new_end - new_start
        if old_len <= 1e-9 or new_len <= 1e-9:
            return
        remapped = []
        kept = []
        for hs, he in self.half_time_ranges:
            hs = float(hs)
            he = float(he)
            ov_s = max(hs, old_start)
            ov_e = min(he, old_end)
            if ov_e <= ov_s:
                kept.append((hs, he))
                continue
            # Drop the portion inside the old segment; keep outside remnants.
            if hs < old_start:
                kept.append((hs, min(he, old_start)))
            if he > old_end:
                kept.append((max(hs, old_end), he))
            # Map the overlapped slice proportionally into the new segment.
            r0 = (ov_s - old_start) / old_len
            r1 = (ov_e - old_start) / old_len
            mapped_s = new_start + r0 * new_len
            mapped_e = new_start + r1 * new_len
            if mapped_e > mapped_s:
                remapped.append((mapped_s, mapped_e))
        self.half_time_ranges = merge_time_ranges(kept + remapped, min_gap_sec=0.0)

    def _apply_segment_resize(self, segment_index, edge, new_time_sec):
        if self.is_busy or self.sr is None or not (0 <= segment_index < len(self.segments)):
            return
        total_duration = len(self.source_audio) / self.sr if self.source_audio is not None else 0.0
        effective_ranges = [(start / self.sr, end / self.sr) for start, end in self.segments]
        start_sec, end_sec = effective_ranges[segment_index]
        old_start_sec, old_end_sec = start_sec, end_sec
        new_time_sec = min(max(float(new_time_sec), 0.0), total_duration if total_duration > 0 else float(new_time_sec))
        min_width = 0.01
        if edge == "start":
            start_sec = min(new_time_sec, end_sec - min_width)
        else:
            end_sec = max(new_time_sec, start_sec + min_width)
        effective_ranges[segment_index] = (start_sec, end_sec)
        self._map_half_time_on_resize(old_start_sec, old_end_sec, start_sec, end_sec)
        self.segments_dirty = True
        self._replace_effective_segments(
            effective_ranges,
            status_on_done=f"状态：已调整绿色片段范围到 {start_sec:.2f}s - {end_sec:.2f}s",
        )
        self.selected_segment_index = min(segment_index, len(self.segments) - 1) if self.segments else None
        self.selected_time_sec = start_sec if edge == "start" else end_sec
        # refresh happens when async rewrite finishes; show provisional status
        self.status_label.config(
            text=f"状态：已调整绿色片段范围到 {start_sec:.2f}s - {end_sec:.2f}s，正在重算...",
            foreground="purple",
        )
        self.refresh_plots()

    def _apply_range_edit(self, start_sec, end_sec):
        edit_range = (float(start_sec), float(end_sec))
        if self.range_edit_mode == "add":
            self.manual_segments = merge_time_ranges(self.manual_segments + [edit_range], min_gap_sec=0.002)
            self.segments_dirty = True
            self._rebuild_effective_segments(
                rewrite_output=True,
                async_rewrite=True,
                status_on_done=f"状态：已手动补充处理区间 {start_sec:.2f}s - {end_sec:.2f}s，并已立即生效",
            )
            self.range_edit_mode = None
            self.status_label.config(
                text=f"状态：已手动补充处理区间 {start_sec:.2f}s - {end_sec:.2f}s，正在重算...",
                foreground="purple",
            )
        elif self.range_edit_mode == "remove":
            target = self._find_clicked_effective_range((edit_range[0] + edit_range[1]) / 2.0)
            if not target:
                self.status_label.config(
                    text=f"状态：这段没有可取消的现有处理区间 {start_sec:.2f}s - {end_sec:.2f}s",
                    foreground="blue",
                )
                self.range_edit_mode = None
                self._update_selection_buttons()
                self.refresh_plots()
                return
            segment_kind, segment_index, removed_start, removed_end = target
            effective_ranges = [(start / self.sr, end / self.sr) for start, end in self.segments]
            effective_ranges = [
                item for item in effective_ranges
                if not (abs(item[0] - removed_start) < 1e-6 and abs(item[1] - removed_end) < 1e-6)
            ]
            self.half_time_ranges = subtract_time_ranges(self.half_time_ranges, [(removed_start, removed_end)])
            self.segments_dirty = True
            self._replace_effective_segments(
                effective_ranges,
                status_on_done=f"状态：已取消处理区间 {removed_start:.2f}s - {removed_end:.2f}s，后续仍可重新手动选择这里",
            )
            self.range_edit_mode = None
            self.status_label.config(
                text=f"状态：已取消处理区间 {removed_start:.2f}s - {removed_end:.2f}s，正在重算...",
                foreground="red",
            )
        self._update_selection_buttons()
        self.refresh_plots()

    def copy_diagnostics(self):
        text = self.debug_text.get()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.status_label.config(text="状态：诊断信息已复制到剪贴板", foreground="blue")

    def _stop_player(self):
        if self.playback_job is not None:
            self.root.after_cancel(self.playback_job)
            self.playback_job = None
        self.playback_start_wall_time = None
        self.playback_start_audio_time = None
        self.playback_duration = None
        self.is_paused = False
        if self.player_process and self.player_process.poll() is None:
            self.player_process.terminate()
            try:
                self.player_process.wait(timeout=1.0)
            except Exception:
                try:
                    self.player_process.kill()
                except Exception:
                    pass
        self.player_process = None
        if self.playback_temp_path and os.path.exists(self.playback_temp_path):
            try:
                os.remove(self.playback_temp_path)
            except OSError:
                pass
        self.playback_temp_path = None
        self._update_play_toggle_buttons()

    def _finish_active_playback(self):
        if self.playback_job is not None:
            self.root.after_cancel(self.playback_job)
            self.playback_job = None
        if self.player_process and self.player_process.poll() is None:
            self.player_process.terminate()
            try:
                self.player_process.wait(timeout=1.0)
            except Exception:
                try:
                    self.player_process.kill()
                except Exception:
                    pass
        self.player_process = None
        if self.playback_temp_path and os.path.exists(self.playback_temp_path):
            try:
                os.remove(self.playback_temp_path)
            except OSError:
                pass
        self.playback_temp_path = None
        self.playback_start_wall_time = None
        self.playback_start_audio_time = None
        self.playback_duration = None
        self.is_paused = False
        self._update_play_toggle_buttons()

    def _play_file(self, path, owns_temp=False):
        self._stop_player()
        if owns_temp:
            self.playback_temp_path = path
        self.player_process = subprocess.Popen(["afplay", path])
        self._update_play_toggle_buttons()

    def _start_playback_tracking(self, start_time_sec, duration_sec, plot_kind):
        self.playback_start_wall_time = self.root.winfo_toplevel().tk.call("clock", "milliseconds")
        self.playback_start_audio_time = float(start_time_sec)
        self.playback_duration = float(duration_sec)
        self.playback_plot_kind = plot_kind
        self.active_plot = plot_kind
        self.is_paused = False
        self._update_play_toggle_buttons()
        self._schedule_playback_tick()

    def _schedule_playback_tick(self):
        if self.playback_start_wall_time is None or self.playback_start_audio_time is None:
            return
        if self.is_paused:
            self.playback_job = self.root.after(80, self._schedule_playback_tick)
            return
        if self.player_process is None or self.player_process.poll() is not None:
            end_time = self.selected_time_sec
            if self.playback_duration is not None:
                end_audio_time = self.playback_start_audio_time + self.playback_duration
                if self.playback_plot_kind == "output":
                    end_time = self._map_output_sample_to_source_sample(int(round(end_audio_time * self.sr))) / self.sr
                else:
                    end_time = end_audio_time
            if end_time is not None:
                self.selected_time_sec = end_time
                self._update_playhead_display(follow_playback=True)
            self._finish_active_playback()
            return

        now_ms = int(self.root.winfo_toplevel().tk.call("clock", "milliseconds"))
        elapsed = max(0.0, (now_ms - self.playback_start_wall_time) / 1000.0)
        if self.playback_duration is not None and elapsed > self.playback_duration:
            end_audio_time = self.playback_start_audio_time + self.playback_duration
            if self.playback_plot_kind == "output":
                self.selected_time_sec = self._map_output_sample_to_source_sample(int(round(end_audio_time * self.sr))) / self.sr
            else:
                self.selected_time_sec = end_audio_time
            self._update_playhead_display(follow_playback=True)
            self._finish_active_playback()
            return

        current_audio_time = self.playback_start_audio_time + elapsed
        if self.playback_plot_kind == "output":
            self.selected_time_sec = self._map_output_sample_to_source_sample(int(round(current_audio_time * self.sr))) / self.sr
        else:
            self.selected_time_sec = current_audio_time
        self._update_playhead_display(follow_playback=True)
        self.playback_job = self.root.after(80, self._schedule_playback_tick)

    def play_output_audio(self):
        if not self.output_path or not os.path.exists(self.output_path):
            messagebox.showwarning("提示", "输出文件不存在，请先处理")
            return
        self._play_file(self.output_path)

    def play_active_selection(self, processed):
        if self.selected_time_sec is None or self.sr is None:
            messagebox.showwarning("提示", "请先点击图上的位置")
            return

        audio = self.output_playback_audio if processed else self.source_playback_audio
        if audio is None:
            return

        requested_source_time = float(max(0, self.selected_time_sec))
        start_sample = int(requested_source_time * self.sr)
        if processed:
            start_sample = self._map_source_sample_to_output_sample(start_sample)
        if start_sample >= len(audio):
            return

        end_sample = len(audio)
        clip = audio[start_sample:end_sample]
        if len(clip) == 0:
            return

        self._stop_player()
        temp_audio = tempfile.NamedTemporaryFile(prefix="breath_cursor_long_", suffix=".wav", delete=False)
        temp_audio.close()
        sf.write(temp_audio.name, np.asarray(clip, dtype=np.float32), self.sr)
        self.playback_temp_path = temp_audio.name
        self.player_process = subprocess.Popen(["afplay", temp_audio.name])
        self.last_playback_anchor_sec = requested_source_time
        self.last_playback_target = "output" if processed else "source"
        self._start_playback_tracking(
            start_sample / self.sr,
            len(clip) / self.sr,
            "output" if processed else "source",
        )

    def _resume_active_playback(self):
        resume_from = self.selected_time_sec if self.selected_time_sec is not None else self.playback_start_audio_time
        audio = self.output_playback_audio if self.playback_plot_kind == "output" else self.source_playback_audio
        if audio is None or self.sr is None:
            return
        start_sample = int(max(0, resume_from) * self.sr)
        if self.playback_plot_kind == "output":
            start_sample = self._map_source_sample_to_output_sample(start_sample)
            resume_from = start_sample / self.sr
        clip = audio[start_sample:]
        if len(clip) == 0:
            return
        if self.playback_temp_path and os.path.exists(self.playback_temp_path):
            try:
                os.remove(self.playback_temp_path)
            except OSError:
                pass
        temp_audio = tempfile.NamedTemporaryFile(prefix="breath_cursor_resume_", suffix=".wav", delete=False)
        temp_audio.close()
        sf.write(temp_audio.name, np.asarray(clip, dtype=np.float32), self.sr)
        self.playback_temp_path = temp_audio.name
        self.player_process = subprocess.Popen(["afplay", temp_audio.name])
        now_ms = int(self.root.winfo_toplevel().tk.call("clock", "milliseconds"))
        self.playback_start_wall_time = now_ms
        self.playback_start_audio_time = resume_from
        self.playback_duration = len(clip) / self.sr
        self.is_paused = False
        self._update_play_toggle_buttons()
        self._schedule_playback_tick()

    def _pause_active_playback(self):
        if self.player_process and self.player_process.poll() is None:
            self.player_process.terminate()
            try:
                self.player_process.wait(timeout=1.0)
            except Exception:
                try:
                    self.player_process.kill()
                except Exception:
                    pass
        self.player_process = None
        self.is_paused = True
        self._update_play_toggle_buttons()

    def _update_play_toggle_buttons(self):
        source_text = "播放原文件"
        output_text = "播放输出文件"
        if self.playback_plot_kind == "source" and self.playback_start_audio_time is not None:
            source_text = "暂停原文件" if not self.is_paused else "继续原文件"
        if self.playback_plot_kind == "output" and self.playback_start_audio_time is not None:
            output_text = "暂停输出文件" if not self.is_paused else "继续输出文件"
        self.play_active_source_btn.config(text=source_text)
        self.play_active_output_btn.config(text=output_text)

    def toggle_active_playback(self, processed):
        if self.is_busy:
            return
        target = "output" if processed else "source"
        if self.playback_plot_kind == target and self.playback_start_audio_time is not None:
            if self.is_paused:
                self._resume_active_playback()
                self.status_label.config(text=f"状态：继续播放{('输出文件' if processed else '原文件')}", foreground="blue")
            else:
                self._pause_active_playback()
                self.status_label.config(text=f"状态：已暂停{('输出文件' if processed else '原文件')}", foreground="blue")
            return
        total_duration = len(self.source_audio) / self.sr if self.source_audio is not None and self.sr else None
        if (
            total_duration is not None
            and self.selected_time_sec is not None
            and self.selected_time_sec >= total_duration - 0.05
        ):
            self.selected_time_sec = 0.0
            self._update_playhead_display(follow_playback=True, force_refresh=True)
        self.play_active_selection(processed)
        self.status_label.config(text=f"状态：开始从当前选中位置播放{('输出文件' if processed else '原文件')}", foreground="blue")

    def play_selected_segment(self, processed):
        if self.selected_segment_index is None or self.sr is None:
            messagebox.showwarning("提示", "请先点击绿色片段")
            return

        audio = self.output_playback_audio if processed else self.source_playback_audio
        if audio is None:
            return

        start, end = self.segments[self.selected_segment_index]
        if processed:
            clip_start = self._map_source_sample_to_output_sample(start)
            clip_end = self._map_source_sample_to_output_sample(end)
        else:
            clip_start, clip_end = start, end
        clip = audio[clip_start:clip_end]
        if len(clip) == 0:
            return

        temp_audio = tempfile.NamedTemporaryFile(prefix="breath_clip_", suffix=".wav", delete=False)
        temp_audio.close()
        sf.write(temp_audio.name, clip, self.sr)
        self._play_file(temp_audio.name, owns_temp=True)


if __name__ == "__main__":
    root = tk.Tk()
    app = BreathReducerApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app._save_current_config(), app._stop_player(), root.destroy()))
    root.mainloop()
