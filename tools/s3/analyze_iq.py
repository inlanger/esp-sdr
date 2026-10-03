#!/usr/bin/env python3
"""Plot measured IQ8 snapshots after rechecking their size, SHA256 and CRC."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import zlib

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory', type=Path)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
files = sorted(args.directory.glob('[0-9][0-9].bin'))
if not files:
    parser.error('No measured snapshots found')
metadata, samples, powers, stats = [], [], [], []
for path in files:
    record = json.loads(path.with_suffix('.json').read_text())
    raw = path.read_bytes()
    assert record['crc_valid'] and record['bits_per_component'] == 8
    assert len(raw) == record['samples'] * 2
    assert hashlib.sha256(raw).hexdigest() == record['sha256']
    assert zlib.crc32(raw) == int(record['header'].split()[2], 16)
    iq = np.frombuffer(raw, dtype=np.int8).reshape(-1, 2).astype(np.float64)
    signal = iq[:, 0] + 1j * iq[:, 1]
    window = np.hanning(len(signal))
    powers.append(np.abs(np.fft.fft((signal - signal.mean()) * window) / window.sum()) ** 2)
    samples.append(iq)
    metadata.append(record)
    stats.append({'capture': path.stem, 'mean_i': float(iq[:, 0].mean()),
                  'mean_q': float(iq[:, 1].mean()),
                  'rms_complex': float(np.sqrt(np.mean(np.abs(signal) ** 2))),
                  'min_component': float(iq.min()), 'max_component': float(iq.max()),
                  'rail_components': int(np.count_nonzero((iq == -128) | (iq == 127)))})
first = metadata[0]
assert all(m['sample_rate_hz'] == first['sample_rate_hz'] and m['samples'] == first['samples']
           and m['receiver'] == first['receiver'] for m in metadata)
args.out.mkdir(parents=True, exist_ok=False)
frequency = np.fft.fftshift(np.fft.fftfreq(first['samples'], 1 / first['sample_rate_hz']))
mean_power = np.fft.fftshift(np.mean(powers, axis=0))
with (args.out / 'mean-spectrum.csv').open('w') as output:
    writer = csv.writer(output)
    writer.writerow(['baseband_hz', 'mean_power_adc_code_squared'])
    writer.writerows(zip(frequency, mean_power))
analysis = {'captures': len(files), 'continuous': False, 'receiver': first['receiver'],
            'sample_rate_hz': first['sample_rate_hz'], 'samples_per_capture': first['samples'],
            'fft': 'Per-snapshot mean removal, symmetric Hann; FFT divided by window sum; mean power over snapshots',
            'units': 'ADC code squared; uncalibrated; not dBm',
            'rf_axis_sign_verified': False, 'statistics': stats,
            'numpy': np.__version__, 'matplotlib': matplotlib.__version__}
(args.out / 'analysis.json').write_text(json.dumps(analysis, indent=2) + '\n')
fig, axes = plt.subplots(3, 1, figsize=(11, 10), constrained_layout=True)
fig.suptitle(f"ESP32-S3: {len(files)} отдельных I/Q snapshots, {first['sample_rate_hz'] / 1e6:g} MS/s\n"
             f"LO {first['receiver']['frequency']} МГц · непрерывность между snapshots отсутствует")
for column, label in enumerate(('I', 'Q')):
    axes[0].plot(np.arange(256) / first['sample_rate_hz'] * 1e6, samples[0][:256, column], label=label, linewidth=.8)
axes[0].set(xlabel='Время внутри первого snapshot, мкс', ylabel='Код АЦП', title='Первые 256 комплексных отсчётов')
axes[0].legend()
positive = mean_power > 0
axes[1].plot(frequency[positive] / 1e6, mean_power[positive], linewidth=.7)
axes[1].set(xlabel='Частота baseband, МГц (знак RF-оси не проверен)', ylabel='Мощность, код АЦП²',
            yscale='log', title='Средняя мощность FFT по отдельным snapshots; некалиброванная шкала')
axes[2].plot(range(len(stats)), [s['rms_complex'] for s in stats], marker='o')
axes[2].set(xlabel='Номер отдельного snapshot', ylabel='RMS комплексного I/Q, код АЦП',
            title=f"Отсчётов на границах IQ8: {sum(s['rail_components'] for s in stats)}; аналоговое насыщение этим не исключается")
for ax in axes:
    ax.grid(True, alpha=.25)
fig.savefig(args.out / 'iq-overview.png', dpi=150)
fig.savefig(args.out / 'iq-overview.svg')
plt.close(fig)
print(json.dumps(analysis, indent=2))
