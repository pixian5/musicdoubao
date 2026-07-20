"""Breath inhale reduction library (split from breath_reduce_mac monolith)."""
from .constants import *  # noqa: F401,F403
from .config import event_log, load_app_config, save_app_config
from .detect import (
    detect_breath_segments,
    expand_segments,
    format_diagnostics_text,
    merge_time_ranges,
    subtract_time_ranges,
    time_ranges_to_samples,
    intersect_time_ranges,
)
from .render import (
    merge_sample_segments,
    is_half_time_sample_segment,
    half_time_overlaps_in_range,
    split_segment_by_half_time,
    render_output_audio,
    apply_breath_segments,
    build_half_time_segment,
    build_breath_silence_envelope,
    build_processed_segment,
)
from .limit import (
    finalize_rendered_output,
    apply_hot_peak_limiter,
    compute_loud_phrase_taming_gain,
    apply_sample_gain_curve,
    audio_buffers_share_content,
    apply_output_headroom,
    smooth_gain_attack_release,
)
from .io import (
    process_breath,
    load_audio_for_processing,
    load_actual_output_audio,
    write_output_mp3,
    find_ffmpeg_binary,
    build_output_path,
    run_ffmpeg,
)
from .segments import (
    BreathSegment,
    segments_from_parallel,
    parallel_from_segments,
)

__all__ = [
    "VERSION",
    "process_breath",
    "detect_breath_segments",
    "render_output_audio",
    "finalize_rendered_output",
    "BreathSegment",
    "segments_from_parallel",
    "parallel_from_segments",
]
