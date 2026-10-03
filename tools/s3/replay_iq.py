#!/usr/bin/env python3
"""Recompute a snapshot FFT from saved CRC-verified IQ8; no synthetic input."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import zlib

import numpy as np
from esp_sdr.spectrum import decode_iq8

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('capture', type=Path, help='NN.bin saved by capture.py iq')
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
metadata = json.loads(args.capture.with_suffix('.json').read_text())
raw = args.capture.read_bytes()
assert metadata['crc_valid'] and metadata['bits_per_component'] == 8
assert len(raw) == metadata['samples'] * 2
assert hashlib.sha256(raw).hexdigest() == metadata['sha256']
assert zlib.crc32(raw) == int(metadata['header'].split()[2], 16)
iq = np.array(decode_iq8(raw), dtype=np.float64)
signal = iq[:, 0] + 1j * iq[:, 1]
window = np.hanning(len(signal))
# Documented host convention: whole snapshot, mean removed, symmetric Hann,
# unnormalized FFT / sum(window). Power remains in squared ADC-code units.
power = np.abs(np.fft.fft((signal - signal.mean()) * window) / window.sum()) ** 2
baseband = np.fft.fftfreq(len(signal), 1 / metadata['sample_rate_hz'])
with args.out.open('x') as output:
    writer = csv.writer(output)
    writer.writerow(['bin_natural_order', 'baseband_hz', 'power_adc_code_squared'])
    writer.writerows(zip(range(len(signal)), baseband, power))
print(args.out)
