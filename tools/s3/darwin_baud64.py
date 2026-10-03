#!/usr/bin/env python3
"""Diagnostic runner using the SDK's 64-bit IOSSIOSPEED ABI on macOS.

The local Apple ioss.h defines IOSSIOSPEED_64 = _IOW('T', 2, uint64_t).
pyserial 3.5 normally uses the 32-bit form. No installed library is modified.
Usage: python darwin_baud64.py -m esptool ...  or  python darwin_baud64.py script.py ...
"""
import array
import fcntl
from pathlib import Path
import runpy
import struct
import sys

import serial.serialposix

if sys.platform != 'darwin' or struct.calcsize('P') != 8:
    raise RuntimeError('This diagnostic is only for 64-bit macOS')


def set_speed(self, baudrate):
    fcntl.ioctl(self.fd, 0x80085402, array.array('Q', [baudrate]), 1)


serial.serialposix.PlatformSpecific._set_special_baudrate = set_speed
if sys.argv[1] == '-m':
    sys.argv = sys.argv[2:]
    runpy.run_module(sys.argv[0], run_name='__main__')
else:
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
    runpy.run_path(sys.argv[0], run_name='__main__')
