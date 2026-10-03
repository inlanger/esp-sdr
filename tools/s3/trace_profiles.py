#!/usr/bin/env python3
"""Run the unchanged upstream check() with raw serial evidence on failure."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import traceback

from capture import Receiver, save, utc

UPSTREAM = Path(__file__).resolve().parents[1] / 'check_spectrum.py'
sys.path.insert(0, str(UPSTREAM.parent))
from check_spectrum import check


class RecordedPort:
    def __init__(self, receiver):
        self.receiver = receiver

    @property
    def timeout(self):
        return self.receiver.port.timeout

    @timeout.setter
    def timeout(self, value):
        self.receiver.port.timeout = value

    def write(self, data):
        self.receiver.write(data)
        return len(data)

    def read(self, count):
        return self.receiver.read(count)

    def readline(self):
        data = self.receiver.port.readline()
        self.receiver.rx.write(data)
        self.receiver.log.write(json.dumps({'utc': utc(), 'rx_line': data.decode('ascii', errors='replace').strip()}) + '\n')
        return data

    def reset_input_buffer(self):
        data = self.receiver.read(self.receiver.port.in_waiting)
        self.receiver.log.write(json.dumps({'utc': utc(), 'drained_bytes': len(data)}) + '\n')


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=False)
save(args.out / 'invocation.json', {'utc': utc(), 'argv': sys.argv})
result = {'utc_start': utc(), 'pass': False, 'milliseconds': 3000,
          'upstream_sha256': hashlib.sha256(UPSTREAM.read_bytes()).hexdigest()}
r = Receiver(args.port, args.out)
try:
    result['identity'] = r.identify(args.out)
    result['stats_requested'] = 'SPECSTAT' in r.caps
    result['upstream'] = check(RecordedPort(r), 3000, result['stats_requested'])
    result['pass'] = True
except Exception as error:
    result['error'] = f'{type(error).__name__}: {error}'
    (args.out / 'upstream.stderr').write_text(traceback.format_exc())
    for frame, _ in traceback.walk_tb(error.__traceback__):
        if frame.f_code.co_name == 'check' and Path(frame.f_code.co_filename).resolve() == UPSTREAM.resolve():
            local = frame.f_locals
            result['partial_upstream'] = local.get('result')
            result['failed_profile'] = local.get('profile')
            result['failed_detector'] = local.get('detector')
            result['failed_run_partial'] = local.get('run')
            result['previous_sample_index'] = local.get('previous')
            result['current_sample_index'] = local.get('index')
            result['current_ffts'] = local.get('ffts')
            if isinstance(local.get('frame'), bytes):
                (args.out / 'failed-frame.bin').write_bytes(local['frame'])
    result['boundary_after_failure'] = 'Not confirmed; no further commands sent. Reopen/reset before next test.'
finally:
    r.close()
    result['utc_end'] = utc()
    save(args.out / 'profile-check.json', result)
print(json.dumps({k: v for k, v in result.items() if k not in ('upstream', 'partial_upstream', 'identity')}, indent=2))
raise SystemExit(0 if result['pass'] else 1)
