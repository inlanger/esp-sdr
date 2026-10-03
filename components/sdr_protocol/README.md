# ESP-SDR wire layouts

A header-only ESP-IDF component containing the existing packed SPC1, SPS1 and
IQS1 declarations and magic values. Header sizes remain 28, 36 and 24 bytes;
compile-time assertions check them. It adds no runtime work or buffers.

The current ring, snapshot spectrum and telemetry producers use the same
layouts. CRC still uses the SDK ROM implementation at the original call sites;
the coverage is header + payload with seed 0, matching host zlib.crc32.
All fields are little-endian. This is a C header for the ESP32 targets.

Within this project add `REQUIRES sdr_protocol` to a consuming component and
`#include "sdr_protocol.h"` to its C source. In another IDF project, copy this
directory into `components/` or add its absolute path to `EXTRA_COMPONENT_DIRS`
before including IDF's project.cmake. No component registry publication is needed.

Radio registers, SRAM ownership, bank switching, assembly FFT/FIR, USB FIFO
access and gain tables remain in the corresponding chip implementation.
Sharing a wire format does not establish capture support on a new chip.
See [spectrum](../../docs/spectrum.md) and [IQ stream](../../docs/iq-stream.md).
