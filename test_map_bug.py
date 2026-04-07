import numpy as np

sr = 1000
HOP_LENGTH = 512

timeline_segments = [
    (0, int(60.3 * sr), 0, int(60.3 * sr)),
    (int(60.3 * sr), int(62.1 * sr), int(60.3 * sr), int(61.2 * sr)), # half time block
    (int(62.1 * sr), int(65.0 * sr), int(61.2 * sr), int(64.1 * sr))
]

output_len = int(64.1 * sr)

times = np.zeros(output_len // HOP_LENGTH + 1)
positions = np.arange(len(times), dtype=np.float32) * HOP_LENGTH

source_positions = np.empty_like(positions, dtype=np.float32)
last_filled = np.zeros_like(positions, dtype=bool)

for idx, (src_start, src_end, out_start, out_end) in enumerate(timeline_segments):
    if idx == len(timeline_segments) - 1:
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

times_mapped = source_positions / sr

print("Original pos(s) | Mapped time(s)")
print(f"{positions[-1]/sr:.3f} | {times_mapped[-1]:.3f}")

# Test a point at source time 62.1+0.2=62.3
# In output, it is at 61.2+0.2 = 61.4
val = 61.4 * sr
idx = np.argmin(np.abs(positions - val))
print(f"Output 61.4 mapped to {times_mapped[idx]:.3f} (expect 62.3)")
