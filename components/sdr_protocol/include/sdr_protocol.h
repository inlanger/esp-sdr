/* ESP-SDR little-endian wire layouts. Payload and CRC32 follow each header.
 * CRC32 covers header + payload, seed 0 (esp_rom_crc32_le / zlib.crc32).
 * No radio, transport, scheduler or DSP dependencies. */
#pragma once
#include <stdint.h>

#define SPEC_MAGIC 0x31435053u /* "SPC1" */
#define STAT_MAGIC 0x31535053u /* "SPS1" */
#define IQS_MAGIC 0x31535149u /* "IQS1" */
#define IQS_PAYLOAD 1024u

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t frame;      /* frame sequence number */
    uint64_t pair_index; /* stream index of the first pair (gapless clock) */
    uint32_t pairs;      /* stream pairs this frame spans */
    uint16_t ffts;       /* FFTs merged into this frame */
    uint8_t flags;       /* bit0 max-hold, bit1 work abandoned, bit2 frames dropped before, bit3 snapshot gaps */
    uint8_t gain;        /* bits 20..27 of the frame's first IQ word */
    uint16_t drops;      /* cumulative dropped frames, saturating */
    uint8_t nfft_log2;   /* frame carries 1 << nfft_log2 bin codes */
    uint8_t db_step;     /* bin code = 10*log10(power) * db_step */
} spec_header_t;
_Static_assert(sizeof(spec_header_t) == 28, "SPEC header layout");

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t core0,core1,coverage,mode;
    uint32_t heap_free,heap_largest,abandoned,drops;
    uint16_t late_max,queue;
    uint32_t ffts_per_s;
} spectrum_stats_frame_t;
_Static_assert(sizeof(spectrum_stats_frame_t)==36,"SPS1 layout");

typedef struct __attribute__((packed)) {
    uint32_t magic, frame;
    uint64_t sample_index; /* decimated stream index of the first sample */
    uint16_t samples;
    uint8_t bits, flags;   /* flags bit0: gap before this frame, bit1: frame(s) dropped before */
    uint16_t dec;
    uint8_t gain, shift;
} iqs_header_t;
_Static_assert(sizeof(iqs_header_t) == 24, "IQS header");
