"""Loudness taming and peak limiting."""
import numpy as np

from .constants import LIMITER_CONTROL_RATE_HZ
from .detect import moving_average

def smooth_gain_attack_release(target_gain, sr, attack_sec, release_sec, control_rate_hz=LIMITER_CONTROL_RATE_HZ):
    """Attack/release envelope with optional control-rate downsampling for speed."""
    target_gain = np.asarray(target_gain, dtype=np.float32)
    n = len(target_gain)
    if n == 0:
        return target_gain

    hop = max(1, int(round(float(sr) / max(float(control_rate_hz), 1.0))))
    if hop > 1 and n > hop * 4:
        pad = (-n) % hop
        padded = np.pad(target_gain, (0, pad), mode="edge")
        blocks = padded.reshape(-1, hop)
        # Prefer more reduction within each block so peaks stay limited after upsample.
        coarse = np.min(blocks, axis=1).astype(np.float32)
        coarse_sr = float(sr) / float(hop)
        attack_coeff = float(np.exp(-1.0 / max(1.0, coarse_sr * attack_sec)))
        release_coeff = float(np.exp(-1.0 / max(1.0, coarse_sr * release_sec)))
        smooth = np.empty_like(coarse)
        smooth[0] = coarse[0]
        for idx in range(1, len(coarse)):
            coeff = attack_coeff if coarse[idx] < smooth[idx - 1] else release_coeff
            smooth[idx] = coeff * smooth[idx - 1] + (1.0 - coeff) * coarse[idx]
        # Piecewise-constant upsample: each coarse sample covers `hop` output samples.
        # Avoids allocating full x_full/x_coarse and O(N) interp work.
        up = np.repeat(smooth, hop)[:n]
        return up.astype(np.float32, copy=False)

    attack_coeff = float(np.exp(-1.0 / max(1.0, float(sr) * attack_sec)))
    release_coeff = float(np.exp(-1.0 / max(1.0, float(sr) * release_sec)))
    smooth = np.empty_like(target_gain)
    smooth[0] = target_gain[0]
    for idx in range(1, n):
        coeff = attack_coeff if target_gain[idx] < smooth[idx - 1] else release_coeff
        smooth[idx] = coeff * smooth[idx - 1] + (1.0 - coeff) * target_gain[idx]
    return smooth

def apply_output_headroom(audio, target_peak=0.98):
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return audio, 1.0
    peak = float(np.max(np.abs(audio)))
    if peak <= 0.0 or peak <= float(target_peak):
        return audio, 1.0
    gain = float(target_peak) / peak
    return (audio * gain).astype(np.float32), gain


def apply_hot_peak_limiter(audio, sr, threshold=0.84):
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return audio
    if audio.ndim == 1:
        control = np.abs(audio)
    else:
        control = np.max(np.abs(audio), axis=1)
    if control.size == 0:
        return audio.astype(np.float32)

    target_gain = np.ones_like(control, dtype=np.float32)
    hot_mask = control > float(threshold)
    if not np.any(hot_mask):
        return audio.astype(np.float32)
    target_gain[hot_mask] = float(threshold) / np.maximum(control[hot_mask], 1e-6)

    gain_curve = smooth_gain_attack_release(
        target_gain,
        sr,
        attack_sec=0.0008,
        release_sec=0.080,
    )

    lookahead = max(1, int(round(sr * 0.004)))
    if len(gain_curve) > lookahead:
        shifted = gain_curve.copy()
        shifted[:-lookahead] = gain_curve[lookahead:]
        shifted[-lookahead:] = gain_curve[-1]
        gain_curve = np.minimum(gain_curve, shifted)

    gain_curve = np.clip(gain_curve, 0.55, 1.0).astype(np.float32)
    return apply_sample_gain_curve(audio, gain_curve)


def smoothstep(values):
    values = np.asarray(values, dtype=np.float32)
    clipped = np.clip(values, 0.0, 1.0)
    return clipped * clipped * (3.0 - 2.0 * clipped)


def compute_loud_phrase_taming_gain(audio, sr):
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return np.asarray([], dtype=np.float32), {
            "active": False,
            "threshold": 0.0,
            "peak": 0.0,
            "min_gain": 1.0,
        }

    if audio.ndim == 1:
        control = np.abs(audio)
    else:
        control = np.max(np.abs(audio), axis=1)
    if control.size == 0:
        return np.asarray([], dtype=np.float32), {
            "active": False,
            "threshold": 0.0,
            "peak": 0.0,
            "min_gain": 1.0,
        }

    fast_window = max(1, int(round(sr * 0.0015)))
    body_window = max(1, int(round(sr * 0.010)))
    fast_env = moving_average(control, fast_window)
    body_env = moving_average(control, body_window)
    control_env = np.maximum(fast_env, body_env * 1.06)
    control_peak = float(np.max(control_env))
    if control_peak <= 0.0:
        return np.ones_like(control_env, dtype=np.float32), {
            "active": False,
            "threshold": 0.0,
            "peak": control_peak,
            "min_gain": 1.0,
        }

    p90 = float(np.percentile(control_env, 90))
    p96 = float(np.percentile(control_env, 96))
    p995 = float(np.percentile(control_env, 99.5))
    threshold = max(p96, p90 * 1.12, 0.14)
    if control_peak <= threshold * 1.02:
        return np.ones_like(control_env, dtype=np.float32), {
            "active": False,
            "threshold": threshold,
            "peak": control_peak,
            "min_gain": 1.0,
        }

    ratio = 4.8
    knee = max(threshold * 0.34, 0.025)
    desired = control_env.copy()
    hard_mask = control_env > threshold
    desired[hard_mask] = threshold + (control_env[hard_mask] - threshold) / ratio
    hard_gain = np.ones_like(control_env, dtype=np.float32)
    hard_gain[hard_mask] = desired[hard_mask] / np.maximum(control_env[hard_mask], 1e-6)

    knee_start = threshold - knee
    knee_end = threshold + knee
    knee_mix = smoothstep((control_env - knee_start) / max(knee_end - knee_start, 1e-6))
    target_gain = 1.0 - knee_mix * (1.0 - hard_gain)

    extra_hot = np.clip((control_env - p995) / max(control_peak - p995, 1e-6), 0.0, 1.0)
    fast_hot = np.clip((fast_env - threshold) / max(control_peak - threshold, 1e-6), 0.0, 1.0)
    transient_ratio = fast_env / np.maximum(body_env, 1e-4)
    transient_hot = np.clip((transient_ratio - 1.08) / 0.55, 0.0, 1.0) * np.clip(
        (fast_env - threshold * 0.90) / max(control_peak - threshold * 0.90, 1e-6),
        0.0,
        1.0,
    )
    target_gain *= 1.0 - 0.18 * extra_hot
    target_gain *= 1.0 - 0.16 * fast_hot
    target_gain *= 1.0 - 0.32 * transient_hot
    target_gain = np.clip(target_gain, 0.38, 1.0)

    smoothed_gain = smooth_gain_attack_release(
        target_gain,
        sr,
        attack_sec=0.0018,
        release_sec=0.120,
    )
    lookahead = max(1, int(round(sr * 0.008)))
    if len(smoothed_gain) > lookahead:
        lookahead_gain = smoothed_gain.copy()
        lookahead_gain[:-lookahead] = smoothed_gain[lookahead:]
        lookahead_gain[-lookahead:] = smoothed_gain[-1]
        smoothed_gain = np.minimum(smoothed_gain, lookahead_gain)
    smoothed_gain = np.clip(smoothed_gain, 0.38, 1.0).astype(np.float32)
    return smoothed_gain, {
        "active": True,
        "threshold": threshold,
        "peak": control_peak,
        "min_gain": float(np.min(smoothed_gain)),
    }


def apply_sample_gain_curve(audio, gain_curve, *, in_place=False):
    audio = np.asarray(audio, dtype=np.float32)
    gain_curve = np.asarray(gain_curve, dtype=np.float32)
    if audio.size == 0 or gain_curve.size == 0:
        return audio.astype(np.float32)
    usable = min(len(audio), len(gain_curve))
    if in_place:
        adjusted = audio
    else:
        adjusted = audio.copy()
    if adjusted.ndim == 1:
        adjusted[:usable] *= gain_curve[:usable]
    else:
        adjusted[:usable] *= gain_curve[:usable, None]
    return adjusted.astype(np.float32, copy=False)




def audio_buffers_share_content(a, b):
    """True when plot/playback can share one limited/render result (mono shared buffer)."""
    if a is b:
        return True
    a = np.asarray(a)
    b = np.asarray(b)
    if a.shape != b.shape or a.ndim != 1 or b.ndim != 1:
        return False
    # Same underlying memory or exact object content for mono.
    return a.__array_interface__["data"][0] == b.__array_interface__["data"][0]


def finalize_rendered_output(output_plot_audio, output_playback_audio, sr):
    output_plot_audio = np.asarray(output_plot_audio, dtype=np.float32)
    output_playback_audio = np.asarray(output_playback_audio, dtype=np.float32)
    share = audio_buffers_share_content(output_plot_audio, output_playback_audio)

    loud_taming_gain, _ = compute_loud_phrase_taming_gain(output_playback_audio, sr)
    output_playback_audio = apply_sample_gain_curve(output_playback_audio, loud_taming_gain)
    if share:
        output_plot_audio = output_playback_audio
    else:
        output_plot_audio = apply_sample_gain_curve(output_plot_audio, loud_taming_gain)

    output_playback_audio, output_headroom_gain = apply_output_headroom(output_playback_audio)
    if share:
        output_plot_audio = output_playback_audio
    else:
        output_plot_audio = (output_plot_audio * output_headroom_gain).astype(np.float32)

    output_playback_audio = apply_hot_peak_limiter(output_playback_audio, sr)
    if share:
        output_plot_audio = output_playback_audio
    else:
        output_plot_audio = apply_hot_peak_limiter(output_plot_audio, sr)
    return (
        np.asarray(output_plot_audio, dtype=np.float32),
        np.asarray(output_playback_audio, dtype=np.float32),
        float(output_headroom_gain),
    )

