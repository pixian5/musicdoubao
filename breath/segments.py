"""Segment data model: breath ranges with optional half-time subranges.

Audio still uses parallel sample lists at render time; this module is the
preferred structured form for new edit paths and for documenting the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BreathSegment:
    """One effective breath segment in seconds (source timeline)."""

    start: float
    end: float
    # Absolute half-time subranges in seconds, clipped to [start, end].
    half_ranges: list[tuple[float, float]] = field(default_factory=list)

    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))

    def with_half(self, half_start: float, half_end: float) -> "BreathSegment":
        hs = max(float(self.start), float(half_start))
        he = min(float(self.end), float(half_end))
        halves = list(self.half_ranges)
        if he > hs:
            halves.append((hs, he))
        return BreathSegment(self.start, self.end, _merge_halves(halves))

    def clear_half(self) -> "BreathSegment":
        return BreathSegment(self.start, self.end, [])


def _merge_halves(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cleaned = sorted((float(s), float(e)) for s, e in ranges if e > s)
    if not cleaned:
        return []
    out = [list(cleaned[0])]
    for s, e in cleaned[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def segments_from_parallel(
    breath_ranges_sec: list[tuple[float, float]],
    half_ranges_sec: list[tuple[float, float]] | None = None,
) -> list[BreathSegment]:
    """Build structured segments from parallel breath + half-time range lists."""
    half_ranges_sec = list(half_ranges_sec or [])
    result: list[BreathSegment] = []
    for start, end in breath_ranges_sec:
        start_f, end_f = float(start), float(end)
        if end_f <= start_f:
            continue
        halves = []
        for hs, he in half_ranges_sec:
            ov_s = max(start_f, float(hs))
            ov_e = min(end_f, float(he))
            if ov_e > ov_s:
                halves.append((ov_s, ov_e))
        result.append(BreathSegment(start_f, end_f, _merge_halves(halves)))
    return result


def parallel_from_segments(
    segments: list[BreathSegment],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Flatten structured segments back to parallel lists for render/UI."""
    breath = [(s.start, s.end) for s in segments if s.end > s.start]
    halves: list[tuple[float, float]] = []
    for s in segments:
        halves.extend(s.half_ranges)
    return breath, _merge_halves(halves)
