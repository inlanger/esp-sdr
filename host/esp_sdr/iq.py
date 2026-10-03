"""IQS1 IQ8/IQ16 decoding, CRC and sample-loss accounting. No transport I/O."""
import struct
import zlib

from .wire import END_FIELDS

HEADER = struct.Struct('<4sIQHBBHBB')
FIELDS = 'sequence sample_index samples bits flags decimation gain_raw shift'.split()


class Stream:
    def __init__(self, decimation, shift, bits):
        self.decimation = decimation
        self.shift = shift
        self.bits = bits
        self.bytes_per_pair = bits // 4
        self.buffer = bytearray()
        self.offset = 0
        self.end = None
        self.frames = self.samples = 0
        self.crc_errors = self.invalid_headers = self.discarded_bytes = 0
        self.sequence_errors = self.gap_samples = self.overlap_frames = 0
        self.gap_flags = self.drop_flags = 0
        self.first_index = self.next_index = None
        self.next_sequence = 0
        self.recovered_truncated_end = False

    def consume(self, size, discarded=False):
        del self.buffer[:size]
        self.offset += size
        if discarded:
            self.discarded_bytes += size

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 4 and self.end is None:
            if self.buffer[:4] == b'IQSE':
                newline = self.buffer.find(b'\n')
                if newline < 0 and len(self.buffer) < 256:
                    break
                parts = bytes(self.buffer[:newline + 1]).split() if newline >= 0 else []
                if len(parts) == 13 and parts[0] == b'IQSEND' and all(p.isdigit() for p in parts[1:]):
                    self.end = dict(zip(END_FIELDS, map(int, parts[1:])))
                    self.consume(newline + 1)
                    break
                self.consume(1, True)
                continue
            if self.buffer[:4] != b'IQS1':
                at = self.buffer.find(b'IQS', 1)
                self.consume(at if at >= 0 else max(1, len(self.buffer) - 3), True)
                continue
            if len(self.buffer) < HEADER.size:
                break
            record = dict(zip(FIELDS, HEADER.unpack_from(self.buffer)[1:]))
            if not (1 <= record['samples'] <= 1024 // self.bytes_per_pair and record['bits'] == self.bits
                    and record['decimation'] == self.decimation and record['shift'] == self.shift
                    and not record['flags'] & ~3):
                self.invalid_headers += 1
                self.consume(1, True)
                continue
            size = HEADER.size + record['samples'] * self.bytes_per_pair + 4
            if len(self.buffer) < size:
                break
            raw = bytes(self.buffer[:size])
            if zlib.crc32(raw[:-4]) != int.from_bytes(raw[-4:], 'little'):
                self.crc_errors += 1
                self.consume(1, True)
                continue
            record['stream_offset'] = self.offset
            record['iq_byte_offset'] = self.samples * self.bytes_per_pair
            self.sequence_errors += record['sequence'] != self.next_sequence
            self.next_sequence = record['sequence'] + 1
            if self.first_index is None:
                self.first_index = record['sample_index']
            if self.next_index is not None:
                gap = record['sample_index'] - self.next_index
                self.gap_samples += max(gap, 0)
                self.overlap_frames += gap < 0
            self.next_index = record['sample_index'] + record['samples']
            self.gap_flags += bool(record['flags'] & 1)
            self.drop_flags += bool(record['flags'] & 2)
            self.frames += 1
            self.samples += record['samples']
            self.consume(size)
            yield record, raw[HEADER.size:-4]

    def recover_end(self):
        """After receive idle only: a bounded device drain may truncate a frame."""
        at = self.buffer.find(b'IQSEND ')
        if at < 0 or not self.buffer.endswith(b'\n'):
            return False
        parts = bytes(self.buffer[at:]).split()
        if len(parts) != 13 or not all(p.isdigit() for p in parts[1:]):
            return False
        end = dict(zip(END_FIELDS, map(int, parts[1:])))
        if end['frames'] < self.frames or end['pairs'] // self.decimation < (self.next_index or 0):
            return False
        self.consume(at, True)
        list(self.feed(b''))
        self.recovered_truncated_end = self.end is not None
        return self.recovered_truncated_end

    def summary(self):
        result = {k: v for k, v in vars(self).items() if k != 'buffer'}
        result['trailing_bytes'] = len(self.buffer)
        end = self.end
        result['unreceived_emitted_frames'] = end['frames'] - self.frames if end else None
        result['lost_input_pairs'] = end['ffts'] if end else None
        result['expected_output_samples'] = end['pairs'] // self.decimation if end else None
        result['output_count_matches'] = bool(end and self.first_index == 0
            and self.samples == result['expected_output_samples'])
        result['integrity_pass'] = bool(end and end['status'] == 0 and self.frames
            and not (self.crc_errors or self.invalid_headers or self.discarded_bytes
                     or self.sequence_errors or self.gap_samples or self.overlap_frames
                     or self.gap_flags or self.drop_flags or self.buffer
                     or end['drops'] or end['abandoned'] or end['ffts']
                     or result['unreceived_emitted_frames'])
            and result['output_count_matches'])
        return result
