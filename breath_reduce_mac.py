import json
import os
import shutil
import subprocess
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import librosa
import numpy as np
import soundfile as sf
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib import rcParams

VERSION = 60
HOP_LENGTH = 512
LEFT_APPEND_MS = 20.0
RIGHT_APPEND_MS = 0.0
MIN_MANUAL_DRAG_SEC = 0.03
APP_CONFIG_PATH = Path.home() / "Library" / "Application Support" / "musicdoubao" / "config.json"

rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False


def _load_app_config():
    defaults = {
        "atten_db": 30,
        "sensitivity": 10,
        "peak_reject": 3.0,
        "percentile_reject": 20.0,
        "voice_floor": 2.0,
        "left_append_ms": LEFT_APPEND_MS,
        "right_append_ms": RIGHT_APPEND_MS,
    }
    try:
        if APP_CONFIG_PATH.exists():
            with APP_CONFIG_PATH.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                defaults.update(loaded)
    except Exception:
        pass
    return defaults


def _save_app_config(config):
    try:
        APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with APP_CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _percentile_norm(values, low=5, high=95):
    values = np.asarray(values, dtype=np.float32)
    lo = float(np.percentile(values, low))
    hi = float(np.percentile(values, high))
    return np.clip((values - lo) / (hi - lo + 1e-6), 0.0, 1.0)


def _moving_average(values, window_size):
    values = np.asarray(values, dtype=np.float32)
    window_size = max(1, int(window_size))
    if window_size == 1:
        return values
    kernel = np.ones(window_size, dtype=np.float32) / window_size
    smoothed = np.convolve(values, kernel, mode="same")
    if len(smoothed) == len(values):
        return smoothed
    if len(smoothed) < len(values):
        padded = np.zeros_like(values)
        padded[: len(smoothed)] = smoothed
        return padded
    extra = len(smoothed) - len(values)
    start = extra // 2
    end = start + len(values)
    return smoothed[start:end]


def _close_mask(mask, gap_tolerance):
    mask = np.asarray(mask, dtype=bool)
    if gap_tolerance <= 0 or mask.size == 0:
        return mask

    closed = mask.copy()
    false_run_start = None
    for idx, value in enumerate(mask):
        if not value and false_run_start is None:
            false_run_start = idx
        elif value and false_run_start is not None:
            if idx - false_run_start <= gap_tolerance:
                closed[false_run_start:idx] = True
            false_run_start = None
    return closed


def _merge_segments(mask, frame_time, sr, min_duration=0.09, max_duration=0.60):
    segments = []
    active = np.flatnonzero(mask)
    if len(active) == 0:
        return segments

    start_frame = active[0]
    prev_frame = active[0]

    for frame in active[1:]:
        if frame - prev_frame <= 2:
            prev_frame = frame
            continue

        start = int(start_frame * frame_time * sr)
        end = int((prev_frame + 1) * frame_time * sr)
        duration = (end - start) / sr
        if min_duration <= duration <= max_duration:
            segments.append((start, end))

        start_frame = frame
        prev_frame = frame

    start = int(start_frame * frame_time * sr)
    end = int((prev_frame + 1) * frame_time * sr)
    duration = (end - start) / sr
    if min_duration <= duration <= max_duration:
        segments.append((start, end))
    return segments


def _sample_slice(values, start_frame, end_frame):
    start_frame = max(0, start_frame)
    end_frame = min(len(values), end_frame)
    if end_frame <= start_frame:
        return np.asarray([], dtype=np.float32)
    return np.asarray(values[start_frame:end_frame], dtype=np.float32)


def _linear_slope(values):
    values = np.asarray(values, dtype=np.float32)
    if len(values) < 2:
        return 0.0
    x = np.arange(len(values), dtype=np.float32)
    x = x - np.mean(x)
    y = values - np.mean(values)
    denom = float(np.sum(x * x)) + 1e-6
    return float(np.sum(x * y) / denom)


def _merge_nearby_segments(segments, sr, max_gap=0.10, max_duration=0.45):
    if not segments:
        return []

    merged = [dict(segments[0])]
    max_gap_samples = int(max_gap * sr)
    max_duration_samples = int(max_duration * sr)

    for item in segments[1:]:
        prev = merged[-1]
        gap = item["start"] - prev["end"]
        combined_duration = item["end"] - prev["start"]
        if gap <= max_gap_samples and combined_duration <= max_duration_samples:
            prev["end"] = item["end"]
            prev["duration"] = (prev["end"] - prev["start"]) / sr
            prev["mean_score"] = max(prev["mean_score"], item["mean_score"])
            prev["peak_score"] = max(prev["peak_score"], item["peak_score"])
            prev["mean_rms"] = min(prev["mean_rms"], item["mean_rms"])
            prev["rise_ratio"] = max(prev["rise_ratio"], item["rise_ratio"])
            prev["texture_score"] = max(prev["texture_score"], item["texture_score"])
        else:
            merged.append(dict(item))
    return merged


def _expand_segment_edges(segments, sr, frame_time, relaxed_threshold, energy_ceiling, smoothed_score, rms_norm, zcr_norm, centroid_norm):
    if not segments:
        return []

    expanded = []
    max_expand_frames = max(1, int(round(0.24 / frame_time)))
    edge_threshold = max(0.11, relaxed_threshold - 0.10)
    edge_energy_limit = min(energy_ceiling * 0.24, 0.13)

    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = min(len(smoothed_score) - 1, int(np.ceil(end / HOP_LENGTH)))

        new_start = start_frame
        for _ in range(max_expand_frames):
            probe = new_start - 1
            if probe < 0:
                break
            if (
                smoothed_score[probe] >= edge_threshold
                or (
                    rms_norm[probe] <= edge_energy_limit
                    and zcr_norm[probe] >= 0.20
                    and centroid_norm[probe] >= 0.19
                )
            ):
                new_start = probe
            else:
                break

        new_end = end_frame
        for _ in range(max_expand_frames):
            probe = new_end + 1
            if probe >= len(smoothed_score):
                break
            if (
                smoothed_score[probe] >= edge_threshold
                or (
                    rms_norm[probe] <= edge_energy_limit
                    and zcr_norm[probe] >= 0.20
                    and centroid_norm[probe] >= 0.19
                )
            ):
                new_end = probe
            else:
                break

        expanded.append((int(new_start * frame_time * sr), int((new_end + 1) * frame_time * sr)))

    return expanded


def _refine_segment_to_core(segments, sr, frame_time, smoothed_score, rms_norm, zcr_norm, centroid_norm):
    refined = []
    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = max(start_frame + 1, int(np.ceil(end / HOP_LENGTH)))
        seg_rms = rms_norm[start_frame:end_frame]
        seg_score = smoothed_score[start_frame:end_frame]
        seg_zcr = zcr_norm[start_frame:end_frame]
        seg_cent = centroid_norm[start_frame:end_frame]
        if len(seg_rms) == 0:
            continue

        # 如果整段能量明显偏高，只保留其中更像吸气的低能量核心
        if float(np.mean(seg_rms)) > 0.10:
            core_mask = (
                (seg_rms < min(float(np.mean(seg_rms)) * 0.55, 0.12))
                & (seg_score > 0.20)
                & (seg_zcr > 0.36)
                & (seg_cent > 0.38)
            )
            core_segments = _merge_segments(core_mask, frame_time, sr, min_duration=0.06, max_duration=0.52)
            if core_segments:
                base = start_frame * frame_time * sr
                for core_start, core_end in core_segments:
                    refined.append((int(base + core_start), int(base + core_end)))
                continue

        refined.append((start, end))

    return refined


def _trim_segment_heads_tails(segments, sr, frame_time, smoothed_score, rms_norm, zcr_norm, centroid_norm, bandwidth_norm):
    trimmed = []
    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = max(start_frame + 1, int(np.ceil(end / HOP_LENGTH)))
        seg_score = smoothed_score[start_frame:end_frame]
        seg_rms = rms_norm[start_frame:end_frame]
        seg_zcr = zcr_norm[start_frame:end_frame]
        seg_cent = centroid_norm[start_frame:end_frame]
        seg_bw = bandwidth_norm[start_frame:end_frame]
        if len(seg_score) == 0:
            continue

        core_mask = _close_mask(
            (seg_rms < 0.09)
            & (
                (seg_score > 0.25)
                | ((seg_zcr > 0.47) & (seg_cent > 0.45) & (seg_bw > 0.44))
            ),
            2,
        )
        voice_mask = _close_mask(
            (
                ((seg_rms > 0.08) & (seg_score < 0.26) & (seg_zcr < 0.42))
                | ((seg_rms > 0.10) & (seg_cent < 0.44) & (seg_bw < 0.45))
                | ((seg_rms > 0.12) & (seg_score < 0.33))
            ),
            2,
        )

        active = np.flatnonzero(core_mask)
        if len(active) == 0:
            trimmed.append((start, end))
            continue

        runs = []
        run_start = active[0]
        prev = active[0]
        for frame in active[1:]:
            if frame - prev <= 2:
                prev = frame
                continue
            runs.append((run_start, prev))
            run_start = frame
            prev = frame
        runs.append((run_start, prev))

        best_start, best_end = max(runs, key=lambda item: item[1] - item[0])

        edge_window = max(2, int(round(0.045 / frame_time)))
        edge_guard = max(1, int(round(0.020 / frame_time)))
        left_voice = bool(np.count_nonzero(voice_mask[:edge_window]) >= max(2, edge_window // 2))
        right_voice = bool(np.count_nonzero(voice_mask[-edge_window:]) >= max(2, edge_window // 2))

        trim_start_frame = start_frame
        trim_end_frame = end_frame - 1
        if left_voice and best_start > edge_guard:
            trim_start_frame = start_frame + max(0, best_start - edge_guard)
        if right_voice and (len(seg_score) - 1 - best_end) > edge_guard:
            trim_end_frame = start_frame + min(len(seg_score) - 1, best_end + edge_guard)

        trimmed_start = int(trim_start_frame * frame_time * sr)
        trimmed_end = int((trim_end_frame + 1) * frame_time * sr)
        duration = (trimmed_end - trimmed_start) / sr
        removed_ratio = 1.0 - (duration / max((end - start) / sr, 1e-6))
        if 0.08 <= duration <= 0.65 and removed_ratio <= 0.60:
            trimmed.append((trimmed_start, trimmed_end))
        else:
            trimmed.append((start, end))

    return trimmed


def _extend_breath_edges(segments, sr, frame_time, smoothed_score, rms_norm, zcr_norm, centroid_norm, bandwidth_norm):
    extended = []
    if not segments:
        return extended

    max_extend_frames = max(1, int(round(0.26 / frame_time)))
    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = max(start_frame + 1, int(np.ceil(end / HOP_LENGTH)))

        new_start = start_frame
        for _ in range(max_extend_frames):
            probe = new_start - 1
            if probe < 0:
                break
            if (
                rms_norm[probe] <= 0.16
                and smoothed_score[probe] >= 0.13
                and zcr_norm[probe] >= 0.31
                and centroid_norm[probe] >= 0.31
            ):
                new_start = probe
            else:
                break

        new_end = end_frame - 1
        for _ in range(max_extend_frames):
            probe = new_end + 1
            if probe >= len(smoothed_score):
                break
            if (
                rms_norm[probe] <= 0.16
                and smoothed_score[probe] >= 0.13
                and zcr_norm[probe] >= 0.31
                and centroid_norm[probe] >= 0.31
            ):
                new_end = probe
            else:
                break

        extended.append((int(new_start * frame_time * sr), int((new_end + 1) * frame_time * sr)))
    return extended


def _trim_loud_edges_by_threshold(segments, sr, frame_time, raw_rms, peak_reject_threshold, percentile_reject_threshold):
    trimmed = []
    if not segments:
        return trimmed

    edge_window = max(1, int(round(0.035 / frame_time)))
    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = max(start_frame + 1, int(np.ceil(end / HOP_LENGTH)))
        seg_raw = raw_rms[start_frame:end_frame]
        if len(seg_raw) == 0:
            continue

        left = 0
        right = len(seg_raw) - 1
        while left <= right:
            left_slice = seg_raw[left : min(len(seg_raw), left + edge_window)]
            if (
                seg_raw[left] > peak_reject_threshold + 1e-6
                or float(np.max(left_slice)) > peak_reject_threshold + 1e-6
                or float(np.percentile(left_slice, 80)) > percentile_reject_threshold + 1e-6
            ):
                left += 1
                continue
            break
        while right >= left:
            right_slice = seg_raw[max(0, right - edge_window + 1) : right + 1]
            if (
                seg_raw[right] > peak_reject_threshold + 1e-6
                or float(np.max(right_slice)) > peak_reject_threshold + 1e-6
                or float(np.percentile(right_slice, 80)) > percentile_reject_threshold + 1e-6
            ):
                right -= 1
                continue
            break

        if right < left:
            continue

        new_start = start_frame + left
        new_end = start_frame + right + 1
        duration = (new_end - new_start) * frame_time
        if duration < 0.035:
            continue

        remaining = raw_rms[new_start:new_end]
        if len(remaining) == 0:
            continue
        if float(np.max(remaining)) > peak_reject_threshold + 1e-6:
            continue
        if float(np.percentile(remaining, 90)) > percentile_reject_threshold + 1e-6:
            continue

        trimmed.append((int(new_start * frame_time * sr), int(new_end * frame_time * sr)))
    return trimmed


def _trim_rising_voice_right_edges(segments, sr, frame_time, raw_rms, peak_reject_threshold, percentile_reject_threshold, voice_floor_threshold):
    trimmed = []
    if not segments:
        return trimmed

    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = max(start_frame + 1, int(np.ceil(end / HOP_LENGTH)))
        seg_raw = np.asarray(raw_rms[start_frame:end_frame], dtype=np.float32)
        if len(seg_raw) < 5:
            trimmed.append((start, end))
            continue

        smoothed = _moving_average(seg_raw, 3)
        low_floor = float(np.percentile(smoothed, 20))
        tail_start = max(1, len(smoothed) // 2)
        trim_at = None

        for idx in range(tail_start, len(smoothed) - 2):
            current = float(smoothed[idx])
            next_1 = float(smoothed[idx + 1])
            next_2 = float(smoothed[idx + 2])
            suffix_max = float(np.max(smoothed[idx:]))
            voice_gate = max(
                low_floor * 2.2,
                voice_floor_threshold * 1.35 if voice_floor_threshold > 0 else 0.0,
                percentile_reject_threshold * 0.58,
                peak_reject_threshold * 0.48,
                0.01,
            )
            suffix_gate = max(
                low_floor * 3.0,
                percentile_reject_threshold * 0.78,
                peak_reject_threshold * 0.68,
                voice_gate * 1.18,
            )
            if (
                current >= voice_gate
                and next_1 >= current * 1.04
                and next_2 >= next_1 * 1.02
                and suffix_max >= suffix_gate
            ):
                trim_at = idx
                break

        if trim_at is None:
            trimmed.append((start, end))
            continue

        new_end_frame = start_frame + max(1, trim_at - 1)
        new_end = int(new_end_frame * frame_time * sr)
        if new_end - start >= int(0.04 * sr):
            trimmed.append((start, new_end))
        else:
            trimmed.append((start, end))

    return trimmed


def _trim_following_voice_onset(segments, sr, frame_time, raw_rms, peak_reject_threshold, percentile_reject_threshold, voice_floor_threshold):
    trimmed = []
    if not segments:
        return trimmed

    look_ahead_frames = max(3, int(round(0.12 / frame_time)))
    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = max(start_frame + 1, int(np.ceil(end / HOP_LENGTH)))
        seg_raw = np.asarray(raw_rms[start_frame:end_frame], dtype=np.float32)
        follow_raw = np.asarray(raw_rms[end_frame : end_frame + look_ahead_frames], dtype=np.float32)
        if len(seg_raw) < 4 or len(follow_raw) < 3:
            trimmed.append((start, end))
            continue

        seg_smooth = _moving_average(seg_raw, 3)
        follow_smooth = _moving_average(follow_raw, 3)
        combined = np.concatenate(
            (
                seg_smooth[max(0, len(seg_smooth) - max(4, len(seg_smooth) // 3)) :],
                follow_smooth,
            )
        )
        seg_floor = float(np.percentile(seg_smooth, 25))
        seg_tail = float(np.percentile(seg_smooth[max(0, len(seg_smooth) - max(2, len(seg_smooth) // 4)) :], 70))
        voice_gate = max(
            seg_floor * 1.75,
            seg_tail * 1.08,
            voice_floor_threshold * 1.30 if voice_floor_threshold > 0 else 0.0,
            percentile_reject_threshold * 0.48,
            peak_reject_threshold * 0.40,
            0.010,
        )

        onset_idx = None
        tail_offset = len(combined) - len(follow_smooth)
        for idx in range(0, len(combined) - 2):
            a = float(combined[idx])
            b = float(combined[idx + 1])
            c = float(combined[idx + 2])
            if (
                a >= voice_gate
                and b >= a * 1.015
                and c >= b * 1.01
                and max(a, b, c) >= max(percentile_reject_threshold * 0.60, peak_reject_threshold * 0.50, voice_gate * 1.03)
            ):
                onset_idx = idx
                break

        if onset_idx is None:
            trimmed.append((start, end))
            continue

        cut_in_seg = min(max(1, onset_idx), tail_offset)
        low_cut_gate = max(seg_floor * 1.08, voice_floor_threshold * 1.02 if voice_floor_threshold > 0 else 0.0, 0.006)
        back_cut = max(1, len(seg_smooth) - max(4, len(seg_smooth) // 3))
        for idx in range(min(len(seg_smooth) - 1, cut_in_seg), max(0, cut_in_seg - 4) - 1, -1):
            if float(seg_smooth[idx]) <= low_cut_gate:
                back_cut = idx
                break

        new_end_frame = start_frame + max(1, back_cut)
        new_end = int(new_end_frame * frame_time * sr)
        if new_end - start >= int(0.04 * sr):
            trimmed.append((start, new_end))
        else:
            trimmed.append((start, end))

    return trimmed


def _snap_right_edge_to_tail_valley(segments, sr, frame_time, raw_rms, peak_reject_threshold, voice_floor_threshold):
    snapped = []
    if not segments:
        return snapped

    look_ahead_frames = max(6, int(round(0.20 / frame_time)))
    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = max(start_frame + 1, int(np.ceil(end / HOP_LENGTH)))
        seg_raw = np.asarray(raw_rms[start_frame:end_frame], dtype=np.float32)
        follow_raw = np.asarray(raw_rms[end_frame : end_frame + look_ahead_frames], dtype=np.float32)
        if len(seg_raw) < 4 or len(follow_raw) < 3:
            snapped.append((start, end))
            continue

        combined = np.concatenate((seg_raw, follow_raw))
        if len(combined) < 5:
            snapped.append((start, end))
            continue

        below_threshold = peak_reject_threshold + 1e-6
        entry_idx = None
        for idx in range(0, len(seg_raw) - 1):
            if float(seg_raw[idx]) <= below_threshold and float(seg_raw[idx + 1]) <= max(below_threshold * 1.10, below_threshold + 0.002):
                entry_idx = idx
                break

        if entry_idx is None:
            snapped.append((start, end))
            continue

        crossing_idx = None
        for idx in range(entry_idx + 1, len(combined)):
            if float(combined[idx]) > below_threshold:
                crossing_idx = idx
                break

        if crossing_idx is None:
            snapped.append((start, end))
            continue

        search_start = max(entry_idx, crossing_idx - max(18, len(seg_raw) // 2))
        search_end = max(search_start + 1, crossing_idx)
        best_valley_idx = None
        for idx in range(search_start, search_end):
            cur = float(combined[idx])
            prev = float(combined[idx - 1])
            nxt = float(combined[idx + 1]) if idx + 1 < len(combined) else cur
            if cur <= prev + 1e-6 and cur <= nxt + 1e-6 and cur <= peak_reject_threshold + 1e-6:
                best_valley_idx = idx

        if best_valley_idx is None:
            for idx in range(search_end - 1, search_start - 1, -1):
                cur = float(combined[idx])
                prev = float(combined[idx - 1]) if idx - 1 >= 0 else cur
                if cur <= prev + 1e-6 and cur <= peak_reject_threshold + 1e-6:
                    best_valley_idx = idx
                    break
        if best_valley_idx is None:
            window = combined[search_start:search_end]
            best_valley_idx = search_start + int(np.argmin(window))

        cut_idx = max(entry_idx, min(best_valley_idx, len(seg_raw) - 1))
        new_end_frame = start_frame + cut_idx
        new_end = int(new_end_frame * frame_time * sr)
        if new_end - start >= int(0.04 * sr):
            snapped.append((start, new_end))
        else:
            snapped.append((start, end))

    return snapped


def _detect_low_voice_silence_segments(raw_rms, frame_time, sr, voice_floor_threshold):
    if voice_floor_threshold <= 0.0:
        return []
    silence_mask = _close_mask(
        raw_rms <= voice_floor_threshold + 1e-6,
        max(1, int(round(0.12 / frame_time))),
    )
    return _merge_segments(silence_mask, frame_time, sr, min_duration=0.08, max_duration=20.0)


def _detect_breath_segments(y, sr, sensitivity, peak_reject_threshold=0.20, percentile_reject_threshold=0.20, voice_floor_threshold=0.0):
    frame_time = HOP_LENGTH / sr
    rms = librosa.feature.rms(y=y, hop_length=HOP_LENGTH)[0]
    flatness = librosa.feature.spectral_flatness(y=y, hop_length=HOP_LENGTH)[0]
    zcr = librosa.feature.zero_crossing_rate(y, hop_length=HOP_LENGTH)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP_LENGTH)[0]
    bandwidth = librosa.feature.spectral_bandwidth(y=y, sr=sr, hop_length=HOP_LENGTH)[0]
    raw_rms = np.asarray(rms, dtype=np.float32)

    rms_norm = _percentile_norm(rms)
    flatness_norm = _percentile_norm(flatness)
    zcr_norm = _percentile_norm(zcr)
    centroid_norm = _percentile_norm(centroid)
    bandwidth_norm = _percentile_norm(bandwidth)
    global_voice_p75 = float(np.percentile(raw_rms, 75))
    global_voice_p87 = float(np.percentile(raw_rms, 87))
    global_noise_p35 = float(np.percentile(raw_rms, 35))

    sensitivity = int(np.clip(sensitivity, 1, 10))
    energy_ceiling = np.clip(0.52 + (10 - sensitivity) * 0.045, 0.50, 0.92)
    energy_floor = np.clip(0.004 + (1 / max(sensitivity, 1)) * 0.01, 0.003, 0.02)
    noise_floor = np.clip(0.30 - sensitivity * 0.017, 0.08, 0.28)

    lead_rms = np.pad(rms_norm[4:], (0, 4), mode="edge")
    breath_score = (
        0.30 * flatness_norm
        + 0.22 * zcr_norm
        + 0.18 * centroid_norm
        + 0.15 * bandwidth_norm
        + 0.15 * np.clip(lead_rms - rms_norm, 0.0, 1.0)
    )
    smooth_frames = max(3, int(round(0.12 / frame_time)))
    smoothed_score = _moving_average(breath_score, smooth_frames)

    candidate_mask = (
        (rms_norm > energy_floor)
        & (rms_norm < energy_ceiling)
        & (
            (flatness_norm > noise_floor)
            | (zcr_norm > max(0.08, noise_floor - 0.02))
            | (centroid_norm > max(0.08, noise_floor - 0.01))
            | (bandwidth_norm > max(0.08, noise_floor - 0.01))
        )
    )
    low_voice_frame_mask = (
        voice_floor_threshold > 0.0
    ) & (raw_rms <= voice_floor_threshold + 1e-6)
    candidate_mask = candidate_mask | low_voice_frame_mask

    strict_threshold = np.clip(0.55 - sensitivity * 0.028, 0.22, 0.50)
    relaxed_threshold = np.clip(strict_threshold - 0.12, 0.14, 0.42)
    strict_mask = (candidate_mask & (smoothed_score > strict_threshold)) | low_voice_frame_mask
    relaxed_mask = (candidate_mask & (smoothed_score > relaxed_threshold)) | low_voice_frame_mask
    post_phrase_mask = (
        (smoothed_score > max(0.18, relaxed_threshold - 0.04))
        & (rms_norm < 0.13)
        & (zcr_norm > 0.40)
        & (centroid_norm > 0.40)
        & (bandwidth_norm > 0.42)
        & (lead_rms > np.maximum(rms_norm * 1.35, 0.11))
    )
    low_energy_breath_mask = (
        (smoothed_score > max(0.20, relaxed_threshold - 0.02))
        & (rms_norm < 0.10)
        & (flatness_norm > 0.07)
        & (zcr_norm > 0.42)
        & (centroid_norm > 0.41)
        & (bandwidth_norm > 0.43)
    )
    rescue_mask = (
        (smoothed_score > max(0.18, strict_threshold - 0.08))
        & (rms_norm > energy_floor * 0.8)
        & (rms_norm < min(energy_ceiling * 0.60, 0.20))
        & (zcr_norm > 0.22)
        & (centroid_norm > 0.22)
    )
    bright_breath_mask = (
        (smoothed_score > max(0.26, strict_threshold))
        & (rms_norm < 0.08)
        & (zcr_norm > 0.50)
        & (centroid_norm > 0.48)
        & (bandwidth_norm > 0.45)
    )
    airy_breath_mask = (
        (smoothed_score > max(0.21, strict_threshold - 0.05))
        & (rms_norm < 0.09)
        & (zcr_norm > 0.44)
        & (centroid_norm > 0.45)
        & (bandwidth_norm > 0.45)
    )
    micro_breath_mask = (
        (smoothed_score > 0.24)
        & (rms_norm < 0.085)
        & (zcr_norm > 0.46)
        & (centroid_norm > 0.45)
        & (bandwidth_norm > 0.45)
    )
    short_breath_mask = (
        (smoothed_score > max(0.20, strict_threshold - 0.06))
        & (rms_norm > energy_floor * 0.7)
        & (rms_norm < 0.11)
        & (flatness_norm > 0.11)
        & (zcr_norm > 0.43)
        & (centroid_norm > 0.43)
        & (bandwidth_norm > 0.42)
    )
    needle_breath_mask = (
        (smoothed_score > max(0.30, strict_threshold + 0.02))
        & (rms_norm < 0.08)
        & (zcr_norm > 0.56)
        & (centroid_norm > 0.52)
        & (bandwidth_norm > 0.50)
    )
    core_breath_mask = (
        (smoothed_score > max(0.23, strict_threshold - 0.02))
        & (rms_norm < 0.10)
        & (zcr_norm > 0.42)
        & (centroid_norm > 0.42)
        & (bandwidth_norm > 0.44)
    )
    relaxed_mask = relaxed_mask | rescue_mask | post_phrase_mask | low_energy_breath_mask

    gap_tolerance = max(1, int(round(0.05 / frame_time)))
    strict_mask = _close_mask(strict_mask, gap_tolerance)
    relaxed_gap = max(gap_tolerance, int(round(0.08 / frame_time)))
    relaxed_mask = _close_mask(relaxed_mask, relaxed_gap)
    rescue_mask = _close_mask(rescue_mask, max(relaxed_gap, int(round(0.10 / frame_time))))
    bright_breath_mask = _close_mask(bright_breath_mask, max(relaxed_gap, int(round(0.10 / frame_time))))
    airy_breath_mask = _close_mask(airy_breath_mask, max(relaxed_gap, int(round(0.16 / frame_time))))
    micro_breath_mask = _close_mask(micro_breath_mask, max(relaxed_gap, int(round(0.08 / frame_time))))
    short_breath_mask = _close_mask(short_breath_mask, max(gap_tolerance, int(round(0.05 / frame_time))))
    needle_breath_mask = _close_mask(needle_breath_mask, max(gap_tolerance, int(round(0.06 / frame_time))))
    core_breath_mask = _close_mask(core_breath_mask, max(gap_tolerance, int(round(0.07 / frame_time))))
    post_phrase_mask = _close_mask(post_phrase_mask, max(relaxed_gap, int(round(0.10 / frame_time))))
    low_energy_breath_mask = _close_mask(low_energy_breath_mask, max(gap_tolerance, int(round(0.08 / frame_time))))

    strict_segments = _merge_segments(strict_mask, frame_time, sr)
    relaxed_segments = _merge_segments(relaxed_mask, frame_time, sr, min_duration=0.08, max_duration=0.60)
    segments = list(strict_segments)
    if relaxed_segments:
        segments = sorted(segments + relaxed_segments, key=lambda item: item[0])
    rescue_segments = _merge_segments(rescue_mask, frame_time, sr, min_duration=0.18, max_duration=0.40)
    bright_segments = _merge_segments(bright_breath_mask, frame_time, sr, min_duration=0.16, max_duration=0.48)
    airy_segments = _merge_segments(airy_breath_mask, frame_time, sr, min_duration=0.15, max_duration=0.50)
    micro_segments = _merge_segments(micro_breath_mask, frame_time, sr, min_duration=0.12, max_duration=0.50)
    short_segments = _merge_segments(short_breath_mask, frame_time, sr, min_duration=0.04, max_duration=0.32)
    needle_segments = _merge_segments(needle_breath_mask, frame_time, sr, min_duration=0.16, max_duration=0.40)
    core_segments = _merge_segments(core_breath_mask, frame_time, sr, min_duration=0.14, max_duration=0.42)
    post_phrase_segments = _merge_segments(post_phrase_mask, frame_time, sr, min_duration=0.10, max_duration=0.65)
    low_energy_segments = _merge_segments(low_energy_breath_mask, frame_time, sr, min_duration=0.08, max_duration=0.52)
    if rescue_segments:
        segments = sorted(segments + rescue_segments, key=lambda item: item[0])
    if bright_segments:
        segments = sorted(segments + bright_segments, key=lambda item: item[0])
    if airy_segments:
        segments = sorted(segments + airy_segments, key=lambda item: item[0])
    if micro_segments:
        segments = sorted(segments + micro_segments, key=lambda item: item[0])
    if short_segments:
        segments = sorted(segments + short_segments, key=lambda item: item[0])
    if needle_segments:
        segments = sorted(segments + needle_segments, key=lambda item: item[0])
    if core_segments:
        segments = sorted(segments + core_segments, key=lambda item: item[0])
    if post_phrase_segments:
        segments = sorted(segments + post_phrase_segments, key=lambda item: item[0])
    if low_energy_segments:
        segments = sorted(segments + low_energy_segments, key=lambda item: item[0])

    silence_mask = _close_mask(rms_norm < 0.05, max(1, int(round(0.80 / frame_time))))
    silence_segments = _merge_segments(silence_mask, frame_time, sr, min_duration=3.0, max_duration=20.0)
    if silence_segments:
        segments = sorted(segments + silence_segments, key=lambda item: item[0])
    floor_silence_segments = _detect_low_voice_silence_segments(raw_rms, frame_time, sr, voice_floor_threshold)
    if floor_silence_segments:
        floor_silence_segments = _snap_right_edge_to_tail_valley(
            floor_silence_segments,
            sr,
            frame_time,
            raw_rms,
            peak_reject_threshold,
            voice_floor_threshold,
        )
    if floor_silence_segments:
        segments = sorted(segments + floor_silence_segments, key=lambda item: item[0])

    segments = _refine_segment_to_core(
        segments,
        sr,
        frame_time,
        smoothed_score,
        rms_norm,
        zcr_norm,
        centroid_norm,
    )

    raw_segments = len(segments)
    scored_segments = []
    for start, end in segments:
        start_frame = max(0, int(start / HOP_LENGTH))
        end_frame = min(len(smoothed_score), int(np.ceil(end / HOP_LENGTH)))

        segment_score = _sample_slice(smoothed_score, start_frame, end_frame)
        segment_rms = _sample_slice(rms_norm, start_frame, end_frame)
        segment_flatness = _sample_slice(flatness_norm, start_frame, end_frame)
        segment_zcr = _sample_slice(zcr_norm, start_frame, end_frame)
        segment_centroid = _sample_slice(centroid_norm, start_frame, end_frame)
        segment_bandwidth = _sample_slice(bandwidth_norm, start_frame, end_frame)
        segment_raw_rms = _sample_slice(raw_rms, start_frame, end_frame)
        follow_rms = _sample_slice(rms_norm, end_frame, end_frame + max(2, int(round(0.18 / frame_time))))
        lead_rms_segment = _sample_slice(rms_norm, max(0, start_frame - max(2, int(round(0.10 / frame_time)))), start_frame)
        lead_raw_segment = _sample_slice(raw_rms, max(0, start_frame - max(2, int(round(0.20 / frame_time)))), start_frame)
        pre_peak_raw_segment = _sample_slice(raw_rms, max(0, start_frame - max(4, int(round(2.20 / frame_time)))), start_frame)
        follow_raw_segment = _sample_slice(raw_rms, end_frame, end_frame + max(2, int(round(0.20 / frame_time))))

        if len(segment_score) == 0 or len(segment_rms) == 0 or len(segment_raw_rms) == 0:
            continue

        duration = (end - start) / sr
        mean_score = float(np.mean(segment_score))
        peak_score = float(np.max(segment_score))
        mean_rms = float(np.mean(segment_rms))
        peak_rms = float(np.percentile(segment_rms, 90))
        max_rms = float(np.max(segment_rms))
        mean_raw_rms = float(np.mean(segment_raw_rms))
        p90_raw_rms = float(np.percentile(segment_raw_rms, 90))
        max_raw_rms = float(np.max(segment_raw_rms))
        texture_score = float(
            0.35 * np.mean(segment_flatness)
            + 0.25 * np.mean(segment_zcr)
            + 0.20 * np.mean(segment_centroid)
            + 0.20 * np.mean(segment_bandwidth)
        )
        inner_slice = slice(max(0, len(segment_rms) // 5), max(1, len(segment_rms) - len(segment_rms) // 5))
        edge_rms = float(
            np.mean(np.concatenate((segment_rms[: max(1, len(segment_rms) // 6)], segment_rms[-max(1, len(segment_rms) // 6) :])))
        )
        middle_rms = float(np.mean(segment_rms[inner_slice])) if len(segment_rms[inner_slice]) else mean_rms
        edge_score = float(
            np.mean(np.concatenate((segment_score[: max(1, len(segment_score) // 6)], segment_score[-max(1, len(segment_score) // 6) :])))
        )
        middle_score = float(np.mean(segment_score[inner_slice])) if len(segment_score[inner_slice]) else mean_score
        middle_texture = float(
            0.35 * np.mean(segment_flatness[inner_slice])
            + 0.25 * np.mean(segment_zcr[inner_slice])
            + 0.20 * np.mean(segment_centroid[inner_slice])
            + 0.20 * np.mean(segment_bandwidth[inner_slice])
        ) if len(segment_flatness[inner_slice]) else texture_score
        mean_flatness = float(np.mean(segment_flatness))
        mean_zcr = float(np.mean(segment_zcr))
        mean_centroid = float(np.mean(segment_centroid))
        mean_bandwidth = float(np.mean(segment_bandwidth))
        core_hint_ratio = float(
            np.mean(
                (segment_rms <= 0.12)
                & (segment_score >= 0.24)
                & (segment_zcr >= 0.42)
                & (segment_centroid >= 0.42)
            )
        )
        follow_mean = float(np.mean(follow_rms)) if len(follow_rms) else mean_rms
        lead_mean = float(np.mean(lead_rms_segment)) if len(lead_rms_segment) else mean_rms
        rise_ratio = (follow_mean + 1e-6) / (mean_rms + 1e-6)
        lead_raw_mean = float(np.mean(lead_raw_segment)) if len(lead_raw_segment) else mean_raw_rms
        pre_peak_raw = float(np.max(pre_peak_raw_segment)) if len(pre_peak_raw_segment) else max_raw_rms
        follow_raw_mean = float(np.mean(follow_raw_segment)) if len(follow_raw_segment) else mean_raw_rms
        lead_raw_p90 = float(np.percentile(lead_raw_segment, 90)) if len(lead_raw_segment) else mean_raw_rms
        follow_raw_p50 = float(np.percentile(follow_raw_segment, 50)) if len(follow_raw_segment) else mean_raw_rms
        lead_decay_slope = _linear_slope(lead_raw_segment)
        seg_shape_slope = _linear_slope(segment_raw_rms)
        seg_edge_raw = float(
            np.mean(
                np.concatenate(
                    (
                        segment_raw_rms[: max(1, len(segment_raw_rms) // 6)],
                        segment_raw_rms[-max(1, len(segment_raw_rms) // 6) :],
                    )
                )
            )
        )
        seg_mid_raw = float(np.mean(segment_raw_rms[inner_slice])) if len(segment_raw_rms[inner_slice]) else mean_raw_rms
        post_phrase_release = (
            lead_raw_mean >= max(mean_raw_rms * 1.30, global_noise_p35 * 1.45)
            and lead_decay_slope <= -max(global_noise_p35 * 0.01, 0.0003)
            and mean_raw_rms <= min(global_voice_p75 * 0.68, global_voice_p87 * 0.52)
            and mean_score >= max(0.20, relaxed_threshold - 0.02)
            and mean_zcr >= 0.40
            and mean_centroid >= 0.40
            and mean_bandwidth >= 0.42
            and seg_mid_raw >= max(follow_raw_p50 * 1.08, global_noise_p35 * 1.02)
            and seg_shape_slope <= max(global_noise_p35 * 0.005, 0.0002)
        )
        pre_drop_ratio = (pre_peak_raw + 1e-6) / (max_raw_rms + 1e-6)
        abrupt_pre_drop = (
            pre_peak_raw >= max(peak_reject_threshold * 1.25, percentile_reject_threshold * 1.45, global_voice_p75 * 0.80)
            and pre_drop_ratio >= 2.2
            and lead_raw_mean >= mean_raw_rms * 1.45
        )
        hard_peak_reject = max_raw_rms > (peak_reject_threshold + 1e-6)
        hard_percentile_reject = p90_raw_rms > (percentile_reject_threshold + 1e-6)
        voice_peak_reject = (
            duration <= 0.50
            and (
                hard_peak_reject
                or hard_percentile_reject
                or (lead_raw_p90 > 0 and p90_raw_rms >= lead_raw_p90 * 0.92 and mean_raw_rms >= lead_raw_mean * 0.72)
            )
            and mean_flatness <= 0.14
            and core_hint_ratio < 0.30
        ) or (
            duration <= 0.50
            and mean_raw_rms >= global_voice_p75 * 0.95
            and p90_raw_rms >= global_voice_p87 * 0.80
            and mean_flatness <= 0.12
            and core_hint_ratio < 0.30
        ) or (
            duration <= 0.50
            and seg_edge_raw >= seg_mid_raw * 0.92
            and p90_raw_rms >= global_voice_p75 * 1.05
            and mean_flatness <= 0.12
            and core_hint_ratio < 0.28
        )

        low_voice_force_keep = (
            voice_floor_threshold > 0.0
            and duration <= 0.72
            and max_raw_rms <= voice_floor_threshold + 1e-6
        )

        keep = low_voice_force_keep or (
            (0.05 <= duration <= 0.58)
            and (mean_score >= max(0.18, strict_threshold - 0.06))
            and (peak_score >= strict_threshold + 0.02)
            and (texture_score >= max(0.16, noise_floor - 0.02))
            and (
                (mean_rms <= min(energy_ceiling * 0.28, 0.16))
                or (core_hint_ratio >= 0.16 and peak_score >= strict_threshold + 0.06 and duration <= 0.65)
            )
            and (
                (rise_ratio >= max(1.02, 1.55 - sensitivity * 0.04) and follow_mean >= max(mean_rms * 1.05, lead_mean * 0.98, 0.02))
                or (mean_zcr >= 0.50 and mean_centroid >= 0.48 and mean_rms <= 0.07)
                or (
                    duration <= 0.26
                    and mean_score >= max(0.20, strict_threshold - 0.07)
                    and mean_zcr >= 0.46
                    and mean_centroid >= 0.44
                    and follow_mean >= max(mean_rms * 1.02, 0.018)
                )
                or (
                    duration <= 0.40
                    and middle_rms <= 0.08
                    and middle_score >= max(0.27, strict_threshold - 0.01)
                    and mean_zcr >= 0.44
                    and mean_centroid >= 0.43
                )
                or abrupt_pre_drop
                or post_phrase_release
            )
            and (lead_mean <= mean_rms * 1.40)
            and not hard_peak_reject
            and not hard_percentile_reject
            and not voice_peak_reject
            and not (
                duration <= 0.42
                and mean_rms >= 0.07
                and edge_rms >= middle_rms * 1.10
                and edge_score <= middle_score * 0.92
                and middle_texture <= texture_score * 0.98
            )
            and not (
                duration <= 0.45
                and mean_rms >= 0.18
                and mean_flatness <= 0.12
                and core_hint_ratio < 0.22
            )
            and not (
                duration <= 0.46
                and peak_rms >= 0.26
                and mean_flatness <= 0.11
                and core_hint_ratio < 0.26
            )
            and not (
                duration <= 0.48
                and peak_rms >= 0.34
                and mean_rms >= 0.22
                and mean_flatness <= 0.12
                and core_hint_ratio < 0.30
            )
        )
        if keep:
            scored_segments.append(
                {
                    "start": start,
                    "end": end,
                    "duration": duration,
                    "mean_score": mean_score,
                    "peak_score": peak_score,
                    "mean_rms": mean_rms,
                    "rise_ratio": rise_ratio,
                    "texture_score": texture_score,
                }
            )

    scored_segments = _merge_nearby_segments(scored_segments, sr, max_gap=0.16, max_duration=0.72)
    filtered = []
    last_end = -1
    for item in scored_segments:
        if item["start"] < last_end - int(0.04 * sr):
            continue
        filtered.append((item["start"], min(item["end"], len(y))))
        last_end = item["end"]

    filtered = _expand_segment_edges(
        filtered,
        sr,
        frame_time,
        relaxed_threshold,
        energy_ceiling,
        smoothed_score,
        rms_norm,
        zcr_norm,
        centroid_norm,
    )
    filtered = _refine_segment_to_core(
        filtered,
        sr,
        frame_time,
        smoothed_score,
        rms_norm,
        zcr_norm,
        centroid_norm,
    )
    filtered = _trim_segment_heads_tails(
        filtered,
        sr,
        frame_time,
        smoothed_score,
        rms_norm,
        zcr_norm,
        centroid_norm,
        bandwidth_norm,
    )
    filtered = _extend_breath_edges(
        filtered,
        sr,
        frame_time,
        smoothed_score,
        rms_norm,
        zcr_norm,
        centroid_norm,
        bandwidth_norm,
    )
    filtered = _trim_loud_edges_by_threshold(
        filtered,
        sr,
        frame_time,
        raw_rms,
        peak_reject_threshold,
        percentile_reject_threshold,
    )
    filtered = _trim_rising_voice_right_edges(
        filtered,
        sr,
        frame_time,
        raw_rms,
        peak_reject_threshold,
        percentile_reject_threshold,
        voice_floor_threshold,
    )
    filtered = _trim_following_voice_onset(
        filtered,
        sr,
        frame_time,
        raw_rms,
        peak_reject_threshold,
        percentile_reject_threshold,
        voice_floor_threshold,
    )
    filtered = _snap_right_edge_to_tail_valley(
        filtered,
        sr,
        frame_time,
        raw_rms,
        peak_reject_threshold,
        voice_floor_threshold,
    )
    if floor_silence_segments:
        filtered_ranges = [(start / sr, end / sr) for start, end in filtered]
        floor_ranges = [(start / sr, end / sr) for start, end in floor_silence_segments]
        floor_ranges = _subtract_time_ranges(floor_ranges, filtered_ranges)
        filtered = _merge_time_ranges(
            filtered_ranges + floor_ranges,
            min_gap_sec=0.0,
        )
        filtered = _time_ranges_to_samples(filtered, sr, len(y))

    durations = [float((end - start) / sr) for start, end in filtered]
    diagnostics = {
        "candidate_hits": int(np.count_nonzero(candidate_mask)),
        "strict_hits": int(np.count_nonzero(strict_mask)),
        "relaxed_hits": int(np.count_nonzero(relaxed_mask)),
        "raw_segments": raw_segments,
        "kept_segments": len(filtered),
        "max_score": float(np.max(smoothed_score)),
        "mean_score": float(np.mean(smoothed_score)),
        "avg_duration": float(np.mean(durations)) if durations else 0.0,
        "avg_rise_ratio": float(np.mean([item["rise_ratio"] for item in scored_segments])) if scored_segments else 0.0,
        "smooth_frames": int(smooth_frames),
        "strict_threshold": float(strict_threshold),
    }
    return filtered, diagnostics


def _format_diagnostics_text(diagnostics, segment_count):
    if not diagnostics:
        return "诊断：未处理"
    return (
        f"诊断：候选帧 {diagnostics['candidate_hits']}，"
        f"严格命中 {diagnostics['strict_hits']}，"
        f"宽松命中 {diagnostics['relaxed_hits']}，"
        f"原始片段 {diagnostics['raw_segments']}，"
        f"最终片段 {segment_count}，"
        f"最高分 {diagnostics['max_score']:.2f}，"
        f"平均分 {diagnostics['mean_score']:.2f}，"
        f"平均时长 {diagnostics['avg_duration']:.2f}s，"
        f"平均后升比 {diagnostics['avg_rise_ratio']:.2f}，"
        f"平滑窗口 {diagnostics['smooth_frames']} 帧，"
        f"严格阈值 {diagnostics['strict_threshold']:.2f}"
    )


def _build_output_path(input_path):
    source = Path(input_path)
    return source.with_name(f"{source.stem}_v{VERSION}.mp3")


def _expand_segments(segments, sr, total_length, left_append_ms=LEFT_APPEND_MS, right_append_ms=RIGHT_APPEND_MS):
    if not segments or sr is None or sr <= 0:
        return segments
    left_samples = int(round((left_append_ms / 1000.0) * sr))
    right_samples = int(round((right_append_ms / 1000.0) * sr))
    expanded = []
    for start, end in segments:
        new_start = start - left_samples
        new_end = end + right_samples
        new_start = max(0, min(new_start, total_length))
        new_end = max(0, min(new_end, total_length))
        if new_end > new_start:
            expanded.append((new_start, new_end))
    merged = _merge_time_ranges([(start / sr, end / sr) for start, end in expanded], min_gap_sec=0.002)
    return _time_ranges_to_samples(merged, sr, total_length)


def _write_output_mp3(y_processed, sr, output_path):
    ffmpeg_bin = _find_ffmpeg_binary()
    temp_wav = tempfile.NamedTemporaryFile(prefix="breath_processed_", suffix=".wav", delete=False)
    temp_wav.close()
    sf.write(temp_wav.name, y_processed, sr)
    try:
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                temp_wav.name,
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        if os.path.exists(temp_wav.name):
            os.remove(temp_wav.name)


def _find_ffmpeg_binary():
    candidates = [
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/local/bin/ffmpeg",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        "处理失败：未找到 ffmpeg。请确认已安装 ffmpeg，或把它放在 /opt/homebrew/bin/ffmpeg"
    )


def _merge_time_ranges(ranges, min_gap_sec=0.0):
    if not ranges:
        return []
    merged = []
    min_gap_sec = float(max(0.0, min_gap_sec))
    for start, end in sorted((float(s), float(e)) for s, e in ranges if e > s):
        if not merged:
            merged.append([start, end])
            continue
        prev = merged[-1]
        if start <= prev[1] + min_gap_sec:
            prev[1] = max(prev[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _subtract_time_ranges(base_ranges, remove_ranges):
    if not base_ranges:
        return []
    if not remove_ranges:
        return [(float(start), float(end)) for start, end in base_ranges if end > start]

    remaining = [(float(start), float(end)) for start, end in base_ranges if end > start]
    for remove_start, remove_end in _merge_time_ranges(remove_ranges):
        next_remaining = []
        for start, end in remaining:
            if remove_end <= start or remove_start >= end:
                next_remaining.append((start, end))
                continue
            if remove_start > start:
                next_remaining.append((start, min(remove_start, end)))
            if remove_end < end:
                next_remaining.append((max(remove_end, start), end))
        remaining = next_remaining
    return _merge_time_ranges(remaining)


def _time_ranges_to_samples(ranges, sr, total_length):
    segments = []
    for start_sec, end_sec in ranges:
        start = max(0, int(round(start_sec * sr)))
        end = min(total_length, int(round(end_sec * sr)))
        if end > start:
            segments.append((start, end))
    return segments


def _intersect_time_ranges(base_ranges, target_range):
    target_start, target_end = float(target_range[0]), float(target_range[1])
    if target_end <= target_start:
        return []
    overlaps = []
    for start, end in base_ranges:
        overlap_start = max(float(start), target_start)
        overlap_end = min(float(end), target_end)
        if overlap_end > overlap_start:
            overlaps.append((overlap_start, overlap_end))
    return _merge_time_ranges(overlaps)


def _build_half_time_segment(segment):
    segment = np.asarray(segment, dtype=np.float32)
    if len(segment) <= 2:
        return np.zeros_like(segment, dtype=np.float32)
    keep_len = max(1, len(segment) // 2)
    source_idx = np.linspace(0, len(segment) - 1, keep_len, dtype=np.float32)
    compressed = np.interp(source_idx, np.arange(len(segment), dtype=np.float32), segment).astype(np.float32)
    output = np.zeros_like(segment, dtype=np.float32)
    output[:keep_len] = compressed
    fade_len = min(max(16, keep_len // 8), keep_len)
    if fade_len > 1:
        output[:fade_len] *= np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
        output[keep_len - fade_len : keep_len] *= np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
    return output


def _apply_breath_segments(y, sr, breath_segments, atten_db=18, half_time_segments=None):
    y_processed = y.copy()
    gain = np.power(10, -atten_db / 20)
    half_time_segments = half_time_segments or []
    half_time_lookup = {(int(start), int(end)) for start, end in half_time_segments}
    for start, end in breath_segments:
        start = max(0, start)
        end = min(end, len(y_processed))
        if end <= start:
            continue
        segment = np.asarray(y_processed[start:end], dtype=np.float32)
        if (int(start), int(end)) in half_time_lookup:
            y_processed[start:end] = _build_half_time_segment(segment)
            continue
        env_window = max(16, int(0.012 * sr))
        local_env = _moving_average(np.abs(segment), env_window)
        env_low = float(np.percentile(local_env, 15))
        env_high = float(np.percentile(local_env, 88))
        env_norm = np.clip((local_env - env_low) / (env_high - env_low + 1e-6), 0.0, 1.0)
        env_norm = _moving_average(env_norm, max(8, int(0.010 * sr)))

        envelope = np.zeros_like(segment, dtype=np.float32)
        left_feather_len = min(int(0.050 * sr), max(len(segment) - 1, 1))
        right_feather_len = min(int(0.050 * sr), max(len(segment) - 1, 1))
        overlap_guard = max(1, len(segment) // 2)
        left_feather_len = min(left_feather_len, overlap_guard)
        right_feather_len = min(right_feather_len, overlap_guard)

        if left_feather_len > 1:
            left_curve = 0.5 + 0.5 * np.cos(np.linspace(0.0, np.pi, left_feather_len, dtype=np.float32))
            envelope[:left_feather_len] = np.maximum(envelope[:left_feather_len], left_curve)
        elif len(segment):
            envelope[0] = 1.0

        if right_feather_len > 1:
            right_curve = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, right_feather_len, dtype=np.float32))
            envelope[-right_feather_len:] = np.maximum(envelope[-right_feather_len:], right_curve)
        elif len(segment):
            envelope[-1] = 1.0

        envelope = np.clip(envelope * max(gain, 0.0), 0.0, 1.0)
        processed_segment = segment * envelope.astype(np.float32)
        y_processed[start:end] = processed_segment

    return y_processed


def process_breath(
    input_path,
    atten_db=18,
    sensitivity=7,
    peak_reject_threshold=0.20,
    percentile_reject_threshold=0.20,
    voice_floor_threshold=0.0,
    left_append_ms=LEFT_APPEND_MS,
    right_append_ms=RIGHT_APPEND_MS,
):
    try:
        y, sr = librosa.load(input_path, sr=None, mono=True)
        breath_segments, diagnostics = _detect_breath_segments(
            y,
            sr,
            sensitivity,
            peak_reject_threshold,
            percentile_reject_threshold,
            voice_floor_threshold,
        )
        breath_segments = _expand_segments(
            breath_segments,
            sr,
            len(y),
            left_append_ms=left_append_ms,
            right_append_ms=right_append_ms,
        )
        y_processed = _apply_breath_segments(y, sr, breath_segments, atten_db=atten_db)

        output_path = _build_output_path(input_path)
        if output_path.exists():
            output_path.unlink()
        _write_output_mp3(y_processed, sr, output_path)

        return {
            "source_audio": y,
            "output_audio": y_processed,
            "sr": sr,
            "segments": breath_segments,
            "auto_segments": list(breath_segments),
            "diagnostics": diagnostics,
            "output_path": str(output_path),
        }
    except Exception as exc:
        raise RuntimeError(f"处理失败：{exc}") from exc


class BreathReducerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"清唱吸气声弱化工具 v{VERSION}")
        self.root.geometry(f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
        self.root.resizable(True, True)
        self.app_config = _load_app_config()

        self.input_path = ""
        self.output_path = ""
        self.source_audio = None
        self.output_audio = None
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
        self.current_view_start = 0.0
        self.current_view_duration = 8.0
        self._syncing_scrollbars = False
        self.source_playhead_line = None
        self.output_playhead_line = None

        self._build_controls()
        self._build_plot()
        self.root.after(150, self.bring_to_front)

    def _build_controls(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill=tk.X)

        ttk.Label(top, text="选择音频文件：").grid(row=0, column=0, sticky="w")
        self.file_label = ttk.Label(top, text="未选择文件", foreground="gray")
        self.file_label.grid(row=0, column=1, sticky="w", padx=(8, 12))
        ttk.Button(top, text="浏览", command=self.select_file).grid(row=0, column=2, padx=6)

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
        ttk.Label(top, text="向左附加(毫秒)：").grid(row=3, column=2, sticky="w", pady=(10, 0))
        self.left_append_entry = ttk.Entry(top, textvariable=self.left_append_ms_var, width=10)
        self.left_append_entry.grid(row=3, column=3, sticky="w", pady=(10, 0))
        ttk.Label(top, text="向右附加(毫秒)：").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.right_append_entry = ttk.Entry(top, textvariable=self.right_append_ms_var, width=10)
        self.right_append_entry.grid(row=4, column=1, sticky="w", pady=(10, 0))
        ttk.Label(top, text="人声下限按 0-100 输入，支持小数；低于此下限的每一帧都会直接按吸气处理", foreground="gray").grid(row=4, column=2, columnspan=2, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(top)
        buttons.grid(row=5, column=0, columnspan=4, sticky="w", pady=(12, 0))
        self.process_btn = ttk.Button(buttons, text="重新处理当前文件", command=self.run_process, state=tk.DISABLED)
        self.process_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.play_active_source_btn = ttk.Button(buttons, text="播放原文件", command=lambda: self.toggle_active_playback(False), state=tk.DISABLED)
        self.play_active_source_btn.pack(side=tk.LEFT, padx=8)
        self.play_active_output_btn = ttk.Button(buttons, text="播放输出文件", command=lambda: self.toggle_active_playback(True), state=tk.DISABLED)
        self.play_active_output_btn.pack(side=tk.LEFT, padx=8)
        self.half_time_btn = ttk.Button(buttons, text="区间时间减半", command=self.toggle_half_time_mode, state=tk.DISABLED)
        self.half_time_btn.pack(side=tk.LEFT, padx=8)
        self.selection_mode_btn = ttk.Button(buttons, text="开启区间选择", command=self.toggle_selection_mode, state=tk.DISABLED)
        self.selection_mode_btn.pack(side=tk.LEFT, padx=8)
        self.pick_segment_btn = ttk.Button(buttons, text="选中处理片段", command=self.toggle_pick_detected_segment_mode, state=tk.DISABLED)
        self.pick_segment_btn.pack(side=tk.LEFT, padx=8)
        self.export_segments_btn = ttk.Button(buttons, text="导出区间", command=self.export_effective_segments, state=tk.DISABLED)
        self.export_segments_btn.pack(side=tk.LEFT, padx=8)
        self.clear_selection_btn = ttk.Button(buttons, text="清空选区", command=self.clear_selected_ranges, state=tk.DISABLED)
        self.clear_selection_btn.pack(side=tk.LEFT, padx=8)
        self.select_range_btn = ttk.Button(buttons, text="手动选择区间", command=lambda: self.toggle_range_edit_mode("add"), state=tk.DISABLED)
        self.select_range_btn.pack(side=tk.LEFT, padx=8)
        self.cancel_range_btn = ttk.Button(buttons, text="取消选择", command=lambda: self.toggle_range_edit_mode("remove"), state=tk.DISABLED)
        self.cancel_range_btn.pack(side=tk.LEFT, padx=8)
        self.zoom_in_btn = ttk.Button(buttons, text="放大比例", command=lambda: self.adjust_zoom(0.5), state=tk.DISABLED)
        self.zoom_in_btn.pack(side=tk.LEFT, padx=8)
        self.zoom_out_btn = ttk.Button(buttons, text="缩小比例", command=lambda: self.adjust_zoom(2.0), state=tk.DISABLED)
        self.zoom_out_btn.pack(side=tk.LEFT, padx=8)
        self.reset_zoom_btn = ttk.Button(buttons, text="重置比例", command=self.reset_zoom, state=tk.DISABLED)
        self.reset_zoom_btn.pack(side=tk.LEFT, padx=8)

        self.status_label = ttk.Label(top, text="状态：等待操作", foreground="blue")
        self.status_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(12, 0))

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
        self.diagnostic_button.grid(row=7, column=0, columnspan=4, sticky="we", pady=(8, 0))

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

    def select_file(self):
        path = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频文件", "*.wav *.mp3 *.m4a *.flac"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.input_path = path
        self.file_label.config(text=os.path.basename(path), foreground="green")
        self.process_btn.config(state=tk.NORMAL)
        self.status_label.config(text="状态：已选择文件，正在自动处理...", foreground="orange")
        self.root.update()
        self.run_process()

    def run_process(self):
        if not self.input_path:
            messagebox.showwarning("提示", "请先选择音频文件")
            return

        self._stop_player()
        self.source_playhead_line = None
        self.output_playhead_line = None
        self.status_label.config(text="状态：正在识别并生成输出 MP3...", foreground="orange")
        self.root.update()

        try:
            peak_reject_threshold = np.clip(float(self.peak_reject_var.get()), 0.0, 100.0) / 100.0
            percentile_reject_threshold = np.clip(float(self.percentile_reject_var.get()), 0.0, 100.0) / 100.0
            voice_floor_threshold = np.clip(float(self.voice_floor_var.get()), 0.0, 100.0) / 100.0
            left_append_ms = float(self.left_append_ms_var.get())
            right_append_ms = float(self.right_append_ms_var.get())
        except ValueError:
            messagebox.showwarning("提示", "吸气最大峰值、吸气最大整体音量、人声下限、向左附加和向右附加都需要填写数字")
            return

        self.peak_reject_var.set(f"{peak_reject_threshold * 100:.2f}".rstrip("0").rstrip("."))
        self.percentile_reject_var.set(f"{percentile_reject_threshold * 100:.2f}".rstrip("0").rstrip("."))
        self.voice_floor_var.set(f"{voice_floor_threshold * 100:.2f}".rstrip("0").rstrip("."))
        self.left_append_ms_var.set(f"{left_append_ms:.2f}".rstrip("0").rstrip("."))
        self.right_append_ms_var.set(f"{right_append_ms:.2f}".rstrip("0").rstrip("."))

        try:
            result = process_breath(
                self.input_path,
                self.atten_slider.get(),
                self.sensitivity_slider.get(),
                peak_reject_threshold,
                percentile_reject_threshold,
                voice_floor_threshold,
                left_append_ms,
                right_append_ms,
            )
        except RuntimeError as exc:
            self.status_label.config(text="状态：处理失败", foreground="red")
            messagebox.showerror("错误", str(exc))
            return

        self.source_audio = result["source_audio"]
        self.output_audio = result["output_audio"]
        self.sr = result["sr"]
        self.segments = result["segments"]
        self.output_path = result["output_path"]
        self.last_diagnostics = result["diagnostics"]
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
        self.drag_start_sec = None
        self.drag_plot_kind = None
        self.range_edit_mode = None
        total_duration = len(self.source_audio) / self.sr if self.source_audio is not None else 0.0
        self.current_view_start = 0.0
        self.current_view_duration = min(8.0, total_duration) if total_duration else 8.0

        self.debug_text.set(_format_diagnostics_text(self.last_diagnostics, len(self.segments)))
        self._save_current_config()
        self.status_label.config(
            text=f"状态：处理完成，输出文件已生成：{os.path.basename(self.output_path)}",
            foreground="green",
        )
        segment_state = tk.NORMAL if self.segments else tk.DISABLED
        click_play_state = tk.NORMAL if self.source_audio is not None else tk.DISABLED
        self.play_active_source_btn.config(state=click_play_state)
        self.play_active_output_btn.config(state=click_play_state)
        self.half_time_btn.config(state=segment_state)
        self.selection_mode_btn.config(state=click_play_state)
        self.pick_segment_btn.config(state=click_play_state)
        self.export_segments_btn.config(state=segment_state)
        self.clear_selection_btn.config(state=click_play_state)
        self.select_range_btn.config(state=click_play_state)
        self.cancel_range_btn.config(state=click_play_state)
        zoom_state = tk.NORMAL if self.source_audio is not None else tk.DISABLED
        self.zoom_in_btn.config(state=zoom_state)
        self.zoom_out_btn.config(state=zoom_state)
        self.reset_zoom_btn.config(state=zoom_state)
        self.source_scroll.config(state=zoom_state)
        self.output_scroll.config(state=zoom_state)
        self._update_selection_buttons()
        self._update_play_toggle_buttons()
        self.refresh_plots()

    def _compute_envelope(self, audio):
        if audio is None or self.sr is None:
            return np.asarray([], dtype=np.float32), np.asarray([], dtype=np.float32)
        envelope = librosa.feature.rms(y=audio, frame_length=2048, hop_length=HOP_LENGTH)[0]
        times = librosa.times_like(envelope, sr=self.sr, hop_length=HOP_LENGTH)
        return times, envelope

    def _draw_wave_envelope(self, ax, audio, title, active=False):
        ax.clear()
        if audio is None or self.sr is None:
            ax.set_title(title)
            return
        duration = len(audio) / self.sr
        times, envelope = self._compute_envelope(audio)
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
                label=f"最大峰值 {peak_line * 100:.0f}",
            )
            ax.axhline(
                percentile_line,
                color="#ffd400",
                linestyle="--",
                linewidth=1.1,
                alpha=0.85,
                label=f"最大整体音量 {percentile_line * 100:.0f}",
            )
            if voice_floor_line > 0:
                ax.axhline(
                    voice_floor_line,
                    color="#ffffff",
                    linestyle="--",
                    linewidth=1.1,
                    alpha=0.95,
                    label=f"人声下限 {voice_floor_line * 100:.0f}",
                )
            ax.legend(loc="upper right", fontsize=8, framealpha=0.85)

        merged_visible_segments = [(start / self.sr, end / self.sr) for start, end in self.segments]

        for index, (start_sec, end_sec) in enumerate(merged_visible_segments):
            is_half_time = self._segment_is_half_time(start_sec, end_sec)
            base_fill = "#a855f7" if is_half_time else "#00ff66"
            edge_color = "#c084fc" if is_half_time else ("#ff7f50" if index in self.picked_detected_segments else "#00cc55")
            fill_alpha = 0.34 if is_half_time else (0.30 if index == self.selected_segment_index else (0.22 if index in self.picked_detected_segments else 0.18))
            selected_edge = "#f7ff00" if index == self.selected_segment_index else edge_color
            ax.axvspan(start_sec, end_sec, color=base_fill, alpha=fill_alpha, ec=selected_edge, lw=2)

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
        self._draw_wave_envelope(self.ax_source, self.source_audio, "源文件音量谱", active=self.active_plot == "source")
        self._draw_wave_envelope(self.ax_output, self.output_audio, "输出文件音量谱", active=self.active_plot == "output")
        self.canvas_source.draw_idle()
        self.canvas_output.draw_idle()
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
            margin = min(max(0.25, view_duration * 0.22), view_duration / 2)
            new_view_start = self.current_view_start
            if self.selected_time_sec < left + margin:
                new_view_start = self.selected_time_sec - margin
            elif self.selected_time_sec > right - margin:
                new_view_start = self.selected_time_sec - (view_duration - margin)
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
        self.canvas_source.draw_idle()
        self.canvas_output.draw_idle()

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

    def on_plot_press(self, event, plot_kind):
        if event.xdata is None or self.sr is None:
            return

        resize_hit = self._find_resize_handle(float(event.xdata))
        if resize_hit is not None and not self.selection_mode and self.range_edit_mode is None and not self.pick_detected_segment_mode:
            self.active_plot = plot_kind
            self.resize_segment_index, self.resize_edge = resize_hit
            self.selected_segment_index = self.resize_segment_index
            self.selected_time_sec = float(event.xdata)
            self.resize_preview_time = float(event.xdata)
            self.status_label.config(
                text=f"状态：拖动调整绿色片段{'左侧' if self.resize_edge == 'start' else '右侧'}边界",
                foreground="purple",
            )
            self.refresh_plots()
            return

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
        if self.resize_segment_index is not None:
            self.active_plot = plot_kind
            self.resize_preview_time = float(event.xdata)
            self.selected_time_sec = float(event.xdata)
            self._update_playhead_display(force_refresh=True)

    def on_plot_release(self, event, plot_kind):
        if self.resize_segment_index is not None and self.sr is not None:
            new_time_sec = self.resize_preview_time
            if event.xdata is not None:
                new_time_sec = float(event.xdata)
            if new_time_sec is not None:
                self._apply_segment_resize(self.resize_segment_index, self.resize_edge, new_time_sec)
            self.resize_segment_index = None
            self.resize_edge = None
            self.resize_preview_time = None
            return
        if ((self.range_edit_mode != "add" and not self.selection_mode) or self.drag_start_sec is None or self.sr is None):
            return
        if event.xdata is None:
            self.drag_start_sec = None
            self.drag_plot_kind = None
            self.refresh_plots()
            return

        start_sec = min(self.drag_start_sec, float(event.xdata))
        end_sec = max(self.drag_start_sec, float(event.xdata))
        drag_duration = abs(end_sec - start_sec)
        if self.range_edit_mode == "add" and drag_duration < MIN_MANUAL_DRAG_SEC:
            self.drag_start_sec = None
            self.drag_plot_kind = None
            self.range_edit_mode = None
            self.status_label.config(text="状态：已取消本次手动选择", foreground="blue")
            self._update_selection_buttons()
            self.refresh_plots()
            return
        if drag_duration < 0.005:
            end_sec = min(start_sec + 0.005, len(self.source_audio) / self.sr if self.source_audio is not None else start_sec + 0.005)

        self.selected_time_sec = start_sec
        self.drag_start_sec = None
        self.drag_plot_kind = None
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
        for index, (start, end) in enumerate(self.segments):
            start_sec = start / self.sr
            end_sec = end / self.sr
            if start_sec <= clicked_time <= end_sec:
                best_index = index
                break
            distance = min(abs(clicked_time - start_sec), abs(clicked_time - end_sec))
            if distance < best_distance and distance <= 0.20:
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
            if best_index is None:
                self.status_label.config(text="状态：未点中绿色处理片段，请再试一次", foreground="purple")
                return
            start, end = self.segments[best_index]
            start_sec = start / self.sr
            end_sec = end / self.sr
            target = (start_sec, end_sec)
            if self._segment_is_half_time(start_sec, end_sec):
                self.half_time_ranges = [
                    item for item in self.half_time_ranges
                    if not (abs(item[0] - start_sec) <= 0.002 and abs(item[1] - end_sec) <= 0.002)
                ]
                action_text = "已取消"
            else:
                self.half_time_ranges = _merge_time_ranges(self.half_time_ranges + [target], min_gap_sec=0.0)
                action_text = "已设为"
            self.selected_segment_index = best_index
            self._normalize_half_time_ranges()
            self._rewrite_output_from_current_segments()
            self.half_time_mode = False
            self._update_selection_buttons()
            self.status_label.config(
                text=f"状态：{action_text}紫色时间减半区间 {int(round(start_sec * 1000))}-{int(round(end_sec * 1000))}",
                foreground="purple",
            )
            self.refresh_plots()
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
            self.half_time_btn.config(text="等待点区间")
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

    def _save_current_config(self):
        self.app_config = {
            "atten_db": int(self.atten_slider.get()),
            "sensitivity": int(self.sensitivity_slider.get()),
            "peak_reject": float(self.peak_reject_var.get() or 0),
            "percentile_reject": float(self.percentile_reject_var.get() or 0),
            "voice_floor": float(self.voice_floor_var.get() or 0),
            "left_append_ms": float(self.left_append_ms_var.get() or 0),
            "right_append_ms": float(self.right_append_ms_var.get() or 0),
        }
        _save_app_config(self.app_config)

    def _segment_is_half_time(self, start_sec, end_sec):
        for half_start, half_end in self.half_time_ranges:
            if abs(half_start - start_sec) <= 0.002 and abs(half_end - end_sec) <= 0.002:
                return True
        return False

    def _normalize_half_time_ranges(self):
        effective_ranges = [(start / self.sr, end / self.sr) for start, end in self.segments] if self.sr is not None else []
        normalized = []
        for start_sec, end_sec in effective_ranges:
            if self._segment_is_half_time(start_sec, end_sec):
                normalized.append((start_sec, end_sec))
        self.half_time_ranges = normalized

    def _rewrite_output_from_current_segments(self):
        if self.source_audio is None or self.sr is None:
            return
        self._stop_player()
        half_time_segments = _time_ranges_to_samples(self.half_time_ranges, self.sr, len(self.source_audio))
        self.output_audio = _apply_breath_segments(
            self.source_audio,
            self.sr,
            self.segments,
            atten_db=self.atten_slider.get(),
            half_time_segments=half_time_segments,
        )
        if self.input_path:
            self.output_path = str(_build_output_path(self.input_path))
            output_file = Path(self.output_path)
            if output_file.exists():
                output_file.unlink()
            _write_output_mp3(self.output_audio, self.sr, output_file)

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
        self.root.update()
        self.status_label.config(text=f"状态：已复制全部处理区间：{text}", foreground="blue")

    def toggle_half_time_mode(self):
        if self.sr is None or not self.segments:
            return
        self.half_time_mode = not self.half_time_mode
        if self.half_time_mode:
            self.selection_mode = False
            self.pick_detected_segment_mode = False
            self.range_edit_mode = None
            self.status_label.config(text="状态：等待点一个绿色区间，点中后该区间会变紫并在输出里时间减半", foreground="purple")
        else:
            self.status_label.config(text="状态：已退出区间时间减半模式", foreground="blue")
        self._update_selection_buttons()
        self.refresh_plots()

    def toggle_selection_mode(self):
        if not self.selection_mode:
            self.selection_mode = True
            self.pick_detected_segment_mode = False
            self.half_time_mode = False
            self.range_edit_mode = None
            self.picked_detected_segments = []
            self.drag_start_sec = None
            self.drag_plot_kind = None
            self.status_label.config(
                text="状态：区间选择模式已开启，拖拽鼠标可选多段；完成后点“输出选中时间”",
                foreground="purple",
            )
        else:
            self.selection_mode = False
            self.drag_start_sec = None
            self.drag_plot_kind = None
            text = self._format_selected_time_ranges()
            if text:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.root.update()
                self.status_label.config(text=f"状态：已复制并清空选区：{text}", foreground="blue")
            else:
                self.status_label.config(text="状态：当前没有选区可输出", foreground="blue")
            self.selected_ranges = []
        self._update_selection_buttons()
        self.refresh_plots()

    def toggle_pick_detected_segment_mode(self):
        if not self.pick_detected_segment_mode:
            self.pick_detected_segment_mode = True
            self.selection_mode = False
            self.half_time_mode = False
            self.range_edit_mode = None
            self.picked_detected_segments = []
            self.drag_start_sec = None
            self.drag_plot_kind = None
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
                    self.root.update()
                    self.status_label.config(text=f"状态：已复制所选处理片段到剪贴板：{text}", foreground="purple")
                else:
                    self.status_label.config(text="状态：未选中任何处理片段", foreground="blue")
            else:
                self.status_label.config(text="状态：未选中任何处理片段", foreground="blue")
        self._update_selection_buttons()
        self.refresh_plots()

    def clear_selected_ranges(self):
        self.selected_ranges = []
        self.drag_start_sec = None
        self.drag_plot_kind = None
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
        if self.range_edit_mode == mode:
            self.range_edit_mode = None
            self.drag_start_sec = None
            self.drag_plot_kind = None
            self.status_label.config(text="状态：已退出区间编辑模式", foreground="blue")
        else:
            self.range_edit_mode = mode
            self.selection_mode = False
            self.pick_detected_segment_mode = False
            self.half_time_mode = False
            self.drag_start_sec = None
            self.drag_plot_kind = None
            if mode == "add":
                self.status_label.config(text="状态：等待手动选择，拖拽鼠标后会立即补充处理区间", foreground="purple")
            else:
                self.status_label.config(text="状态：等待取消选择，点击一个已存在的绿色区间即可取消", foreground="red")
        self._update_selection_buttons()
        self.refresh_plots()

    def _rebuild_effective_segments(self, rewrite_output=True):
        if self.source_audio is None or self.sr is None:
            return
        effective_ranges = _merge_time_ranges(self.auto_segments + self.manual_segments, min_gap_sec=0.002)
        self.segments = _time_ranges_to_samples(effective_ranges, self.sr, len(self.source_audio))
        self._normalize_half_time_ranges()

        if self.selected_segment_index is not None and self.selected_segment_index >= len(self.segments):
            self.selected_segment_index = len(self.segments) - 1 if self.segments else None
        if self.selected_segment_index is None and self.segments:
            self.selected_segment_index = 0

        if rewrite_output:
            self._rewrite_output_from_current_segments()

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

    def _replace_effective_segments(self, ranges_sec):
        merged = _merge_time_ranges(ranges_sec, min_gap_sec=0.002)
        self.auto_segments = list(merged)
        self.manual_segments = []
        self._rebuild_effective_segments(rewrite_output=True)

    def _apply_segment_resize(self, segment_index, edge, new_time_sec):
        if self.sr is None or not (0 <= segment_index < len(self.segments)):
            return
        total_duration = len(self.source_audio) / self.sr if self.source_audio is not None else 0.0
        effective_ranges = [(start / self.sr, end / self.sr) for start, end in self.segments]
        start_sec, end_sec = effective_ranges[segment_index]
        old_start_sec, old_end_sec = start_sec, end_sec
        was_half_time = self._segment_is_half_time(old_start_sec, old_end_sec)
        new_time_sec = min(max(float(new_time_sec), 0.0), total_duration if total_duration > 0 else float(new_time_sec))
        min_width = 0.01
        if edge == "start":
            start_sec = min(new_time_sec, end_sec - min_width)
        else:
            end_sec = max(new_time_sec, start_sec + min_width)
        effective_ranges[segment_index] = (start_sec, end_sec)
        self._replace_effective_segments(effective_ranges)
        if was_half_time:
            self.half_time_ranges = [
                item for item in self.half_time_ranges
                if not (abs(item[0] - old_start_sec) <= 0.002 and abs(item[1] - old_end_sec) <= 0.002)
            ]
            self.half_time_ranges = _merge_time_ranges(self.half_time_ranges + [(start_sec, end_sec)], min_gap_sec=0.0)
            self._normalize_half_time_ranges()
            self._rewrite_output_from_current_segments()
        self.selected_segment_index = min(segment_index, len(self.segments) - 1) if self.segments else None
        self.selected_time_sec = start_sec if edge == "start" else end_sec
        self.status_label.config(
            text=f"状态：已调整绿色片段范围到 {start_sec:.2f}s - {end_sec:.2f}s",
            foreground="purple",
        )
        self.refresh_plots()

    def _apply_range_edit(self, start_sec, end_sec):
        edit_range = (float(start_sec), float(end_sec))
        if self.range_edit_mode == "add":
            self.manual_segments = _merge_time_ranges(self.manual_segments + [edit_range], min_gap_sec=0.002)
            self._rebuild_effective_segments(rewrite_output=True)
            self.range_edit_mode = None
            self.status_label.config(
                text=f"状态：已手动补充处理区间 {start_sec:.2f}s - {end_sec:.2f}s，并已立即生效",
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
            self.half_time_ranges = [
                item for item in self.half_time_ranges
                if not (abs(item[0] - removed_start) <= 0.002 and abs(item[1] - removed_end) <= 0.002)
            ]
            self._replace_effective_segments(effective_ranges)
            self.range_edit_mode = None
            self.status_label.config(
                text=f"状态：已取消处理区间 {removed_start:.2f}s - {removed_end:.2f}s，后续仍可重新手动选择这里",
                foreground="red",
            )
        self._update_selection_buttons()
        self.refresh_plots()

    def copy_diagnostics(self):
        text = self.debug_text.get()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
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

    def _play_file(self, path):
        self._stop_player()
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
                end_time = self.playback_start_audio_time + self.playback_duration
            if end_time is not None:
                self.selected_time_sec = end_time
                self._update_playhead_display(follow_playback=True)
            self._finish_active_playback()
            return

        now_ms = int(self.root.winfo_toplevel().tk.call("clock", "milliseconds"))
        elapsed = max(0.0, (now_ms - self.playback_start_wall_time) / 1000.0)
        if self.playback_duration is not None and elapsed > self.playback_duration:
            self.selected_time_sec = self.playback_start_audio_time + self.playback_duration
            self._update_playhead_display(follow_playback=True)
            self._finish_active_playback()
            return

        self.selected_time_sec = self.playback_start_audio_time + elapsed
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

        audio = self.output_audio if processed else self.source_audio
        if audio is None:
            return

        start_sample = int(max(0, self.selected_time_sec) * self.sr)
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
        self._start_playback_tracking(
            self.selected_time_sec,
            len(clip) / self.sr,
            "output" if processed else "source",
        )

    def _resume_active_playback(self):
        resume_from = self.selected_time_sec if self.selected_time_sec is not None else self.playback_start_audio_time
        audio = self.output_audio if self.playback_plot_kind == "output" else self.source_audio
        if audio is None or self.sr is None:
            return
        start_sample = int(max(0, resume_from) * self.sr)
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
        target = "output" if processed else "source"
        if self.playback_plot_kind == target and self.playback_start_audio_time is not None:
            if self.is_paused:
                self._resume_active_playback()
                self.status_label.config(text=f"状态：继续播放{('输出文件' if processed else '原文件')}", foreground="blue")
            else:
                self._pause_active_playback()
                self.status_label.config(text=f"状态：已暂停{('输出文件' if processed else '原文件')}", foreground="blue")
            return
        self.play_active_selection(processed)
        self.status_label.config(text=f"状态：开始从当前选中位置播放{('输出文件' if processed else '原文件')}", foreground="blue")

    def play_selected_segment(self, processed):
        if self.selected_segment_index is None or self.sr is None:
            messagebox.showwarning("提示", "请先点击绿色片段")
            return

        audio = self.output_audio if processed else self.source_audio
        if audio is None:
            return

        start, end = self.segments[self.selected_segment_index]
        clip = audio[start:end]
        if len(clip) == 0:
            return

        temp_audio = tempfile.NamedTemporaryFile(prefix="breath_clip_", suffix=".wav", delete=False)
        temp_audio.close()
        sf.write(temp_audio.name, clip, self.sr)
        self._play_file(temp_audio.name)


if __name__ == "__main__":
    root = tk.Tk()
    app = BreathReducerApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app._save_current_config(), app._stop_player(), root.destroy()))
    root.mainloop()
