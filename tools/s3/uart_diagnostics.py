#!/usr/bin/env python3
"""Additional real UART checks; these do not replace the native-USB SPEC tests.

Protocol source: pinned receiver.c, ring_capture.h, ring_io.h and s3_ring.py.
Run through darwin_baud64.py on the tested WCH/AppleUSBACM host.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import zlib

from capture import Receiver, save, utc

FIELDS = 'status detail units pairs elapsed_us late_max work_max frames drops abandoned ffts stopped_by_host'.split()
RATES = {6: 16000000, 1: 40000000, 0: 80000000}


def report(text, tag):
    words = text.split()
    if len(words) != 13 or words[0] != tag:
        raise RuntimeError(f'Unexpected end report: {text!r}')
    return dict(zip(FIELDS, map(int, words[1:])))


def payload(r, count):
    data = bytearray()
    deadline = time.monotonic() + 10
    while len(data) < count and time.monotonic() < deadline:
        data.extend(r.read(min(4096, count - len(data))))
    return bytes(data)


def iq10(r, out, samples, rate, settings):
    started = utc()
    command = f'CAP20 {samples} {rate}'
    header = r.command(command)
    out.with_suffix('.header.txt').write_text(header + '\n')
    words = header.split()
    if len(words) != 4 or words[0] != 'DATA':
        raise RuntimeError(header)
    r.streaming = True
    raw = payload(r, (samples * 20 + 7) // 8)
    out.with_suffix('.bin').write_bytes(raw)
    valid = int(words[1]) == samples and len(raw) == (samples * 20 + 7) // 8 and zlib.crc32(raw) == int(words[2], 16)
    save(out.with_suffix('.json'), {'utc_start': started, 'utc_end': utc(), 'command': command,
         'header': header, 'samples': samples, 'sample_rate_hz': RATES[rate], 'bytes': len(raw),
         'bits_per_component': 10, 'packing': 'Little-endian contiguous 20-bit pairs: signed I bits 0..9, Q bits 10..19',
         'continuous': False, 'receiver': settings, 'crc_valid': valid,
         'sha256': hashlib.sha256(raw).hexdigest()})
    if not valid:
        raise RuntimeError('IQ10 size/CRC mismatch; stream boundary unconfirmed')
    r.streaming = False


def ring(r, out, milliseconds, rate, stop_after=None):
    started, start = utc(), time.monotonic()
    command = f'RING {milliseconds} {rate}'
    r.streaming = True
    r.write((command + '\n').encode())
    data, stopped = bytearray(), False
    while time.monotonic() - start < milliseconds / 1000 + 10:
        if stop_after is not None and not stopped and time.monotonic() - start >= stop_after:
            r.write(b'\n')
            stopped = True
        data.extend(r.read(1))
        if data.endswith(b'\n'):
            break
    text = data.decode('ascii').strip()
    out.with_suffix('.txt').write_text(text + '\n')
    end = report(text, 'RING')
    r.streaming = False
    result = {'utc_start': started, 'utc_end': utc(), 'command': command,
              'host_elapsed_seconds': time.monotonic() - start, 'sample_rate_hz': RATES[rate],
              'host_stop_requested': stopped, 'report': end, 'spectra_transferred': 0,
              'note': 'Bank rotation statistics only; no continuous IQ or spectrum sent to host'}
    result['identity_after'] = r.command('INFO')
    result['pass'] = end['status'] == 0 and result['identity_after'] == r.identity
    result['pass'] &= bool(end['stopped_by_host']) == stopped
    if not stopped:
        result['pass'] &= end['elapsed_us'] >= milliseconds * 1000
    save(out.with_suffix('.json'), result)
    print(json.dumps(result), flush=True)
    if not result['pass']:
        raise RuntimeError(f'Ring check failed: {out}')


def ringcap(r, out, rate, settings):
    started = utc()
    header = r.command(f'RINGCAP 3 {rate}')
    out.with_suffix('.header.txt').write_text(header + '\n')
    parts = header.split()
    if len(parts) != 7 or parts[0] != 'RINGDATA':
        raise RuntimeError(header)
    units, fs, n0, n1, n2 = map(int, parts[1:6])
    r.streaming = True
    raw = payload(r, 4 * (n0 + n1 + n2))
    out.with_suffix('.bin').write_bytes(raw)
    valid = units == 3 and fs == RATES[rate] and len(raw) == 4 * (n0 + n1 + n2) and zlib.crc32(raw) == int(parts[6], 16)
    if not valid:
        raise RuntimeError('RINGCAP size/CRC mismatch; stream boundary unconfirmed')
    end_text = r.line()
    end = report(end_text, 'RINGCAP')
    r.streaming = False
    save(out.with_suffix('.json'), {'utc_start': started, 'utc_end': utc(), 'header': header,
         'end_text': end_text, 'report': end, 'sample_rate_hz': fs, 'bank_samples': [n0, n1, n2],
         'receiver': settings,
         'packing': 'uint32 LE words: signed I bits 0..9, Q bits 10..19, gain above bit 19',
         'bytes': len(raw), 'crc_valid': valid, 'sha256': hashlib.sha256(raw).hexdigest(),
         'phase_continuity_verified': False, 'reason': 'No controlled CW source'})
    if end['status'] or end['pairs'] != n0 + n1 + n2:
        raise RuntimeError(f'RINGCAP end failure: {end}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('controls', 'ring600'))
    parser.add_argument('--port', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    save(args.out / 'invocation.json', {'utc': utc(), 'argv': sys.argv})
    status = {'pass': False, 'completed_cycles': 0}
    r = Receiver(args.port, args.out)
    try:
        r.identify(args.out)
        if r.transport != 'UART':
            raise RuntimeError('These additional checks describe the UART experiment')
        if args.mode == 'controls':
            for i in range(20):
                settings = {'frequency': (2412, 2437, 2462)[i % 3],
                            'bandwidth': (13, 20, 40)[i % 3], 'gain': (40, 50, 60)[i % 3]}
                rate = (6, 1, 0)[i % 3]
                save(args.out / f'{i:02d}-controls.json', {'requested': settings, 'answers': r.configure(SimpleNamespace(**settings))})
                ring(r, args.out / f'{i:02d}-stop', 1000, rate, stop_after=.2)
                iq10(r, args.out / f'{i:02d}-iq10', (256, 257, 16380)[i % 3], rate, settings)
                status['completed_cycles'] += 1
                save(args.out / 'result.json', status)
            for rate in RATES:
                ringcap(r, args.out / f'ringcap-rate{rate}', rate, settings)
        else:
            settings = {'frequency': 2442, 'bandwidth': 13, 'gain': 60}
            save(args.out / 'controls.json', {'requested': settings, 'answers': r.configure(SimpleNamespace(**settings))})
            ring(r, args.out / 'ring600', 600000, 6)
        status['identity_after'] = r.command('INFO')
        r.ok('RELEASE')
        status['pass'] = True
    except Exception as error:
        status['error'] = f'{type(error).__name__}: {error}'
    finally:
        r.close()
        save(args.out / 'result.json', status)
    print(json.dumps(status), flush=True)
    return 0 if status['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
