/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 ESPsoup contributors / Peter Holzhauser
 *
 * ESP32-C5 register-level capture engine (ESPsoup extension, receive only).
 * Adapted from ESPsoup 085c752e51127832f1f9259efe4a60a8ec0f8635.
 * Only capture/CAPT are retained; see docs/c5-receiver.md.
 *
 * Hardware facts this relies on (measured on ESP32-C5 rev 1.0 boards):
 *  - The RF dump writes 32-bit words (I = bits 0-9, Q = bits 10-19, signed)
 *    into SRAM bank 1 (0x40820000-0x4083ffff) while SRAM_OWNER bits 8-11 = 2.
 *    Owner bit 16 selects the 64 KiB half (0 = 0x40820000, 1 = 0x40830000);
 *    the write address is  half + 4 * (position mod 16384).
 *  - DUMP_MODE bits 0-16 are the live write position.  Counts <= 65535 stop by
 *    themselves (CTRL bit 18 = done); larger counts never complete, i.e. the
 *    writer free-runs until CTRL bit 31 is cleared.
 *  - While the dump owns bank 1, neither the CPU nor the GDMA can read any
 *    part of it (stale words).  A finished dump re-arms within ~4 us, so the
 *    continuous engine (c5_cspec.h) copies each block out between two dumps.
 *  - Every access to the "source-15 gate" register 0x600a20b4 stalls the bus
 *    for ~0.82 ms; the vendor adctrig() does a read-modify-write on every
 *    snapshot (the fixed ~1.7 ms overhead of the stock path).  The engine
 *    checks the bit once per boot instead.
 *  - phy_set_rf_freq_offset(xtal, MHz, offset) takes the offset in kHz.
 */
#pragma once
#include "esp_heap_caps.h"
#include <math.h>

#define DUMP_CTRL 0x600a9004u
#define DUMP_MODE 0x600a9008u
#define DUMP_PACK 0x600a9018u
#define DUMP_WORDS ((volatile uint32_t *)0x40820000u)
#define DUMP_BUF ((uint32_t *)0x40820000u)
#define HALF_WORDS 16384u
#define BIG_MAX 32704u            /* 2 x 16384 minus the worst accepted flip lag */
#define FLIP_MARGIN 64u
#define SENTINEL 0xa5a0055au

static bool opt_stock_engine;     /* ENGINE STOCK: vendor adctrig for <=16380 */
static uint32_t opt_seq;
static int64_t opt_last_t0;
static unsigned opt_last_lag;

/* ---------------------------------------------------------------- engine */
static inline uint32_t dump_pos16(void) { return REG_READ(DUMP_MODE) & 0xffffu; }

static void dump_arm(unsigned div, uint32_t count, bool upper, uint32_t *owner_saved) {
    *owner_saved = REG_READ(SRAM_OWNER_REG);
    REG_WRITE(DUMP_CTRL, 0);
    REG_WRITE(DUMP_MODE, (REG_READ(DUMP_MODE) & ~0x00e00000u) | ((div & 7u) << 21));
    REG_WRITE(0x600a9c04, 0xffffffffu);              /* dump clocks, as adctrig */
    REG_SET_BIT(0x600a0800, 4);
    REG_WRITE(DUMP_CTRL, count & 0x1ffffu);
    REG_WRITE(DUMP_PACK, (REG_READ(DUMP_PACK) & ~0x00ffffffu) | 24u | (25u << 6) | (26u << 12) | (27u << 18));
    REG_SET_BIT(DUMP_PACK, 1u << 24);
    REG_WRITE(SRAM_OWNER_REG, (*owner_saved & ~0x10f00u) | 0x200u | (upper ? 0x10000u : 0));
    __asm__ volatile("fence rw,rw" ::: "memory"); (void)REG_READ(SRAM_OWNER_REG);
    /* Source-15 gate at 0x600a20b4. Every access to this register stalls the
     * bus for ~0.82 ms (read) / 1.64 ms (read-modify-write, as in the vendor
     * adctrig) - this is the fixed ~1.7 ms per-snapshot overhead of the stock
     * firmware. The bit was found clear after boot and stays clear, so check
     * it once per boot only. */
    static bool gate_checked;
    if (!gate_checked) { if (REG_READ(0x600a20b4) & 1u) { REG_CLR_BIT(0x600a20b4, 1u); } gate_checked = true; }
    REG_WRITE(DUMP_MODE, (REG_READ(DUMP_MODE) & ~0x001e0000u) | (15u << 17));
}
static inline void dump_go(void) {
    REG_SET_BIT(DUMP_CTRL, 1u << 31);
    REG_SET_BIT(DUMP_CTRL, 1u << 19); REG_CLR_BIT(DUMP_CTRL, 1u << 19);
}
static void dump_halt(uint32_t owner_saved) {
    REG_CLR_BIT(DUMP_CTRL, 1u << 31);
    REG_WRITE(DUMP_CTRL, 0);
    REG_WRITE(SRAM_OWNER_REG, owner_saved);
    __asm__ volatile("fence rw,rw" ::: "memory"); (void)REG_READ(SRAM_OWNER_REG);
}
/* Spin (interrupts off) until the 16-bit position passes target, then toggle the
 * half.  Returns positions read before (p0) and after (p1) the owner write. */
static inline void IRAM_ATTR dump_flip_at(uint32_t target16, uint32_t *p0, uint32_t *p1) {
    unsigned irq = portSET_INTERRUPT_MASK_FROM_ISR();
    uint32_t o = REG_READ(SRAM_OWNER_REG) ^ 0x10000u, p;
    unsigned guard = 400000;       /* ~20 ms at 4 MS/s; no timer call inside the spin */
    do { p = REG_READ(DUMP_MODE) & 0xffffu; } while (((p - target16) & 0x8000u) && --guard);
    REG_WRITE(SRAM_OWNER_REG, o);
    *p1 = REG_READ(DUMP_MODE) & 0xffffu;
    *p0 = p;
    portCLEAR_INTERRUPT_MASK_FROM_ISR(irq);
}

/* Contiguous snapshot of n <= BIG_MAX samples into DUMP_BUF[0..n).
 * n <= 16380: one finite dump into the lower half (stock-equivalent).
 * n  > 16380: two halves; the flip lag L is measured exactly from sentinels
 * left in the upper half and the first L samples are dropped, then the data
 * is re-ordered in place so the caller sees one contiguous block. */
static const char *fast_capture(unsigned n, unsigned div, uint32_t *elapsed) {
    volatile uint32_t *w = DUMP_WORDS;
    bool big = n > HALF_WORDS - 4;
    if (big) { for (unsigned j = 0; j < 2 * HALF_WORDS; j++) w[j] = SENTINEL; }
    else {
        for (unsigned j = 0; j < n; j++) w[j] = SENTINEL;
        for (unsigned j = 0; j < 4; j++) w[n + j] = 0x5a5aa5a5u ^ j;
    }
    unsigned analog_saved[2]; rx_analog_apply(analog_saved);
    uint32_t owner;
    dump_arm(div, big ? 2 * HALF_WORDS : n, false, &owner);
    int64_t t0 = esp_timer_get_time();
    dump_go();
    uint32_t p0 = 0, p1 = 0;
    if (big) dump_flip_at(HALF_WORDS, &p0, &p1);
    int64_t lim = t0 + 20000;
    while (!(REG_READ(DUMP_CTRL) & (1u << 18)) && esp_timer_get_time() < lim) {}
    bool done = (REG_READ(DUMP_CTRL) & (1u << 18)) != 0;
    *elapsed = (uint32_t)(esp_timer_get_time() - t0);
    dump_halt(owner);
    rx_analog_restore(analog_saved);
    opt_last_t0 = t0; opt_last_lag = 0;
    if (!done) return "ERR capture_timeout\n";
    if (!big) {
        for (unsigned j = 0; j < n; j++) if (w[j] == SENTINEL) return "ERR capture_timeout\n";
        for (unsigned j = 0; j < 4; j++) if (w[n + j] != (0x5a5aa5a5u ^ j)) return "ERR capture_overrun\n";
        return NULL;
    }
    unsigned L = 0;
    while (L < FLIP_MARGIN + 1 && w[HALF_WORDS + L] == SENTINEL) L++;
    opt_last_lag = L;
    if (L > FLIP_MARGIN) return "ERR capture_flip\n";
    uint32_t tmp[FLIP_MARGIN];
    for (unsigned j = 0; j < L; j++) tmp[j] = w[j];
    memmove(DUMP_BUF, DUMP_BUF + L, (HALF_WORDS - L) * 4);
    for (unsigned j = 0; j < L; j++) DUMP_BUF[HALF_WORDS - L + j] = tmp[j];
    /* 16381..16383 also use two halves for the canary margin; they have no
     * output tail. Avoid unsigned underflow in Soup's unconditional copy. */
    if (n > HALF_WORDS)
        memmove(DUMP_BUF + HALF_WORDS, DUMP_BUF + HALF_WORDS + L, (n - HALF_WORDS) * 4);
    for (unsigned j = 0; j < n; j++) if (DUMP_BUF[j] == SENTINEL) return "ERR capture_timeout\n";
    return NULL;
}

/* ------------------------------------------------------------- packing */
static void opt_pack(uint32_t *b, unsigned n, unsigned format) {
    uint8_t *p = (uint8_t *)b;
    if (format == 16) {
        for (unsigned j = 0; j < n; j++) { uint32_t x = b[j]; p[2 * j] = (x >> 2) & 255; p[2 * j + 1] = (x >> 12) & 255; }
    } else if (format == 20) {
        for (unsigned j = 0; j < n; j += 2, p += 5) {
            uint32_t a = b[j] & 0xfffffu, c = j + 1 < n ? b[j + 1] & 0xfffffu : 0;
            p[0] = a; p[1] = a >> 8; p[2] = (a >> 16) | (c << 4);
            if (j + 1 < n) { p[3] = c >> 4; p[4] = c >> 12; }
        }
    }
}
static size_t opt_wire(unsigned n, unsigned format) {
    return format == 16 ? n * 2 : format == 20 ? (n * 20u + 7u) / 8u : n * 4;
}
/* Header "DATA n crc us" (stock) or "DATAT n crc us t0_us seq lag" (stamped). */
static bool opt_send_frame(const void *payload, size_t bytes, unsigned n, uint32_t elapsed, bool stamped) {
    uint32_t crc = esp_rom_crc32_le(0, payload, bytes);
    char h[128];
    if (stamped)
        snprintf(h, sizeof(h), "DATAT %u %08" PRIx32 " %" PRIu32 " %" PRId64 " %" PRIu32 " %u\n", n, crc, elapsed,
                 opt_last_t0, ++opt_seq, opt_last_lag);
    else snprintf(h, sizeof(h), "DATA %u %08" PRIx32 " %" PRIu32 "\n", n, crc, elapsed);
    return send_bytes(h, strlen(h)) && send_bytes(payload, bytes);
}
static bool opt_capture(unsigned n, unsigned div, unsigned format, bool stamped) {
    prepare_rx();
    uint32_t elapsed;
    const char *err = fast_capture(n, div, &elapsed);
    if (err) { reply(err); return false; }
    opt_pack(DUMP_BUF, n, format);
    return opt_send_frame(DUMP_BUF, opt_wire(n, format), n, elapsed, stamped);
}

/* Snapshot source for the shared on-chip spectrum (spectrum.c, upstream SPEC
 * protocol): the same capture as acquire_iq() without the ~1.7 ms vendor stall. */
static bool opt_acquire(unsigned n, unsigned div, const uint32_t **words, unsigned *elapsed_us) {
    prepare_rx();
    uint32_t elapsed;
    const char *err = fast_capture(n, div, &elapsed);
    if (err) { reply(err); return false; }
    *words = DUMP_BUF; *elapsed_us = elapsed;
    return true;
}

/* Additive commands; existing CAP replies keep protocol 6. */
static bool engine_command(const char *line) {
    unsigned n, rate, khz; char extra;
    if (!strcmp(line, "ENGINE STOCK")) { opt_stock_engine = true; reply("OK\n"); return true; }
    if (!strcmp(line, "ENGINE FAST")) { opt_stock_engine = false; reply("OK\n"); return true; }
    if (!strcmp(line, "ENGINE?")) { reply(opt_stock_engine ? "ENGINE STOCK\n" : "ENGINE FAST\n"); return true; }
    if (!strcmp(line, "FWOPT?")) { reply("FWOPT inlanger-c5-1 rx-only C5 soup-rx-085c752 upstream-dc-5ede49b\n"); return true; }
    if (sscanf(line, "FREQK %u %c", &khz, &extra) == 1) {
        if (khz < RX_FREQ_MIN * 1000 || khz > RX_FREQ_MAX * 1000) { reply("ERR command\n"); return true; }
        unsigned mhz = (khz + 500) / 1000;
        int offset = (int)khz - (int)mhz * 1000;
        if (offset && c5_tune_method == 2) { reply("ERR freqk_tunem\n"); return true; }
        frequency_mhz = mhz; c5_khz_offset = offset; rx_ready = false; prepare_rx();
        reply("OK\n"); return true;
    }
    if (!strcmp(line, "FREQK?")) {
        char h[48]; snprintf(h, sizeof(h), "FREQK %d\n", (int)frequency_mhz * 1000 + c5_khz_offset); reply(h); return true;
    }
    if (sscanf(line, "CAPT16 %u %u %c", &n, &rate, &extra) == 2 || sscanf(line, "CAPT20 %u %u %c", &n, &rate, &extra) == 2) {
        if (n < 256 || n > BIG_MAX || rate > 5) { reply("ERR command\n"); return true; }
        opt_capture(n, rate, line[4] == '1' ? 16 : 20, true); return true;
    }
    return false;
}
