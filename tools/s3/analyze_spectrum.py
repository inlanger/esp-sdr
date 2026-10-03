#!/usr/bin/env python3
"""Plot measured SPC1 power codes and SPS1 telemetry; no dBm conversion."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('capture', type=Path)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
summary = json.loads((args.capture / 'summary.json').read_text())
fs, _, bins = summary['profile'][:3]
total = np.zeros(bins, dtype=np.float64)
maximum = np.zeros(bins, dtype=np.uint8)
buckets, bucket_counts, telemetry = {}, Counter(), []
gains = Counter()
frames = floor = ceiling = 0
first_host = None
with (args.capture / 'records.jsonl').open() as source:
    for line in source:
        row = json.loads(line)
        if first_host is None:
            first_host = row['host_monotonic_ns']
        if row['kind'] == 'stats':
            telemetry.append({'host_seconds': (row['host_monotonic_ns'] - first_host) / 1e9, **row})
        if row['kind'] != 'frame':
            continue
        codes = np.asarray(row['power_codes'], dtype=np.uint8)
        if len(codes) != bins:
            raise ValueError('Inconsistent FFT size')
        total += codes
        maximum = np.maximum(maximum, codes)
        second = int(row['sample_index'] / fs)
        if second not in buckets:
            buckets[second] = np.zeros(bins, dtype=np.float64)
        buckets[second] += codes
        bucket_counts[second] += 1
        frames += 1
        floor += int(np.count_nonzero(codes == 0))
        ceiling += int(np.count_nonzero(codes == 255))
        gains[row['gain_raw']] += 1
if not frames:
    raise ValueError('No measured spectrum frames')
if frames != summary['frames']:
    raise ValueError('Frame count differs from capture summary')
args.out.mkdir(parents=True, exist_ok=False)
frequency = np.fft.fftshift(np.fft.fftfreq(bins, 1 / fs))
seconds = sorted(buckets)
waterfall = np.full((max(seconds) + 1, bins), np.nan)
for second in seconds:
    waterfall[second] = np.fft.fftshift(buckets[second] / bucket_counts[second])
with (args.out / 'mean-and-max-codes.csv').open('w') as output:
    writer = csv.writer(output)
    writer.writerow(['baseband_hz', 'mean_power_code', 'max_power_code'])
    writer.writerows(zip(frequency, np.fft.fftshift(total / frames), np.fft.fftshift(maximum)))
with (args.out / 'waterfall-mean-codes.csv').open('w') as output:
    writer = csv.writer(output)
    writer.writerow(['device_second', 'frames', *frequency])
    for second in range(len(waterfall)):
        writer.writerow([second, bucket_counts[second], *waterfall[second]])
analysis = {'input': str(args.capture), 'frames': frames, 'fft_bins': bins, 'sample_rate_hz': fs,
            'capture_pass': summary['pass'], 'gain_raw_counts': dict(gains),
            'quantized_floor_fraction': floor / (frames * bins), 'code_255_fraction': ceiling / (frames * bins),
            'units': 'Raw SPC1 power codes 0..255, uncalibrated; no dBm or dBFS conversion',
            'aggregation': 'Arithmetic mean of codes; waterfall buckets use frame start sample_index/fs, 1 second each',
            'rf_axis_sign_verified': False, 'numpy': np.__version__, 'matplotlib': matplotlib.__version__}
if telemetry:
    analysis['coverage_per_mille'] = {k: float(fn([x['coverage_per_mille'] for x in telemetry]))
                                    for k, fn in [('min', min), ('mean', np.mean), ('max', max)]}
(args.out / 'analysis.json').write_text(json.dumps(analysis, indent=2) + '\n')
fig, axes = plt.subplots(3, 1, figsize=(12, 11), constrained_layout=True)
fig.suptitle(f'ESP32-S3 · реальные SPC1 · {fs / 1e6:g} MS/s · {bins} FFT bins · {frames:,} кадров')
axes[0].plot(frequency / 1e6, np.fft.fftshift(total / frames), label='Средний код')
axes[0].plot(frequency / 1e6, np.fft.fftshift(maximum), label='Максимальный код', alpha=.6)
axes[0].set(xlabel='Baseband, MHz; знак RF-оси не проверен', ylabel='Код мощности, 0…255')
axes[0].legend()
im = axes[1].imshow(waterfall, aspect='auto', origin='lower', interpolation='nearest',
                    extent=[-fs / 2e6, fs / 2e6, 0, len(waterfall)], vmin=0, vmax=255, cmap='viridis')
axes[1].set(xlabel='Baseband, MHz', ylabel='Время устройства, s', title='Средний код по кадрам в интервалах 1 s')
axes[1].set_ylim(0, summary['end']['elapsed_us'] / 1e6)
fig.colorbar(im, ax=axes[1], label='Код мощности; не dBm')
if telemetry:
    t = [x['host_seconds'] for x in telemetry]
    for key, label in [('coverage_per_mille', 'Доля обработанных отсчётов'), ('core0_per_mille', 'Обработка CPU0'), ('core1_per_mille', 'Обработка CPU1')]:
        axes[2].plot(t, [x[key] / 10 for x in telemetry], label=label, linewidth=.8)
    axes[2].legend()
axes[2].set(xlabel='Время получения на хосте, s', ylabel='%', ylim=(0, 105), title='Телеметрия SPS1; нагрузка обработки, не общая загрузка ОС')
axes[0].grid(True, alpha=.25)
axes[2].grid(True, alpha=.25)
fig.savefig(args.out / 'spectrum-overview.png', dpi=150)
fig.savefig(args.out / 'spectrum-overview.svg')
plt.close(fig)
print(json.dumps(analysis, indent=2))
