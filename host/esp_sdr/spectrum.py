"""SPC1/SPS1 decoding and continuous-stream integrity accounting."""
import struct
import zlib

HEADER = struct.Struct('<4sIQIHBBHBB')
STATS = struct.Struct('<4sHHHHIIIIHHI')
FRAME_FIELDS = 'sequence sample_index samples_spanned ffts flags gain_raw drops log2_bins db_multiplier'.split()
STAT_FIELDS = ('core0_per_mille core1_per_mille coverage_per_mille mode heap_free '
               'heap_largest abandoned drops late_max queue_per_mille ffts_per_second').split()
from .wire import END_FIELDS


class Decoder:
    """Resynchronize on CRC-valid records or a complete SPECEND, preserving offsets."""
    def __init__(self):
        self.buffer = bytearray()
        self.offset = 0
        self.crc_errors = {'SPC1': 0, 'SPS1': 0}
        self.discarded_bytes = 0
        self.invalid_headers = 0
        self.end = None

    def consume(self, count, discarded=False):
        del self.buffer[:count]
        self.offset += count
        if discarded:
            self.discarded_bytes += count

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 4 and self.end is None:
            magic = bytes(self.buffer[:4])
            if magic == b'SPEC':
                newline = self.buffer.find(b'\n')
                if newline < 0 and len(self.buffer) < 256:
                    break
                parts = bytes(self.buffer[:newline + 1]).split() if newline >= 0 else []
                if len(parts) == 13 and parts[0] == b'SPECEND' and all(p.isdigit() for p in parts[1:]):
                    self.end = dict(zip(END_FIELDS, map(int, parts[1:])))
                    at = self.offset
                    self.consume(newline + 1)
                    yield 'end', self.end, at
                    break
                self.consume(1, True)
                continue
            if magic not in (b'SPC1', b'SPS1'):
                self.consume(1, True)
                continue
            if magic == b'SPC1':
                if len(self.buffer) < HEADER.size:
                    break
                log2n = self.buffer[26]
                if log2n not in (8, 9, 10, 11) or self.buffer[27] != 2:
                    self.invalid_headers += 1
                    self.consume(1, True)
                    continue
                size = HEADER.size + (1 << log2n) + 4
            else:
                size = STATS.size + 4
            if len(self.buffer) < size:
                break
            raw = bytes(self.buffer[:size])
            if zlib.crc32(raw[:-4]) != int.from_bytes(raw[-4:], 'little'):
                self.crc_errors[magic.decode()] += 1
                self.consume(1, True)
                continue
            if magic == b'SPC1':
                record = dict(zip(FRAME_FIELDS, HEADER.unpack(raw[:HEADER.size])[1:]))
                record['power_codes'] = list(raw[HEADER.size:-4])
                kind = 'frame'
            else:
                record = dict(zip(STAT_FIELDS, STATS.unpack(raw[:-4])[1:]))
                kind = 'stats'
            at = self.offset
            self.consume(size)
            yield kind, record, at


class Audit:
    """Audit a continuous SPEC profile; snapshot flag/gaps do not pass this check."""
    def __init__(self, bins, detector):
        self.bins, self.detector = bins, detector
        self.frames = self.ffts = self.stats = 0
        self.sequence_missing = self.sequence_reordered = 0
        self.sample_gap_pairs = self.sample_overlap_frames = 0
        self.skipped_processing_frames = self.prior_drop_frames = 0
        self.shape_errors = self.telemetry_errors = 0
        self.previous = None

    def add(self, kind, record):
        if kind == 'stats':
            self.stats += 1
            if (any(record[k] > 1000 for k in ('core0_per_mille', 'core1_per_mille',
                                               'coverage_per_mille', 'queue_per_mille'))
                    or record['heap_largest'] > record['heap_free']):
                self.telemetry_errors += 1
        if kind != 'frame':
            return
        self.frames += 1
        self.ffts += record['ffts']
        flags = record['flags']
        self.skipped_processing_frames += bool(flags & 2)
        self.prior_drop_frames += bool(flags & 4)
        if (1 << record['log2_bins'] != self.bins or not record['ffts']
                or not record['samples_spanned'] or flags & 1 != self.detector or flags & ~7):
            self.shape_errors += 1
        previous = self.previous
        expected = (previous['sequence'] + 1) & 0xffffffff if previous else 0
        delta = (record['sequence'] - expected) & 0xffffffff
        if delta < 0x80000000:
            self.sequence_missing += delta
        else:
            self.sequence_reordered += 1
        if previous:
            gap = record['sample_index'] - previous['sample_index'] - previous['samples_spanned']
            self.sample_gap_pairs += max(gap, 0)
            self.sample_overlap_frames += gap < 0
        self.previous = record

    def summary(self, decoder):
        result = {k: v for k, v in vars(self).items() if k != 'previous'}
        result.update(crc_errors=decoder.crc_errors, discarded_bytes=decoder.discarded_bytes,
                      invalid_headers=decoder.invalid_headers, end=decoder.end,
                      end_confirmed=decoder.end is not None)
        end = decoder.end
        result['unreceived_emitted_frames'] = end['frames'] - self.frames if end else None
        result['analyzed_fraction'] = end['ffts'] * self.bins / end['pairs'] if end and end['pairs'] else None
        result['delivered_ffts_match'] = self.ffts == end['ffts'] if end else None
        result['integrity_pass'] = bool(end and end['status'] == 0 and self.frames
            and not (sum(decoder.crc_errors.values()) or decoder.discarded_bytes
                     or decoder.invalid_headers or self.sequence_missing or self.sequence_reordered
                     or self.sample_gap_pairs or self.sample_overlap_frames or self.shape_errors
                     or self.telemetry_errors or end['drops'] or result['unreceived_emitted_frames'])
            and result['delivered_ffts_match'])
        return result


def decode_iq8(payload):
    """Signed I then Q, per receiver.c pack_iq8; raw ADC units, not volts."""
    if len(payload) % 2:
        raise ValueError('Odd IQ8 payload size')
    values = struct.unpack(f'{len(payload)}b', payload)
    return list(zip(values[::2], values[1::2]))
