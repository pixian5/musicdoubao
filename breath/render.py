"""Segment render, half-time, and breath gain application."""
import numpy as np

from .constants import HALF_TIME_MATCH_TOLERANCE_SEC

def merge_sample_segments(segments, total_length=None):
    """Merge overlapping/touching sample ranges so render never double-appends audio."""
    cleaned = []
    for start, end in segments or []:
        start_i = int(start)
        end_i = int(end)
        if total_length is not None:
            total_i = int(total_length)
            start_i = max(0, min(start_i, total_i))
            end_i = max(0, min(end_i, total_i))
        if end_i > start_i:
            cleaned.append((start_i, end_i))
    if not cleaned:
        return []
    cleaned.sort(key=lambda item: (item[0], item[1]))
    merged = [[cleaned[0][0], cleaned[0][1]]]
    for start_i, end_i in cleaned[1:]:
        prev = merged[-1]
        if start_i <= prev[1]:
            prev[1] = max(prev[1], end_i)
        else:
            merged.append([start_i, end_i])
    return [(start_i, end_i) for start_i, end_i in merged]


def is_half_time_sample_segment(start, end, half_time_segments, sr, tolerance_sec=HALF_TIME_MATCH_TOLERANCE_SEC):
    """True if this sample range intersects any half-time range (or matches within tolerance)."""
    if not half_time_segments or end <= start:
        return False
    start = int(start)
    end = int(end)
    tol = max(1, int(round(float(tolerance_sec) * float(sr or 1))))
    for half_start, half_end in half_time_segments:
        hs = int(half_start)
        he = int(half_end)
        if he <= hs:
            continue
        if abs(hs - start) <= tol and abs(he - end) <= tol:
            return True
        if max(0, min(end, he) - max(start, hs)) > 0:
            return True
    return False


def half_time_overlaps_in_range(start, end, half_time_segments):
    """Merged half-time sample ranges clipped to [start, end]."""
    start = int(start)
    end = int(end)
    if end <= start or not half_time_segments:
        return []
    overlaps = []
    for half_start, half_end in half_time_segments:
        hs = max(start, int(half_start))
        he = min(end, int(half_end))
        if he > hs:
            overlaps.append((hs, he))
    return merge_sample_segments(overlaps, total_length=end)


def split_segment_by_half_time(start, end, half_time_segments):
    """
    Split [start, end] into contiguous pieces tagged with half-time.
    Ensures half-time still applies after breath segments are merged.
    """
    start = int(start)
    end = int(end)
    if end <= start:
        return []
    halves = half_time_overlaps_in_range(start, end, half_time_segments)
    if not halves:
        return [(start, end, False)]
    pieces = []
    cursor = start
    for hs, he in halves:
        if hs > cursor:
            pieces.append((cursor, hs, False))
        pieces.append((hs, he, True))
        cursor = he
    if cursor < end:
        pieces.append((cursor, end, False))
    return pieces

def build_half_time_segment(segment):
    segment = np.asarray(segment, dtype=np.float32)
    keep_len = max(1, len(segment) // 2)
    if len(segment) <= 2:
        if segment.ndim > 1:
            return np.zeros((keep_len, segment.shape[1]), dtype=np.float32)
        return np.zeros(keep_len, dtype=np.float32)
        
    source_idx = np.linspace(0, len(segment) - 1, keep_len, dtype=np.float32)
    original_idx = np.arange(len(segment), dtype=np.float32)
    
    if segment.ndim > 1:
        compressed = np.empty((keep_len, segment.shape[1]), dtype=np.float32)
        for ch in range(segment.shape[1]):
            compressed[:, ch] = np.interp(source_idx, original_idx, segment[:, ch])
    else:
        compressed = np.interp(source_idx, original_idx, segment).astype(np.float32)
        
    fade_len = min(max(16, keep_len // 8), keep_len)
    if fade_len > 1:
        fade_in = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
        fade_out = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
        if compressed.ndim > 1:
            fade_in = fade_in[:, None]
            fade_out = fade_out[:, None]
        compressed[:fade_len] *= fade_in
        compressed[keep_len - fade_len:keep_len] *= fade_out
    return compressed


def build_breath_silence_envelope(segment, sr, gain):
    segment = np.asarray(segment, dtype=np.float32)
    sample_count = segment.shape[0] if segment.ndim > 1 else len(segment)
    center_gain = float(np.clip(gain, 0.0, 1.0))
    if center_gain <= 0.035:
        center_gain = 0.0
    envelope = np.full(sample_count, center_gain, dtype=np.float32)
    if sample_count == 0:
        return envelope

    left_feather_len = min(int(0.050 * sr), max(sample_count - 1, 1))
    right_feather_len = min(int(0.050 * sr), max(sample_count - 1, 1))
    overlap_guard = max(1, sample_count // 2)
    left_feather_len = min(left_feather_len, overlap_guard)
    right_feather_len = min(right_feather_len, overlap_guard)

    if left_feather_len > 1:
        left_mix = 0.5 + 0.5 * np.cos(np.linspace(0.0, np.pi, left_feather_len, dtype=np.float32))
        left_curve = center_gain + (1.0 - center_gain) * left_mix
        envelope[:left_feather_len] = np.maximum(envelope[:left_feather_len], left_curve)
    else:
        envelope[0] = 1.0

    if right_feather_len > 1:
        right_mix = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, right_feather_len, dtype=np.float32))
        right_curve = center_gain + (1.0 - center_gain) * right_mix
        envelope[-right_feather_len:] = np.maximum(envelope[-right_feather_len:], right_curve)
    else:
        envelope[-1] = 1.0

    return np.clip(envelope, 0.0, 1.0)


def build_processed_segment(segment, sr, gain, half_time=False):
    segment = np.asarray(segment, dtype=np.float32)
    if half_time:
        compressed = build_half_time_segment(segment)
        envelope = build_breath_silence_envelope(compressed, sr, gain).astype(np.float32)
        if compressed.ndim > 1:
            return compressed * envelope[:, None]
        return compressed * envelope
    envelope = build_breath_silence_envelope(segment, sr, gain).astype(np.float32)
    if segment.ndim > 1:
        return segment * envelope[:, None]
    return segment * envelope


def render_output_audio(y, sr, breath_segments, atten_db=18, half_time_segments=None):
    source_audio = np.asarray(y, dtype=np.float32)
    gain = np.power(10, -atten_db / 20)
    total_length = len(source_audio)
    # Force non-overlapping sample ranges so cursor-based concat never double-counts.
    breath_segments = merge_sample_segments(breath_segments, total_length=total_length)
    half_time_segments = merge_sample_segments(half_time_segments or [], total_length=total_length)
    rendered_chunks = []
    timeline_segments = []
    cursor = 0
    output_cursor = 0

    for start, end in breath_segments:
        start = max(0, int(start))
        end = min(int(end), total_length)
        if end <= start:
            continue
        if start < cursor:
            # Defensive: merged list should not overlap; clamp if it does.
            start = cursor
            if end <= start:
                continue
        if start > cursor:
            chunk = np.asarray(source_audio[cursor:start], dtype=np.float32)
            rendered_chunks.append(chunk)
            next_cursor = output_cursor + len(chunk)
            timeline_segments.append((cursor, start, output_cursor, next_cursor))
            output_cursor = next_cursor

        # Split by half-time intersections so merge of adjacent breath segments
        # does not drop partial half-time ranges.
        for piece_start, piece_end, use_half in split_segment_by_half_time(start, end, half_time_segments):
            segment = np.asarray(source_audio[piece_start:piece_end], dtype=np.float32)
            processed = build_processed_segment(segment, sr, gain, half_time=use_half)
            rendered_chunks.append(processed)
            next_cursor = output_cursor + len(processed)
            timeline_segments.append((piece_start, piece_end, output_cursor, next_cursor))
            output_cursor = next_cursor
        cursor = end

    if cursor < total_length:
        chunk = np.asarray(source_audio[cursor:], dtype=np.float32)
        rendered_chunks.append(chunk)
        next_cursor = output_cursor + len(chunk)
        timeline_segments.append((cursor, total_length, output_cursor, next_cursor))
        output_cursor = next_cursor

    if not rendered_chunks:
        return np.asarray([], dtype=np.float32), []
    return np.concatenate(rendered_chunks, axis=0).astype(np.float32), timeline_segments



def apply_breath_segments(y, sr, breath_segments, atten_db=18, half_time_segments=None):
    output_audio, _ = render_output_audio(
        y,
        sr,
        breath_segments,
        atten_db=atten_db,
        half_time_segments=half_time_segments,
    )
    return output_audio
