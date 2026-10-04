#!/usr/bin/env python3
"""Collect S3 TRIG raw-I/Q events; each arm has a separate sample clock.

Fixed 16 MS/s, 64-pair variance windows; the reply advertises the stride. Threshold zero is
forced diagnostic capture, not RF detection. No continuous archive is implied.
"""
import argparse
import hashlib
from pathlib import Path
import struct
import sys
import time
import zlib

from capture import Receiver, save, utc
from esp_sdr.wire import END_FIELDS

META_FIELDS = ('triggered capture_start_us capture_end_us saved_first_index '
               'trigger_window_index power_numerator examined_windows '
               'skipped_deadline_windows max_slice_cycles last_below_index').split()


def report(line, tag, fields):
    words = line.split()
    if len(words) != len(fields) + 1 or words[0] != tag or not all(w.isdigit() for w in words[1:]):
        raise ValueError(f'Invalid {tag}: {line!r}')
    return dict(zip(fields, map(int, words[1:])))


def wait_line(receiver, deadline):
    raw = bytearray()
    while time.monotonic() < deadline and len(raw) < 4096:
        raw.extend(receiver.read(1))
        if raw.endswith(b'\n'):
            return raw.decode('ascii').strip()
    raise TimeoutError(f'Incomplete TRIG reply ({len(raw)} bytes); boundary unconfirmed')


def power_numerator(raw, offset):
    pairs = struct.unpack_from('<64I', raw, offset * 4)
    i = [((w & 1023) ^ 512) - 512 for w in pairs]
    q = [(((w >> 10) & 1023) ^ 512) - 512 for w in pairs]
    return 64 * sum(a*a + b*b for a, b in zip(i, q)) - sum(i)**2 - sum(q)**2


def verify_capture(record, raw):
    """Recheck saved metadata and exact integer detector arithmetic offline."""
    meta, end = record['meta'], record['end']
    profile = report(record['start_reply'], 'TRIG', ('rate', 'window', 'stride', 'threshold'))
    if (profile['rate'] != 16000000 or profile['window'] != 64
            or profile['stride'] not in (128, 256) or profile['threshold'] != record['threshold']):
        raise ValueError('Unsupported TRIG profile')
    stride = profile['stride']
    if end['status'] or meta['triggered'] not in (0, 1):
        raise ValueError(f'TRIG failure: {end}')
    if meta['capture_end_us'] < meta['capture_start_us']:
        raise ValueError('Device capture timestamps reversed')
    examined = meta['examined_windows'] * 64
    if examined > end['pairs']:
        raise ValueError('Examined pairs exceed captured pairs')
    result = {'examined_pairs': examined, 'unexamined_pairs': end['pairs'] - examined,
              'examined_fraction': examined / end['pairs'] if end['pairs'] else None,
              'skipped_deadline_pairs': meta['skipped_deadline_windows'] * 64,
              'raw_bytes': len(raw), 'raw_sha256': hashlib.sha256(raw).hexdigest()}
    if not meta['triggered']:
        if raw or record.get('header') or meta['saved_first_index'] or meta['trigger_window_index']:
            raise ValueError('Unexpected payload/index on arm without trigger')
        if not end['stopped_by_host'] and end['elapsed_us'] < record['wait_ms'] * 1000:
            raise ValueError('Timeout ended before requested wait')
        result['outcome'] = 'host_stop' if end['stopped_by_host'] else 'timeout_no_event'
        return result
    words = record['header'].split()
    if len(words) != 7 or words[0] != 'RINGDATA':
        raise ValueError('Invalid RINGDATA header')
    units, fs, *counts = map(int, words[1:6])
    count = sum(counts)
    if units != 3 or fs != 16000000 or any(n <= 0 or n > 16384 for n in counts):
        raise ValueError('Invalid raw bank lengths/rate')
    if len(raw) != count * 4 or zlib.crc32(raw) != int(words[6], 16):
        raise ValueError('Raw payload length/CRC mismatch')
    first, hit = meta['saved_first_index'], meta['trigger_window_index']
    pre = hit - first
    if (not counts[0] <= pre or pre + 64 > counts[0] + counts[1]
            or first + count != end['pairs'] or end['units'] < 3 or (pre - counts[0]) % stride):
        raise ValueError('Trigger window outside middle bank or saved interval outside capture')
    numerator = power_numerator(raw, pre)
    if numerator != meta['power_numerator'] or numerator < record['threshold'] * 4096:
        raise ValueError('Raw trigger-window arithmetic does not match firmware')
    if record['threshold']:
        below = meta['last_below_index']
        if below < first or below + 64 > hit:
            raise ValueError('Missing earlier below-threshold window in payload')
        result['below_power_numerator'] = power_numerator(raw, below - first)
        if result['below_power_numerator'] >= record['threshold'] * 4096:
            raise ValueError('Saved earlier window is not below threshold')
        result['crossing_interval_pairs'] = hit - below
    elif meta['last_below_index'] != 18446744073709551615:
        raise ValueError('Forced diagnostic unexpectedly reports a below-threshold window')
    result.update(outcome='forced_diagnostic' if record['threshold'] == 0 else 'threshold_crossing',
                  crc_valid=True, detector_replay_matches=True, bank_samples=counts,
                  saved_pairs=count, saved_first_index=first, saved_end_index_exclusive=first + count,
                  pre_window_pairs=pre, post_window_pairs=count - pre - 64,
                  bank_spans=[[first + sum(counts[:n]), first + sum(counts[:n + 1])] for n in range(3)],
                  window_ac_power_adc_code_squared=numerator / 4096,
                  packing='LE uint32: signed I[0:9], Q[10:19], gain_raw[20:27]',
                  phase_continuity_verified=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--frequency', type=int, default=2467)
    parser.add_argument('--bandwidth', type=int, default=13)
    parser.add_argument('--gain', type=int, default=40)
    parser.add_argument('--threshold', type=int, required=True, help='Variance in ADC-code squared; 0 forces a diagnostic event')
    parser.add_argument('--wait', type=float, default=3, help='Maximum seconds armed per capture')
    parser.add_argument('--count', type=int, default=1)
    args = parser.parse_args()
    milliseconds = round(args.wait * 1000)
    if not 0 <= args.threshold <= 524288 or not 1 <= milliseconds <= 60000 or args.count < 1:
        parser.error('Require threshold 0..524288, wait 0.001..60 seconds, count >= 1')
    args.out.mkdir(parents=True, exist_ok=False)
    save(args.out / 'invocation.json', {'utc': utc(), 'argv': sys.argv,
         'collector_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    receiver, previous_end = None, None
    summary = {'pass': False, 'captures': [], 'error': None, 'continuous_archive': False}
    try:
        receiver = Receiver(args.port, args.out)
        receiver.identify(args.out)
        if receiver.transport != 'USB' or 'TRIG' not in receiver.caps:
            raise ValueError('TRIG requires native USB and advertised TRIG capability')
        receiver.ok('FOFS 0')
        save(args.out / 'controls.json', receiver.configure(args))
        for index in range(args.count):
            prefix = args.out / f'{index:03d}'
            record = {'utc_start': utc(), 'threshold': args.threshold, 'wait_ms': milliseconds,
                      'frequency_mhz': args.frequency, 'bandwidth_mhz': args.bandwidth,
                      'gain_index': args.gain, 'fofs_pll_khz': 0, 'pass': False, 'error': None}
            raw = bytearray()
            try:
                command = f'TRIG {milliseconds} {args.threshold}'
                record.update(command=command, command_monotonic_ns=time.monotonic_ns())
                receiver.streaming = True
                receiver.write((command + '\n').encode('ascii'))
                record['start_reply'] = receiver.line()
                if record['start_reply'] not in (f'TRIG 16000000 64 {stride} {args.threshold}' for stride in (128, 256)):
                    raise ValueError(f'Unexpected start: {record["start_reply"]}')
                record['meta_line'] = wait_line(receiver, time.monotonic() + args.wait + 10)
                record['meta'] = report(record['meta_line'], 'TRIGMETA', META_FIELDS)
                line = receiver.line()
                if line.startswith('RINGDATA '):
                    record['header'] = line
                    parts = line.split()
                    if len(parts) != 7:
                        raise ValueError('Invalid RINGDATA header')
                    counts = list(map(int, parts[3:6]))
                    if any(n <= 0 or n > 16384 for n in counts):
                        raise ValueError('Invalid bank payload bounds')
                    deadline = time.monotonic() + 10
                    with prefix.with_suffix('.bin').open('xb') as output:
                        while len(raw) < 4 * sum(counts) and time.monotonic() < deadline:
                            data = receiver.read(min(65536, 4 * sum(counts) - len(raw)))
                            output.write(data)
                            raw.extend(data)
                    if len(raw) != 4 * sum(counts):
                        raise TimeoutError('Incomplete raw payload; boundary unconfirmed')
                    line = receiver.line()
                record['end_line'] = line
                record['end'] = report(line, 'TRIGEND', END_FIELDS)
                record['end_received_monotonic_ns'] = time.monotonic_ns()
                record.update(verify_capture(record, raw))
                receiver.streaming = False
                record['command_access_after'] = receiver.command('INFO') == receiver.identity
                if not record['command_access_after']:
                    raise ValueError('Receiver identity changed after arm')
                start, end = (record['meta'][f'capture_{which}_us'] for which in ('start', 'end'))
                record['capture_gap_from_previous_us'] = start - previous_end if previous_end is not None else None
                previous_end = end
                record['pass'] = True
            except Exception as error:
                record['error'] = f'{type(error).__name__}: {error}'
                raise
            finally:
                record['utc_end'] = utc()
                save(prefix.with_suffix('.json'), record)
                summary['captures'].append(record)
                save(args.out / 'summary.json', summary)
            print(f'{utc()} arm={index} outcome={record["outcome"]}', flush=True)
        receiver.ok('RELEASE')
        summary['pass'] = True
    except Exception as error:
        summary['error'] = f'{type(error).__name__}: {error}'
    finally:
        if receiver:
            receiver.close()
        save(args.out / 'summary.json', summary)
    return 0 if summary['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
