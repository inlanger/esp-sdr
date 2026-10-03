#!/usr/bin/env python3
"""Receive-only S3 capture. Requires known ESP-SDR firmware and an idle command channel.

Run identity first. On an uncertain stream boundary reconnect/reset before rerunning.
Each --out must be a fresh directory. See README.md for the hardware prerequisites.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import zlib

from esp_sdr.spectrum import Audit, Decoder, decode_iq8


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


class Receiver:
    def __init__(self, port, out):
        import serial
        self.log = (out / 'serial.jsonl').open('w', buffering=1)
        self.rx = (out / 'serial-rx.bin').open('wb')
        self.port = serial.Serial(port=None, baudrate=2000000, timeout=.05, write_timeout=2)
        self.port.dtr = self.port.rts = False
        self.port.port = port
        self.streaming = False
        self.port.open()
        # Opening this WCH/AppleUSBACM port was observed to reset the board.
        # Let it boot before SYNC; the caller must already have an idle receiver.
        time.sleep(2)
        self.port.baudrate = 2000000
        startup = self.read(self.port.in_waiting)
        self.log.write(json.dumps({'utc': utc(), 'startup_bytes': len(startup)}) + '\n')

    def write(self, data):
        self.log.write(json.dumps({'utc': utc(), 'monotonic_ns': time.monotonic_ns(),
                                   'tx_ascii': data.decode('ascii')}) + '\n')
        if self.port.write(data) != len(data):
            raise IOError('Short serial write')

    def read(self, size):
        data = self.port.read(size)
        if data:
            self.rx.write(data)
        return data

    def line(self):
        data = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(data) < 4096:
            data.extend(self.read(1))
            if data.endswith(b'\n'):
                text = data.decode('ascii').strip()
                self.log.write(json.dumps({'utc': utc(), 'rx_line': text}) + '\n')
                return text
        raise TimeoutError(f'Incomplete command reply ({len(data)} bytes)')

    def command(self, text):
        if self.streaming:
            raise RuntimeError('Unconfirmed stream boundary: reconnect before commands')
        self.write((text + '\n').encode('ascii'))
        return self.line()

    def ok(self, text):
        reply = self.command(text)
        if reply != 'OK':
            raise RuntimeError(f'{text}: {reply}')

    def identify(self, out):
        nonce = time.time_ns()
        answer = self.command(f'SYNC {nonce}')
        deadline = time.monotonic() + 5
        while answer != f'SYNC {nonce}':
            # Native USB can deliver the remaining boot log after the initial
            # drain. Require our nonce before issuing any receiver commands.
            if time.monotonic() >= deadline:
                raise RuntimeError('SYNC failed; reconnect/reset; no further commands sent')
            answer = self.line()
        answers = {q: self.command(q) for q in ('INFO', 'CAPS', 'LIMITS?', 'TRANSPORT?', 'SPECINFO?',
                                                'GAIN?', 'LPF?', 'DC?', 'DUAL?')}
        (out / 'identity.txt').write_text('\n'.join(f'> {k}\n{v}' for k, v in answers.items()) + '\n')
        if not answers['INFO'].startswith('S3SDR 6 burst '):
            raise RuntimeError('Expected pinned S3 burst protocol 6')
        self.transport = answers['TRANSPORT?'].split()[1]
        if self.transport not in ('USB', 'UART'):
            raise RuntimeError('Unknown receiver transport')
        self.identity = answers['INFO']
        self.caps = answers['CAPS'].split()[1:]
        self.limits = json.loads(answers['LIMITS?'].removeprefix('LIMITS '))
        self.specinfo = json.loads(answers['SPECINFO?'].removeprefix('SPECINFO '))
        return answers

    def configure(self, args):
        lo, hi, step = self.limits['gain']
        bwlo, bwhi, bwstep, _ = self.limits['bandwidth']
        if not (lo <= args.gain <= hi and (args.gain - lo) % step == 0):
            raise ValueError('Gain outside advertised limits')
        if args.bandwidth != 0 and not (bwlo <= args.bandwidth <= bwhi and (args.bandwidth - bwlo) % bwstep == 0):
            raise ValueError('Bandwidth outside advertised limits')
        if not 2400 <= args.frequency <= 2483:
            raise ValueError('This experiment is restricted to integer MHz in the 2.4 GHz band')
        for command in (f'FREQ {args.frequency}', f'BANDWIDTH {args.bandwidth}', f'GAIN MANUAL {args.gain}'):
            self.ok(command)
        return {q: self.command(q) for q in ('GAIN?', 'LPF?', 'DC?', 'DUAL?')}

    def close(self):
        self.port.close()
        self.rx.close()
        self.log.close()


def iq(receiver, args):
    rates = {0: 80000000, 1: 40000000, 6: 16000000}
    if args.rate not in rates or rates[args.rate] not in receiver.limits['rates'] or 8 not in receiver.limits['bits']:
        raise ValueError('IQ8 rate/format not advertised')
    maximum = int(receiver.identity.split()[-1])
    if not 256 <= args.samples <= maximum:
        raise ValueError(f'Samples must be between 256 and {maximum}')
    records = []
    for index in range(20):
        prefix = args.out / f'{index:02d}'
        command = f'CAP16 {args.samples} {args.rate}'
        started = utc()
        header = receiver.command(command)
        prefix.with_suffix('.header.txt').write_text(header + '\n')
        words = header.split()
        if len(words) != 4 or words[0] != 'DATA':
            raise RuntimeError(f'Unexpected IQ header: {header}')
        count, crc, elapsed = int(words[1]), int(words[2], 16), int(words[3])
        receiver.streaming = True
        raw = bytearray()
        deadline = time.monotonic() + 10
        with prefix.with_suffix('.bin').open('xb') as output:
            while len(raw) < count * 2 and time.monotonic() < deadline:
                data = receiver.read(min(4096, count * 2 - len(raw)))
                raw.extend(data)
                output.write(data)
        valid = len(raw) == count * 2 and count == args.samples and zlib.crc32(raw) == crc
        samples = decode_iq8(raw) if valid else []
        record = {'utc_start': started, 'utc_end': utc(), 'command': command, 'header': header,
                  'sample_rate_hz': rates[args.rate], 'bits_per_component': 8, 'order': 'signed I,Q',
                  'continuous': False, 'samples': count, 'capture_us': elapsed, 'crc_valid': valid,
                  'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                  'rail_components': sum(v in (-128, 127) for pair in samples for v in pair),
                  'receiver': vars(args) | {'out': str(args.out)}}
        save(prefix.with_suffix('.json'), record)
        records.append(record)
        save(args.out / 'iq-summary.json', records)
        if not valid:
            raise RuntimeError('IQ size/CRC failure; reconnect before further commands')
        receiver.streaming = False
    return {'captures': len(records), 'continuous': False, 'crc_errors': 0}


def spectrum(receiver, args, out):
    if receiver.transport != 'USB':
        raise RuntimeError('S3 spectrum requires native USB')
    profile = receiver.specinfo['profiles'][args.profile]
    fs, rate, bins, stride, units = profile[:5]
    if not (profile[5] if len(profile) > 5 else receiver.specinfo['continuous']):
        raise ValueError('Continuous capture profile required')
    stats = 'SPECSTAT' in receiver.caps
    command = f'SPEC 0 {stride} {units} {args.detector} {rate} {bins}' + (' 1' if stats else '')
    decoder, audit = Decoder(), Audit(bins, args.detector)
    summary = {'utc_start': utc(), 'profile': profile, 'command': command,
               'requested_seconds': args.seconds, 'stats_requested': stats, 'error': None,
               'stop_monotonic_ns': None, 'command_access_after': False,
               'host_seconds_first_frame_to_stop': None}
    started = time.monotonic()
    try:
        # Mark uncertain before sending SPEC; even an invalid start reply may be followed by binary data.
        receiver.streaming = True
        receiver.write((command + '\n').encode('ascii'))
        header = receiver.line()
        summary['start_reply'] = header
        fields = header.split()
        if (len(fields) != 5 or fields[0] != 'SPEC' or int(fields[1]) != bins
                or int(fields[2]) != fs or int(fields[4]) != args.frequency):
            raise RuntimeError(f'Unexpected spectrum start: {header}')
        started = last_data = time.monotonic()
        stop_at = first_frame_at = None
        with (out / 'stream.bin').open('xb') as raw, (out / 'records.jsonl').open('w') as records:
            while decoder.end is None:
                now = time.monotonic()
                if stop_at is None and ((first_frame_at is not None and now - first_frame_at >= args.seconds)
                                        or now - last_data >= 5):
                    receiver.write(b'\n')
                    stop_at = now
                    summary['stop_monotonic_ns'] = time.monotonic_ns()
                    if first_frame_at is not None:
                        summary['host_seconds_first_frame_to_stop'] = now - first_frame_at
                    if now - last_data >= 5:
                        summary['error'] = 'No stream bytes for 5 seconds; requested stop'
                if stop_at is not None and now - stop_at > 10:
                    raise TimeoutError('SPECEND not received; reconnect before further commands')
                try:
                    data = receiver.read(min(16384, max(1, receiver.port.in_waiting)))
                except KeyboardInterrupt:
                    if stop_at is None:
                        receiver.write(b'\n')
                        stop_at = time.monotonic()
                        summary['error'] = 'Interrupted by operator'
                    continue
                if not data:
                    continue
                last_data = time.monotonic()
                raw.write(data)
                timestamp, monotonic = utc(), time.monotonic_ns()
                for kind, record, offset in decoder.feed(data):
                    if kind == 'frame' and first_frame_at is None:
                        # SPEC's text header precedes capture setup. Start the
                        # requested duration only once measured frames arrive.
                        first_frame_at = time.monotonic()
                    audit.add(kind, record)
                    records.write(json.dumps({'kind': kind, 'utc_received': timestamp,
                        'host_monotonic_ns': monotonic, 'stream_offset': offset, **record}) + '\n')
            receiver.streaming = False
        summary['command_access_after'] = receiver.command('INFO') == receiver.identity
    except Exception as error:
        summary['error'] = f'{type(error).__name__}: {error}'
    finally:
        summary.update(audit.summary(decoder))
        summary.update(utc_end=utc(), host_elapsed_seconds=time.monotonic() - started)
        summary['duration_pass'] = bool(decoder.end and decoder.end['elapsed_us'] >= args.seconds * 1e6
                                        and decoder.end['stopped_by_host'] == 1
                                        and summary['stop_monotonic_ns'] is not None)
        summary['pass'] = bool(summary['integrity_pass'] and summary['duration_pass']
            and summary['command_access_after'] and not summary['error'] and (not stats or audit.stats > 0))
        save(out / 'summary.json', summary)
    if not summary['pass']:
        raise RuntimeError(f'Capture did not pass; inspect {out / "summary.json"}')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('identity', 'iq', 'spectrum', 'cycles'))
    parser.add_argument('--port', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--frequency', type=int)
    parser.add_argument('--bandwidth', type=int)
    parser.add_argument('--gain', type=int)
    parser.add_argument('--profile', type=int, help='Zero-based SPECINFO profile index; no automatic selection')
    parser.add_argument('--detector', type=int, choices=(0, 1), default=0)
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--rate', type=int, choices=(0, 1, 6))
    parser.add_argument('--samples', type=int)
    args = parser.parse_args()
    if args.mode != 'identity' and any(x is None for x in (args.frequency, args.bandwidth, args.gain)):
        parser.error('Capture requires explicit --frequency, --bandwidth and --gain')
    if args.mode in ('spectrum', 'cycles') and (args.profile is None or args.profile < 0 or args.seconds <= 0):
        parser.error('Spectrum needs a nonnegative --profile and positive --seconds')
    if args.mode == 'iq' and (args.rate is None or args.samples is None):
        parser.error('IQ needs --rate and --samples')
    args.out.mkdir(parents=True, exist_ok=False)
    save(args.out / 'invocation.json', {'utc': utc(), 'argv': sys.argv})
    receiver = None
    status = {'pass': False, 'error': None}
    try:
        receiver = Receiver(args.port, args.out)
        receiver.identify(args.out)
        if args.mode != 'identity':
            save(args.out / 'controls.json', receiver.configure(args))
        if args.mode == 'iq':
            status['result'] = iq(receiver, args)
        elif args.mode == 'spectrum':
            status['result'] = spectrum(receiver, args, args.out)
        elif args.mode == 'cycles':
            status['cycles_completed'] = 0
            for index in range(20):
                out = args.out / f'cycle-{index:02d}'
                out.mkdir()
                save(out / 'controls.json', receiver.configure(args))
                spectrum(receiver, args, out)
                status['cycles_completed'] += 1
                save(args.out / 'result.json', status)
        receiver.ok('RELEASE')
        status['pass'] = True
    except Exception as error:
        status['error'] = f'{type(error).__name__}: {error}'
    finally:
        if receiver:
            receiver.close()
        save(args.out / 'result.json', status)
    print(json.dumps(status, indent=2))
    return 0 if status['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
