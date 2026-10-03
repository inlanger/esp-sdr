#!/usr/bin/env python3
"""Record an observed source switch; does not control or transmit RF."""
import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--file', type=Path, required=True)
parser.add_argument('--cycle', type=int, choices=(1, 2, 3), required=True)
parser.add_argument('--state', choices=('off', 'on'), required=True)
parser.add_argument('--note', required=True, help='How the actual state change was confirmed; timing uncertainty')
args = parser.parse_args()
new = not args.file.exists() or args.file.stat().st_size == 0
with args.file.open('a') as output:
    writer = csv.writer(output)
    if new:
        writer.writerow(['utc_observed', 'host_monotonic_ns', 'cycle', 'source_state', 'note'])
    writer.writerow([datetime.now(timezone.utc).isoformat(), time.monotonic_ns(), args.cycle, args.state, args.note])
