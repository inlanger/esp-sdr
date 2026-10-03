#!/usr/bin/env python3
"""Run the unchanged upstream checker; preserve stderr and failure as JSON."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', required=True)
parser.add_argument('--identity', type=Path, required=True, help='Fresh identity.txt from capture.py')
parser.add_argument('--out', type=Path, required=True, help='New output directory')
args = parser.parse_args()
lines = args.identity.read_text().splitlines()
caps = next(line for line in lines if line.startswith('CAPS ')).split()[1:]
info = json.loads(next(line for line in lines if line.startswith('SPECINFO '))[9:])
checker = Path(__file__).resolve().parents[1] / 'check_spectrum.py'
command = [sys.executable, str(checker), '--port', args.port, '--milliseconds', '3000']
if 'SPECSTAT' in caps:
    command.append('--stats')
args.out.mkdir(parents=True, exist_ok=False)
start = datetime.now(timezone.utc).isoformat()
with (args.out / 'upstream.stdout').open('w') as stdout, (args.out / 'upstream.stderr').open('w') as stderr:
    result = subprocess.run(command, stdout=stdout, stderr=stderr)
report = {'utc_start': start, 'utc_end': datetime.now(timezone.utc).isoformat(),
          'command': command, 'returncode': result.returncode, 'advertised_profiles': info['profiles'],
          'pass': False, 'upstream': None}
if result.returncode == 0:
    report['upstream'] = json.loads((args.out / 'upstream.stdout').read_text())
    report['pass'] = True
else:
    report['error'] = (args.out / 'upstream.stderr').read_text()
    report['profile_results'] = 'Upstream aborted; completed runs were not emitted. Do not mark any unreported profile passed.'
    report['recovery'] = 'Reconnect/reset and confirm command boundary before further commands.'
(args.out / 'profile-check.json').write_text(json.dumps(report, indent=2) + '\n')
sys.exit(0 if report['pass'] else 1)
