"""ffmpeg IO and top-level process_breath orchestration."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from .constants import LEFT_APPEND_MS, RIGHT_APPEND_MS, VERSION
from .detect import detect_breath_segments, expand_segments
from .limit import audio_buffers_share_content, finalize_rendered_output
from .render import render_output_audio

def build_output_path(input_path):
    source = Path(input_path)
    return source.with_name(f"{source.stem}_v{VERSION}.mp3")

def write_output_mp3(y_processed, sr, output_path, bitrate_kbps=128):
    ffmpeg_bin = find_ffmpeg_binary()
    temp_wav = tempfile.NamedTemporaryFile(prefix="breath_processed_", suffix=".wav", delete=False)
    temp_wav.close()
    sf.write(temp_wav.name, y_processed, sr, subtype="FLOAT")
    bitrate_kbps = int(np.clip(int(bitrate_kbps), 64, 320))
    try:
        run_ffmpeg(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                temp_wav.name,
                "-codec:a",
                "libmp3lame",
                "-b:a",
                f"{bitrate_kbps}k",
                str(output_path),
            ],
            error_prefix="导出 MP3 失败",
        )
    finally:
        if os.path.exists(temp_wav.name):
            os.remove(temp_wav.name)


def find_ffmpeg_binary():
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
def run_ffmpeg(args, error_prefix="ffmpeg 失败"):
    """Run ffmpeg and surface stderr tail on failure."""
    try:
        completed = subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed
    except FileNotFoundError as exc:
        raise RuntimeError(f"{error_prefix}：找不到可执行文件 {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr_text = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        tail = stderr_text[-800:] if stderr_text else "(无 stderr)"
        raise RuntimeError(f"{error_prefix}：\n{tail}") from exc

def load_audio_for_processing(input_path):
    ffmpeg_bin = find_ffmpeg_binary()
    temp_wav = tempfile.NamedTemporaryFile(prefix="breath_input_decode_", suffix=".wav", delete=False)
    temp_wav.close()
    try:
        run_ffmpeg(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                input_path,
                "-vn",
                "-map",
                "a:0",
                "-acodec",
                "pcm_f32le",
                temp_wav.name,
            ],
            error_prefix="解码音频失败",
        )
        y_full, sr = sf.read(temp_wav.name, dtype="float32", always_2d=False)
    finally:
        if os.path.exists(temp_wav.name):
            os.remove(temp_wav.name)
    y_full = np.asarray(y_full, dtype=np.float32)
    if y_full.ndim == 1:
        # Mono: share one buffer for analysis and playback (no forced copy).
        playback_audio = y_full
        analysis_audio = y_full
    else:
        playback_audio = np.asarray(y_full, dtype=np.float32)
        analysis_audio = np.asarray(librosa.to_mono(y_full.T), dtype=np.float32)
    return analysis_audio, playback_audio, sr

def load_actual_output_audio(output_path):
    analysis_audio, playback_audio, _ = load_audio_for_processing(str(output_path))
    return np.asarray(analysis_audio, dtype=np.float32), np.asarray(playback_audio, dtype=np.float32)

def process_breath(
    input_path,
    atten_db=18,
    sensitivity=7,
    peak_reject_threshold=0.20,
    percentile_reject_threshold=0.20,
    voice_floor_threshold=0.0,
    left_append_ms=LEFT_APPEND_MS,
    right_append_ms=RIGHT_APPEND_MS,
    min_segment_length_ms=0.0,
):
    try:
        analysis_audio, playback_audio, sr = load_audio_for_processing(input_path)
        breath_segments, diagnostics = detect_breath_segments(
            analysis_audio,
            sr,
            sensitivity,
            peak_reject_threshold,
            percentile_reject_threshold,
            voice_floor_threshold,
            min_segment_length_ms,
        )
        breath_segments = expand_segments(
            breath_segments,
            sr,
            len(analysis_audio),
            left_append_ms=left_append_ms,
            right_append_ms=right_append_ms,
        )
        limited_plot, limited_playback, output_headroom_gain = finalize_rendered_output(
            analysis_audio,
            playback_audio,
            sr,
        )
        y_processed_plot, output_timeline_segments = render_output_audio(
            limited_plot,
            sr,
            breath_segments,
            atten_db=atten_db,
        )
        # Mono (shared limited buffers): one render is enough for plot + playback.
        if audio_buffers_share_content(limited_plot, limited_playback):
            y_processed_playback = y_processed_plot
        else:
            y_processed_playback, _ = render_output_audio(
                limited_playback,
                sr,
                breath_segments,
                atten_db=atten_db,
            )
        output_path = build_output_path(input_path)

        return {
            "source_audio": analysis_audio,
            "limited_source_audio": limited_plot,
            "limited_playback_audio": limited_playback,
            "output_audio": y_processed_plot,
            "output_display_audio": y_processed_plot,
            "source_playback_audio": playback_audio,
            "output_playback_audio": y_processed_playback,
            "sr": sr,
            "segments": breath_segments,
            "auto_segments": list(breath_segments),
            "diagnostics": diagnostics,
            "output_path": str(output_path),
            "output_timeline_segments": output_timeline_segments,
            "output_headroom_gain": output_headroom_gain,
        }
    except Exception as exc:
        raise RuntimeError(f"处理失败：{exc}") from exc
