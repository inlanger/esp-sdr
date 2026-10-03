#!/usr/bin/env python3
"""Re-decode a saved SPEC stream and compare every field with its JSONL export."""
import argparse
import hashlib
import json
from pathlib import Path

from esp_sdr.spectrum import Audit, Decoder

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('capture', type=Path)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
summary = json.loads((args.capture / 'summary.json').read_text())
decoder = Decoder()
audit = Audit(summary['bins'], summary['detector'])
digest = hashlib.sha256()
matched = 0
with (args.capture / 'stream.bin').open('rb') as source, (args.capture / 'records.jsonl').open() as records:
    while chunk := source.read(65536):
        digest.update(chunk)
        for kind, record, offset in decoder.feed(chunk):
            exported = json.loads(next(records))
            if (exported['kind'] != kind or exported['stream_offset'] != offset
                    or any(exported[k] != v for k, v in record.items())):
                raise ValueError(f'JSONL differs from raw stream at offset {offset}')
            audit.add(kind, record)
            matched += 1
    if next(records, None) is not None or decoder.buffer:
        raise ValueError('Extra JSONL records or unconsumed stream bytes')
replayed = audit.summary(decoder)
if any(summary[k] != v for k, v in replayed.items()):
    raise ValueError('Replayed audit differs from capture summary')
result = {'raw_sha256': digest.hexdigest(), 'records_matched': matched,
          'matches_saved_export': True, 'audit': replayed}
with args.out.open('x') as output:
    json.dump(result, output, indent=2)
    output.write('\n')
print(json.dumps({'matches_saved_export': True, 'records_matched': matched,
                  'capture_integrity_pass': replayed['integrity_pass']}))
