#!/usr/bin/env python3
"""Record the pinned S3 IQS protocol at 16 MS/s, decimation 64/128, signed IQ8/16.

Wire layout: docs/iq-stream.md and main/common/ring_capture.c.
No RF source is assumed. Frame offsets preserve gaps in the derived iq.cs8/16 file.
"""
import argparse
import csv
import hashlib
from pathlib import Path
import sys
import time

from capture import Receiver, save, utc
from esp_sdr.iq import Stream, FIELDS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--decimation', type=int, choices=(64, 128), required=True)
    parser.add_argument('--seconds', type=float, required=True)
    parser.add_argument('--mode', type=int, choices=(0, 2), default=0)
    parser.add_argument('--bits', type=int, choices=(8, 16), default=8)
    parser.add_argument('--shift', type=int, choices=range(25))
    parser.add_argument('--frequency', type=int, default=2442)
    parser.add_argument('--experimental-tuning', action='store_true',
                        help='Allow integer MHz tuning within advertised RANGE; RF reception is not guaranteed')
    parser.add_argument('--bandwidth', type=int, default=13)
    parser.add_argument('--gain', type=int, default=40)
    args = parser.parse_args()
    if args.shift is None:
        args.shift = 7 if args.bits == 8 else 0
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    args.out.mkdir(parents=True, exist_ok=False)
    save(args.out / 'invocation.json', {'utc': utc(), 'argv': sys.argv,
         'collector_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    receiver = None
    decoder = Stream(args.decimation, args.shift, args.bits)
    command = f'IQS 0 {args.decimation} {args.bits} 6 {args.shift} {args.mode}'
    result = {'utc_start': utc(), 'command': command, 'requested_seconds': args.seconds,
              'input_rate_hz': 16000000, 'output_rate_hz': 16000000 // args.decimation,
              'bits': args.bits, 'shift': args.shift, 'mode': args.mode, 'frequency_mhz': args.frequency,
              'bandwidth_mhz': args.bandwidth, 'gain_index': args.gain,
              'command_access_after': False, 'stop_monotonic_ns': None, 'error': None}
    stream_hash, iq_hash = hashlib.sha256(), hashlib.sha256()
    first_frame = stop = None
    try:
        receiver = Receiver(args.port, args.out)
        receiver.identify(args.out)
        if receiver.transport != 'USB':
            raise RuntimeError('IQS requires native USB')
        save(args.out / 'controls.json', receiver.configure(args))
        receiver.ok('DUAL 1')
        receiver.streaming = True
        receiver.write((command + '\n').encode('ascii'))
        result['start_reply'] = receiver.line()
        if result['start_reply'] != f'IQS 16000000 {args.decimation} {args.bits} {args.shift} {args.mode} {args.frequency}':
            raise RuntimeError(f'Unexpected IQS start: {result["start_reply"]}')
        last_data = last_progress = time.monotonic()
        with (args.out / 'stream.bin').open('xb') as raw, (args.out / f'iq.cs{args.bits}').open('xb') as iq, \
                (args.out / 'frames.csv').open('w', newline='') as index:
            columns = ['host_monotonic_ns', 'utc_received', 'stream_offset', 'iq_byte_offset'] + FIELDS
            writer = csv.DictWriter(index, fieldnames=columns)
            writer.writeheader()
            while decoder.end is None:
                now = time.monotonic()
                if stop is None and ((first_frame is not None and now - first_frame >= args.seconds)
                                     or now - last_data >= 5):
                    receiver.write(b'\n')
                    stop = now
                    result['stop_monotonic_ns'] = time.monotonic_ns()
                    if now - last_data >= 5:
                        result['error'] = 'No stream data for 5 seconds; stop requested'
                if stop is not None and now - stop > 10:
                    raise TimeoutError('No IQSEND; no ordinary commands sent')
                # Batch native USB reads: a one-byte minimum wakes the host for
                # tiny chunks at 1 MB/s. The serial timeout still bounds the tail.
                data = receiver.read(min(65536, max(4096, receiver.port.in_waiting)))
                if data:
                    last_data = time.monotonic()
                    raw.write(data)
                    stream_hash.update(data)
                    timestamp, mono = utc(), time.monotonic_ns()
                    for record, payload in decoder.feed(data):
                        if first_frame is None:
                            first_frame = time.monotonic()
                            result['first_frame_monotonic_ns'] = mono
                        iq.write(payload)
                        iq_hash.update(payload)
                        writer.writerow({'host_monotonic_ns': mono, 'utc_received': timestamp, **record})
                elif now - last_data >= .25:
                    decoder.recover_end()
                if now - last_progress >= 30:
                    print(f'{utc()} frames={decoder.frames} samples={decoder.samples} '
                          f'CRC={decoder.crc_errors} gaps={decoder.gap_samples}', flush=True)
                    save(args.out / 'progress.json', decoder.summary() | {'utc': utc()})
                    last_progress = now
        receiver.streaming = False
        result['command_access_after'] = receiver.command('INFO') == receiver.identity
        receiver.ok('RELEASE')
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
    finally:
        if receiver:
            receiver.close()
        result.update(decoder.summary())
        result.update(utc_end=utc(), stream_sha256=stream_hash.hexdigest(), iq_sha256=iq_hash.hexdigest())
        result['host_seconds_first_frame_to_stop'] = stop - first_frame if stop and first_frame else None
        result['duration_pass'] = bool(decoder.end and stop and first_frame
            and stop - first_frame >= args.seconds and decoder.end['elapsed_us'] >= args.seconds * 1e6
            and decoder.end['stopped_by_host'] == 1)
        result['pass'] = bool(result['integrity_pass'] and result['duration_pass']
                              and result['command_access_after'] and not result['error'])
        save(args.out / 'summary.json', result)
    print(result, flush=True)
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
