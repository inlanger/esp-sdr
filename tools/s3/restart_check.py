#!/usr/bin/env python3
"""Observe UART boot on open, then verify one EN reset without reflashing.

Uses esptool.reset.HardReset, whose documented sequence pulses EN via RTS.
This is not a physical USB unplug/replug test. Run through darwin_baud64.py.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import zlib

import serial
from esptool.reset import HardReset
from capture import save, utc

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=False)
s = serial.Serial(port=None, baudrate=115200, timeout=.05, write_timeout=2)
s.dtr = s.rts = False
s.port = args.port
result = {'utc_start': utc(), 'physical_usb_replug': False, 'flash_written': False, 'pass': False}
log = (args.out / 'serial.jsonl').open('w', buffering=1)


def boot(name):
    data = bytearray()
    until = time.monotonic() + 3
    while time.monotonic() < until:
        data.extend(s.read(4096))
    (args.out / f'{name}.bin').write_bytes(data)
    (args.out / f'{name}.txt').write_text(data.decode('ascii', errors='replace'))
    return {'bytes': len(data), 'rom_boot_markers': data.count(b'ESP-ROM:esp32s3-20210327')}


def command(text):
    log.write(json.dumps({'utc': utc(), 'tx': text}) + '\n')
    s.write((text + '\n').encode())
    data = bytearray()
    until = time.monotonic() + 5
    while time.monotonic() < until:
        data.extend(s.read(1))
        if data.endswith(b'\n'):
            reply = data.decode('ascii').strip()
            log.write(json.dumps({'utc': utc(), 'rx': reply}) + '\n')
            return reply
    raise TimeoutError(f'Incomplete reply: {data!r}')


try:
    s.open()
    result['open_at_115200'] = boot('boot-on-open')
    s.baudrate = 2000000
    result['before_reset'] = {q: command(q) for q in ('INFO', 'TRANSPORT?')}
    if result['before_reset']['INFO'] != 'S3SDR 6 burst 16380':
        raise RuntimeError('Pre-reset identity failed')
    s.baudrate = 115200
    HardReset(s)()
    result['explicit_en_reset'] = boot('boot-after-en-reset')
    s.baudrate = 2000000
    result['after_reset'] = {q: command(q) for q in ('INFO', 'CAPS', 'LIMITS?', 'TRANSPORT?', 'SPECINFO?', 'GAIN?')}
    if result['after_reset']['INFO'] != result['before_reset']['INFO']:
        raise RuntimeError('Identity changed after EN reset')
    # Verify receive after reboot in its reported default hardware-AGC state.
    for text in ('FREQ 2442', 'BANDWIDTH 13'):
        if command(text) != 'OK':
            raise RuntimeError(f'Command failed: {text}')
    header = command('CAP16 16380 6')
    (args.out / 'iq.header.txt').write_text(header + '\n')
    fields = header.split()
    if len(fields) != 4 or fields[0] != 'DATA' or int(fields[1]) != 16380:
        raise RuntimeError(f'Unexpected capture header: {header}')
    raw = bytearray()
    until = time.monotonic() + 10
    while len(raw) < 32760 and time.monotonic() < until:
        raw.extend(s.read(min(4096, 32760 - len(raw))))
    (args.out / 'iq.bin').write_bytes(raw)
    result['iq'] = {'header': header, 'bytes': len(raw), 'sample_rate_hz': 16000000,
                    'format': 'signed int8 I,Q', 'frequency_mhz': 2442, 'bandwidth_mhz': 13,
                    'gain_before_capture': result['after_reset']['GAIN?'], 'continuous': False,
                    'crc_valid': len(raw) == 32760 and zlib.crc32(raw) == int(fields[2], 16),
                    'sha256': hashlib.sha256(raw).hexdigest()}
    if not result['iq']['crc_valid']:
        raise RuntimeError('Post-reset capture size/CRC failure')
    result['info_after_capture'] = command('INFO')
    if command('RELEASE') != 'OK':
        raise RuntimeError('Release failed')
    result['pass'] = result['info_after_capture'] == result['before_reset']['INFO']
except Exception as error:
    result['error'] = f'{type(error).__name__}: {error}'
finally:
    s.close()
    log.close()
    result['utc_end'] = utc()
    save(args.out / 'result.json', result)
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['pass'] else 1)
