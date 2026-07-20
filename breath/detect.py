"""Breath / inhale detection pipeline."""
import numpy as np
import librosa

from .constants import HOP_LENGTH, LEFT_APPEND_MS, RIGHT_APPEND_MS

def percentile_norm(values, low=5, high=95):
    values = np.asarray(values, dtype=np.float32)
    lo = float(np.percentile(values, low))
    hi = float(np.percentile(values, high))
    return np.clip((values - lo) / (hi - lo + 1e-6), 0.0, 1.0)


def moving_average(values, window_size):
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


def close_mask(mask, gap_tolerance):
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


def merge_segments(mask, frame_time, sr, min_duration=0.09, max_duration=0.60):
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


def sample_slice(values, start_frame, end_frame):
    start_frame = max(0, start_frame)
    end_frame = min(len(values), end_frame)
    if end_frame <= start_frame:
        return np.asarray([], dtype=np.float32)
    return np.asarray(values[start_frame:end_frame], dtype=np.float32)


def linear_slope(values):
    values = np.asarray(values, dtype=np.float32)
    if len(values) < 2:
        return 0.0
    x = np.arange(len(values), dtype=np.float32)
    x = x - np.mean(x)
    y = values - np.mean(values)
    denom = float(np.sum(x * x)) + 1e-6
    return float(np.sum(x * y) / denom)


def merge_nearby_segments(segments, sr, max_gap=0.10, max_duration=0.45):
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


def expand_segment_edges(segments, sr, frame_time, relaxed_threshold, energy_ceiling, smoothed_score, rms_norm, zcr_norm, centroid_norm):
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


def refine_segment_to_core(segments, sr, frame_time, smoothed_score, rms_norm, zcr_norm, centroid_norm):
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
            core_segments = merge_segments(core_mask, frame_time, sr, min_duration=0.06, max_duration=0.52)
            if core_segments:
                base = start_frame * frame_time * sr
                for core_start, core_end in core_segments:
                    refined.append((int(base + core_start), int(base + core_end)))
                continue

        refined.append((start, end))

    return refined


def trim_segment_heads_tails(segments, sr, frame_time, smoothed_score, rms_norm, zcr_norm, centroid_norm, bandwidth_norm):
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

        core_mask = close_mask(
            (seg_rms < 0.09)
            & (
                (seg_score > 0.25)
                | ((seg_zcr > 0.47) & (seg_cent > 0.45) & (seg_bw > 0.44))
            ),
            2,
        )
        voice_mask = close_mask(
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


def extend_breath_edges(segments, sr, frame_time, smoothed_score, rms_norm, zcr_norm, centroid_norm, bandwidth_norm):
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


def trim_loud_edges_by_threshold(segments, sr, frame_time, raw_rms, peak_reject_threshold, percentile_reject_threshold):
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


def trim_rising_voice_right_edges(segments, sr, frame_time, raw_rms, peak_reject_threshold, percentile_reject_threshold, voice_floor_threshold):
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

        smoothed = moving_average(seg_raw, 3)
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


def trim_following_voice_onset(segments, sr, frame_time, raw_rms, peak_reject_threshold, percentile_reject_threshold, voice_floor_threshold):
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

        seg_smooth = moving_average(seg_raw, 3)
        follow_smooth = moving_average(follow_raw, 3)
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


def snap_right_edge_to_tail_valley(segments, sr, frame_time, raw_rms, peak_reject_threshold, voice_floor_threshold):
    snapped = []
    if not segments:
        return snapped

    look_ahead_frames = max(12, int(round(0.55 / frame_time)))
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

        cut_idx = max(entry_idx, best_valley_idx)
        new_end_frame = start_frame + cut_idx
        new_end = int(new_end_frame * frame_time * sr)
        if new_end - start >= int(0.04 * sr):
            snapped.append((start, new_end))
        else:
            snapped.append((start, end))

    return snapped


def detect_low_voice_silence_segments(raw_rms, frame_time, sr, voice_floor_threshold):
    if voice_floor_threshold <= 0.0:
        return []
    silence_mask = close_mask(
        raw_rms <= voice_floor_threshold + 1e-6,
        max(1, int(round(0.12 / frame_time))),
    )
    return merge_segments(silence_mask, frame_time, sr, min_duration=0.08, max_duration=20.0)


def detect_breath_segments(y, sr, sensitivity, peak_reject_threshold=0.20, percentile_reject_threshold=0.20, voice_floor_threshold=0.0, min_segment_length_ms=0.0):
    frame_time = HOP_LENGTH / sr
    rms = librosa.feature.rms(y=y, hop_length=HOP_LENGTH)[0]
    flatness = librosa.feature.spectral_flatness(y=y, hop_length=HOP_LENGTH)[0]
    zcr = librosa.feature.zero_crossing_rate(y, hop_length=HOP_LENGTH)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP_LENGTH)[0]
    bandwidth = librosa.feature.spectral_bandwidth(y=y, sr=sr, hop_length=HOP_LENGTH)[0]
    raw_rms = np.asarray(rms, dtype=np.float32)

    if raw_rms.size == 0:
        diagnostics = {
            "candidate_hits": 0,
            "strict_hits": 0,
            "relaxed_hits": 0,
            "raw_segments": 0,
            "kept_segments": 0,
            "max_score": 0.0,
            "mean_score": 0.0,
            "avg_duration": 0.0,
            "avg_rise_ratio": 0.0,
            "smooth_frames": 0,
            "strict_threshold": 0.0,
        }
        return [], diagnostics

    rms_norm = percentile_norm(rms)
    flatness_norm = percentile_norm(flatness)
    zcr_norm = percentile_norm(zcr)
    centroid_norm = percentile_norm(centroid)
    bandwidth_norm = percentile_norm(bandwidth)
    global_voice_p75 = float(np.percentile(raw_rms, 75))
    global_voice_p87 = float(np.percentile(raw_rms, 87))
    global_noise_p35 = float(np.percentile(raw_rms, 35))
    # 动态气息能量上限：即使用户把 peak_reject 调高，也不允许超过音轨整体响度的
    # 一定比例，防止把歌唱段误判为气息。上限 = min(用户阈值, p75*0.62)，
    # 但不低于 0.05（保证低响度音轨仍有足够空间）。
    breath_rms_cap = float(np.clip(
        min(peak_reject_threshold, global_voice_p75 * 0.62),
        0.05, peak_reject_threshold,
    ))

    sensitivity = int(np.clip(sensitivity, 1, 10))
    energy_ceiling = np.clip(0.52 + (10 - sensitivity) * 0.045, 0.50, 0.92)
    energy_floor = np.clip(0.004 + (1 / max(sensitivity, 1)) * 0.01, 0.003, 0.02)
    noise_floor = np.clip(0.30 - sensitivity * 0.017, 0.08, 0.28)

    if len(rms_norm) > 4:
        lead_rms = np.pad(rms_norm[4:], (0, 4), mode="edge")
    else:
        lead_rms = np.full_like(rms_norm, rms_norm[-1])
    breath_score = (
        0.30 * flatness_norm
        + 0.22 * zcr_norm
        + 0.18 * centroid_norm
        + 0.15 * bandwidth_norm
        + 0.15 * np.clip(lead_rms - rms_norm, 0.0, 1.0)
    )
    smooth_frames = max(3, int(round(0.12 / frame_time)))
    smoothed_score = moving_average(breath_score, smooth_frames)

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
    strict_mask = close_mask(strict_mask, gap_tolerance)
    relaxed_gap = max(gap_tolerance, int(round(0.08 / frame_time)))
    relaxed_mask = close_mask(relaxed_mask, relaxed_gap)
    rescue_mask = close_mask(rescue_mask, max(relaxed_gap, int(round(0.10 / frame_time))))
    bright_breath_mask = close_mask(bright_breath_mask, max(relaxed_gap, int(round(0.10 / frame_time))))
    airy_breath_mask = close_mask(airy_breath_mask, max(relaxed_gap, int(round(0.16 / frame_time))))
    micro_breath_mask = close_mask(micro_breath_mask, max(relaxed_gap, int(round(0.08 / frame_time))))
    short_breath_mask = close_mask(short_breath_mask, max(gap_tolerance, int(round(0.05 / frame_time))))
    needle_breath_mask = close_mask(needle_breath_mask, max(gap_tolerance, int(round(0.06 / frame_time))))
    core_breath_mask = close_mask(core_breath_mask, max(gap_tolerance, int(round(0.07 / frame_time))))
    post_phrase_mask = close_mask(post_phrase_mask, max(relaxed_gap, int(round(0.10 / frame_time))))
    low_energy_breath_mask = close_mask(low_energy_breath_mask, max(gap_tolerance, int(round(0.08 / frame_time))))

    strict_segments = merge_segments(strict_mask, frame_time, sr)
    relaxed_segments = merge_segments(relaxed_mask, frame_time, sr, min_duration=0.08, max_duration=0.60)
    segments = list(strict_segments)
    if relaxed_segments:
        segments = sorted(segments + relaxed_segments, key=lambda item: item[0])
    rescue_segments = merge_segments(rescue_mask, frame_time, sr, min_duration=0.18, max_duration=0.40)
    bright_segments = merge_segments(bright_breath_mask, frame_time, sr, min_duration=0.16, max_duration=0.48)
    airy_segments = merge_segments(airy_breath_mask, frame_time, sr, min_duration=0.15, max_duration=0.50)
    micro_segments = merge_segments(micro_breath_mask, frame_time, sr, min_duration=0.12, max_duration=0.50)
    short_segments = merge_segments(short_breath_mask, frame_time, sr, min_duration=0.04, max_duration=0.32)
    needle_segments = merge_segments(needle_breath_mask, frame_time, sr, min_duration=0.16, max_duration=0.40)
    core_segments = merge_segments(core_breath_mask, frame_time, sr, min_duration=0.14, max_duration=0.42)
    post_phrase_segments = merge_segments(post_phrase_mask, frame_time, sr, min_duration=0.10, max_duration=0.65)
    low_energy_segments = merge_segments(low_energy_breath_mask, frame_time, sr, min_duration=0.08, max_duration=0.52)
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

    silence_mask = close_mask(rms_norm < 0.05, max(1, int(round(0.80 / frame_time))))
    silence_segments = merge_segments(silence_mask, frame_time, sr, min_duration=3.0, max_duration=20.0)
    if silence_segments:
        segments = sorted(segments + silence_segments, key=lambda item: item[0])
    floor_silence_segments = detect_low_voice_silence_segments(raw_rms, frame_time, sr, voice_floor_threshold)
    if floor_silence_segments:
        floor_silence_segments = snap_right_edge_to_tail_valley(
            floor_silence_segments,
            sr,
            frame_time,
            raw_rms,
            peak_reject_threshold,
            voice_floor_threshold,
        )
    if floor_silence_segments:
        segments = sorted(segments + floor_silence_segments, key=lambda item: item[0])

    segments = refine_segment_to_core(
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

        segment_score = sample_slice(smoothed_score, start_frame, end_frame)
        segment_rms = sample_slice(rms_norm, start_frame, end_frame)
        segment_flatness = sample_slice(flatness_norm, start_frame, end_frame)
        segment_zcr = sample_slice(zcr_norm, start_frame, end_frame)
        segment_centroid = sample_slice(centroid_norm, start_frame, end_frame)
        segment_bandwidth = sample_slice(bandwidth_norm, start_frame, end_frame)
        segment_raw_rms = sample_slice(raw_rms, start_frame, end_frame)
        follow_rms = sample_slice(rms_norm, end_frame, end_frame + max(2, int(round(0.18 / frame_time))))
        lead_rms_segment = sample_slice(rms_norm, max(0, start_frame - max(2, int(round(0.10 / frame_time)))), start_frame)
        lead_raw_segment = sample_slice(raw_rms, max(0, start_frame - max(2, int(round(0.20 / frame_time)))), start_frame)
        pre_peak_raw_segment = sample_slice(raw_rms, max(0, start_frame - max(4, int(round(2.20 / frame_time)))), start_frame)
        follow_raw_segment = sample_slice(raw_rms, end_frame, end_frame + max(2, int(round(0.20 / frame_time))))

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
        lead_decay_slope = linear_slope(lead_raw_segment)
        seg_shape_slope = linear_slope(segment_raw_rms)
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
        hard_peak_reject = max_raw_rms > (breath_rms_cap + 1e-6)
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
            and max_raw_rms <= breath_rms_cap + 1e-6
        )

        # 相对谷值气息：仅在用户主动提高 peak_reject（>0.08）时激活。
        # 判断标准：段能量低于前后各1s的中位数，且前后都明显更高（真实谷值）。
        # breath_rms_cap 仍然是上限，保证不会把响亮的歌声误判为气息。
        valley_lead_raw = sample_slice(raw_rms, max(0, start_frame - max(3, int(round(1.0 / frame_time)))), start_frame)
        valley_follow_raw = sample_slice(raw_rms, end_frame, end_frame + max(3, int(round(1.0 / frame_time))))
        valley_lead_med = float(np.median(valley_lead_raw)) if len(valley_lead_raw) else 0.0
        valley_follow_med = float(np.median(valley_follow_raw)) if len(valley_follow_raw) else 0.0
        valley_ratio_lead = (valley_lead_med + 1e-6) / (mean_raw_rms + 1e-6)
        valley_ratio_follow = (valley_follow_med + 1e-6) / (mean_raw_rms + 1e-6)
        relative_valley_keep = (
            peak_reject_threshold > 0.08
            and not hard_peak_reject
            and not hard_percentile_reject
            and 0.05 <= duration <= 0.45
            and valley_ratio_lead >= 2.0
            and valley_ratio_follow >= 2.0
            and valley_lead_med >= breath_rms_cap * 0.55
            and valley_follow_med >= breath_rms_cap * 0.55
            and mean_raw_rms <= breath_rms_cap * 0.75
        )

        keep = low_voice_force_keep or relative_valley_keep or (
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

    scored_segments = merge_nearby_segments(scored_segments, sr, max_gap=0.16, max_duration=0.72)
    filtered = []
    last_end = -1
    for item in scored_segments:
        if item["start"] < last_end - int(0.04 * sr):
            continue
        filtered.append((item["start"], min(item["end"], len(y))))
        last_end = item["end"]

    filtered = expand_segment_edges(
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
    filtered = refine_segment_to_core(
        filtered,
        sr,
        frame_time,
        smoothed_score,
        rms_norm,
        zcr_norm,
        centroid_norm,
    )
    filtered = trim_segment_heads_tails(
        filtered,
        sr,
        frame_time,
        smoothed_score,
        rms_norm,
        zcr_norm,
        centroid_norm,
        bandwidth_norm,
    )
    filtered = extend_breath_edges(
        filtered,
        sr,
        frame_time,
        smoothed_score,
        rms_norm,
        zcr_norm,
        centroid_norm,
        bandwidth_norm,
    )
    filtered = trim_loud_edges_by_threshold(
        filtered,
        sr,
        frame_time,
        raw_rms,
        breath_rms_cap,
        percentile_reject_threshold,
    )
    filtered = trim_rising_voice_right_edges(
        filtered,
        sr,
        frame_time,
        raw_rms,
        breath_rms_cap,
        percentile_reject_threshold,
        voice_floor_threshold,
    )
    filtered = trim_following_voice_onset(
        filtered,
        sr,
        frame_time,
        raw_rms,
        breath_rms_cap,
        percentile_reject_threshold,
        voice_floor_threshold,
    )
    filtered = snap_right_edge_to_tail_valley(
        filtered,
        sr,
        frame_time,
        raw_rms,
        breath_rms_cap,
        voice_floor_threshold,
    )
    if floor_silence_segments:
        filtered_ranges = [(start / sr, end / sr) for start, end in filtered]
        floor_ranges = [(start / sr, end / sr) for start, end in floor_silence_segments]
        floor_ranges = subtract_time_ranges(floor_ranges, filtered_ranges)
        filtered = merge_time_ranges(
            filtered_ranges + floor_ranges,
            min_gap_sec=0.0,
        )
        filtered = time_ranges_to_samples(filtered, sr, len(y))

    if min_segment_length_ms > 0:
        min_samples = int((min_segment_length_ms / 1000.0) * sr)
        filtered = [(start, end) for start, end in filtered if (end - start) >= min_samples]

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

def format_diagnostics_text(diagnostics, segment_count):
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

def expand_segments(segments, sr, total_length, left_append_ms=LEFT_APPEND_MS, right_append_ms=RIGHT_APPEND_MS):
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
    merged = merge_time_ranges([(start / sr, end / sr) for start, end in expanded], min_gap_sec=0.002)
    return time_ranges_to_samples(merged, sr, total_length)

def merge_time_ranges(ranges, min_gap_sec=0.0):
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

def subtract_time_ranges(base_ranges, remove_ranges):
    if not base_ranges:
        return []
    if not remove_ranges:
        return [(float(start), float(end)) for start, end in base_ranges if end > start]

    remaining = [(float(start), float(end)) for start, end in base_ranges if end > start]
    for remove_start, remove_end in merge_time_ranges(remove_ranges):
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
    return merge_time_ranges(remaining)


def time_ranges_to_samples(ranges, sr, total_length):
    segments = []
    for start_sec, end_sec in ranges:
        start = max(0, int(round(start_sec * sr)))
        end = min(total_length, int(round(end_sec * sr)))
        if end > start:
            segments.append((start, end))
    return segments


def intersect_time_ranges(base_ranges, target_range):
    target_start, target_end = float(target_range[0]), float(target_range[1])
    if target_end <= target_start:
        return []
    overlaps = []
    for start, end in base_ranges:
        overlap_start = max(float(start), target_start)
        overlap_end = min(float(end), target_end)
        if overlap_end > overlap_start:
            overlaps.append((overlap_start, overlap_end))
    return merge_time_ranges(overlaps)

