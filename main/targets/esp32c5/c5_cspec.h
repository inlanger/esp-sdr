/* SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026 ESPsoup contributors / Peter Holzhauser
 *
 * ESP32-C5 continuous capture engine (ESPsoup extension, receive only),
 * included by the C5 receiver. The dump runs back to back in 8192-word blocks; the
 * gaps include copying and processing time. Only CSPEC is retained here.
 * Adapted from ESPsoup 085c752e51127832f1f9259efe4a60a8ec0f8635;
 * see docs/c5-receiver.md for limits and protocol. */

/* ------------------------------------------------- USB/UART output ring */
#define STREAM_RING 16384u
static uint8_t *st_ring; static unsigned st_ring_size = STREAM_RING;
static volatile unsigned st_head, st_tail;  /* free-running byte counters */
static bool st_isr;                         /* native USB: ISR drains the ring */
static inline unsigned ring_used(void) { return st_head - st_tail; }
static void ring_put(const void *p, unsigned n) {
    unsigned h = st_head, off = h & (st_ring_size - 1), k = n > st_ring_size - off ? st_ring_size - off : n;
    memcpy(st_ring + off, p, k);
    if (n > k) memcpy(st_ring, (const uint8_t *)p + k, n - k);
    __asm__ volatile("fence rw,rw" ::: "memory");
    st_head = h + n;
}
/* Non-blocking: hand ring bytes to the transport (ISR kick or FIFO polling). */
static void IRAM_ATTR ring_pump(void) {
    if (st_isr) { if (ring_used()) burst_serial_ring_kick(); return; }
    while (ring_used()) {
        unsigned t = st_tail & (st_ring_size - 1), k = ring_used();
        if (k > st_ring_size - t) k = st_ring_size - t;
        int s = burst_serial_try_send(st_ring + t, k);
        if (s <= 0) break;
        st_tail += s;
    }
}
/* Queue header + payload as one unit; false (nothing queued) when it does not fit. */
static bool ring_frame(const char *h, unsigned hl, const void *pl, unsigned bytes) {
    if (ring_used() + hl + bytes > st_ring_size) return false;
    ring_put(h, hl); if (bytes) ring_put(pl, bytes);
    ring_pump();
    return true;
}
static bool ring_open_sz(unsigned size) {   /* size: power of two */
    st_ring = heap_caps_malloc(size, MALLOC_CAP_8BIT); st_ring_size = size;
    if (!st_ring) return false;
    st_head = st_tail = 0;
    st_isr = burst_serial_ring_start(st_ring, size, &st_head, &st_tail);
    return true;
}
static bool ring_open(void) { return ring_open_sz(STREAM_RING); }
static void ring_close(void) {
    int64_t lim = esp_timer_get_time() + 3000000;
    while (ring_used() && esp_timer_get_time() < lim) ring_pump();
    if (st_isr) { burst_serial_ring_stop(); st_isr = false; }
    free(st_ring); st_ring = NULL;
}

/* ------------------------------------------------- continuous engine */
/* Measured by Soup (its diagnostics/c5_gap_probe.h): neither CPU nor GDMA can read
 * bank 1 while the dump owns it (GDMATEST: reads return stale words), so true
 * ping-pong is impossible.  But a finished dump re-arms in ~4 us (B2B, 99.9 % duty with no
 * work) and memcpy of a block into the heap costs ~2.6 CPU cycles per word.
 * So the engine dumps block k+1 while the CPU works on the heap copy of block
 * k; the only gap per block is the copy (~90 us per 8192 words): 96 % duty at
 * 4 MS/s, 92 % at 8 MS/s, 83 % at 20 MS/s.  Gaps are counted exactly with the
 * CPU cycle counter (CPU and dump clock share the crystal: 3/6/12/24/30/60
 * cycles per sample). */
#define CONT_WORDS 8192u
static const uint8_t hw_cps[6] = {3, 6, 12, 24, 30, 60};
typedef struct {
    unsigned div, n, cps;
    uint32_t *buf;                       /* heap copy of the last finished block */
    uint32_t owner; unsigned analog[2];
    uint32_t cyc_go, cyc_prev; int64_t t_go; bool have_prev;
    int64_t blk_t0; uint32_t blk_gap;    /* block in buf: time of sample 0, input samples missing before it */
    uint32_t blocks, maxgap;
    uint64_t lost, copy_cyc, wait_cyc, work_cyc;
} cont_t;
static cont_t cont_last;                                  /* CSTAT? */

static void IRAM_ATTR cont_go(cont_t *c) {
    dump_arm(c->div, c->n, false, &c->owner);
    unsigned irq = portSET_INTERRUPT_MASK_FROM_ISR();
    c->t_go = esp_timer_get_time(); c->cyc_go = esp_cpu_get_cycle_count();
    dump_go();
    portCLEAR_INTERRUPT_MASK_FROM_ISR(irq);
}
static void cont_start(cont_t *c, unsigned div, uint32_t *buf, unsigned words) {
    memset(c, 0, sizeof(*c));
    c->div = div; c->n = words; c->cps = hw_cps[div]; c->buf = buf;
    prepare_rx(); rx_analog_apply(c->analog);
    cont_go(c);
}
/* Wait for the running dump, copy it to c->buf and start the next one. */
static bool cont_next(cont_t *c) {
    uint32_t w0 = esp_cpu_get_cycle_count();
    int64_t lim = esp_timer_get_time() + 100000;
    while (!(REG_READ(DUMP_CTRL) & (1u << 18))) { ring_pump(); if (esp_timer_get_time() > lim) return false; }
    uint32_t w1 = esp_cpu_get_cycle_count();
    dump_halt(c->owner);
    memcpy(c->buf, DUMP_BUF, c->n * 4);
    /* A tracking update can change DC during a dump. Recover only while
     * stopped; the completed block can still contain the transition.
     * The recovery delay remains part of the next measured capture gap. */
    if(rx_recalibration_stale()) {
        rx_analog_restore(c->analog);
        prepare_rx();
        rx_analog_apply(c->analog);
    }
    uint32_t cg = c->cyc_go; int64_t tg = c->t_go;
    cont_go(c);
    c->wait_cyc += w1 - w0; c->copy_cyc += esp_cpu_get_cycle_count() - w1;
    c->blk_t0 = tg; c->blk_gap = 0;
    if (c->have_prev) {
        uint32_t span = (cg - c->cyc_prev + c->cps / 2) / c->cps;   /* samples between the two triggers */
        c->blk_gap = span > c->n ? span - c->n : 0;
    }
    c->cyc_prev = cg; c->have_prev = true;
    c->lost += c->blk_gap; if (c->blk_gap > c->maxgap) c->maxgap = c->blk_gap;
    c->blocks++;
    return true;
}
static void cont_stop(cont_t *c) {
    int64_t lim = esp_timer_get_time() + 100000;
    while (!(REG_READ(DUMP_CTRL) & (1u << 18)) && esp_timer_get_time() < lim) {}
    dump_halt(c->owner);
    rx_analog_restore(c->analog);
    cont_last = *c;
}
/* Complete lines on the owning port stop the run; no command executes
 * until the binary queue is drained and END CSPEC has been sent. */
static bool cont_poll(void) {
    char line[48];
    return burst_serial_poll_run_line(line, sizeof(line));
}

static const unsigned hw_rates[6] = {80000000, 40000000, 20000000, 10000000, 8000000, 4000000};
/* Sine table dependency from Soup c5_dsp.h; no DDC/stream demodulator. */
static int16_t sin_tab[1024];
static void dsp_init_tables(void) {
    static bool done;
    if (done) return;
    for (unsigned j = 0; j < 1024; j++) sin_tab[j] = (int16_t)lroundf(16383.0f * sinf(2.0f * (float)M_PI * j / 1024.0f));
    done = true;
}

/* ------------------------------------------ continuous spectrogram (CSPEC) */
/* At stride=1 every sample of a captured block enters a Hann-windowed FFT;
 * at larger strides only the selected FFT windows are processed.
 * navg FFTs form one line, reported as mean and/or max-hold power (a short
 * packet inside a line stays visible in the max line).  Lines never span
 * blocks (nfft*navg divides the block).  Line value u8 = 2*10*log10(|X|^2)
 * (0.5 dB steps), X = FFT of hann*x with x in 10-bit LSB; host orientation
 * (-fs/2..+fs/2, host negates Q) like SPEC. */
/* The CSPEC DSP runs from flash (cache): measured as fast as IRAM in CSPEC,
 * and ~8 KB of IRAM would shrink the largest heap block below the 32 KB
 * block copy + 16 KB ring (-> 4096-word blocks, longer relative gaps). */
#define CS_ATTR
#include "cspec_dsp.h"
static uint32_t cs_cyc[3];   /* per block: fft, lines, frame */
/* One line: navg segments (stride*nfft apart) -> nfft u8 bins (mode 0 mean,
 * 1 max) or mean then max (mode 2) at o.  acc: u64 sums (only when navg >
 * CS_FOLD), a32: u32 partial sums, mx: max; all in FFT output order. */
static void CS_ATTR cs_line(const uint32_t *w, unsigned nfft, unsigned navg, unsigned stride, unsigned mode, unsigned sh,
                              int32_t *x, uint64_t *acc, uint32_t *a32, uint32_t *mx, uint8_t *o, uint32_t *fcyc) {
    uint32_t c0 = esp_cpu_get_cycle_count();
    bool wide = navg > CS_FOLD;
    if (mode != 1) { memset(a32, 0, nfft * 4); if (wide) memset(acc, 0, nfft * 8); }
    if (mode != 0) memset(mx, 0, nfft * 4);
    for (unsigned sg = 0, fold = 0; sg < navg; sg++) {
        cs_segment(w + sg * stride * nfft, x, a32, mx, nfft, sh, mode);
        if (wide && mode != 1 && (++fold == CS_FOLD || sg == navg - 1)) {
            for (unsigned j = 0; j < nfft; j++) { acc[j] += a32[j]; a32[j] = 0; }
            fold = 0;
        }
    }
    *fcyc += esp_cpu_get_cycle_count() - c0;
    unsigned lgn = __builtin_ctz(navg);
    uint8_t *om = mode == 2 ? o + nfft : o;
    for (unsigned hb = 0; hb < nfft; hb++) {
        unsigned p = cs_hpos[hb];
        if (mode != 1) o[hb] = cs_u8f(wide ? (uint32_t)(acc[p] >> lgn) : a32[p] >> lgn, sh);
        if (mode != 0) om[hb] = cs_u8f(mx[p], sh);
    }
}
static bool cspec_run(unsigned div, unsigned nfft, unsigned navg, unsigned mode, unsigned stride) {
    cs_init(nfft);
    unsigned sh = nfft > 64 ? __builtin_ctz(nfft) - 6 : 0;
    unsigned bpp = mode == 2 ? 2 : 1, per_line = nfft * navg * stride, words = CONT_WORDS;
    bool ring = false; int32_t *x = NULL; uint64_t *acc = NULL; uint32_t *mx, *a32; uint8_t *fb = NULL; uint32_t *cbuf = NULL;
    for (; words >= CONT_WORDS / 2 && words >= per_line; words /= 2) {
        cbuf = heap_caps_malloc(words * 4, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        ring = cbuf && ring_open();
        x = heap_caps_malloc(nfft * 8, MALLOC_CAP_8BIT);
        acc = heap_caps_malloc(nfft * 16, MALLOC_CAP_8BIT);
        fb = heap_caps_malloc((words / per_line) * nfft * bpp, MALLOC_CAP_8BIT);
        if (cbuf && ring && x && acc && fb) break;
        if (ring) ring_close();
        free(cbuf); free(x); free(acc); free(fb); cbuf = NULL; ring = false;
    }
    if (!cbuf) { reply("ERR cspec_memory\n"); return false; }
    mx = (uint32_t *)(acc + nfft); a32 = mx + nfft;
    unsigned lines = words / per_line, fbytes = lines * nfft * bpp;
    char h[128];
    snprintf(h, sizeof(h), "OK CSPEC %u %u %u %u %u %u\n", hw_rates[div], nfft, navg, mode, words, lines);
    reply(h);
    memset(cs_cyc, 0, sizeof(cs_cyc));
    static cont_t c;
    cont_start(&c, div, cbuf, words);
    uint32_t seq = 0, overruns = 0; bool stop = false; const char *why = "stop";
    int64_t last_progress = esp_timer_get_time(); unsigned last_tail = 0;
    while (!stop) {
        while (!stop && ring_used() > STREAM_RING / 2) {
            ring_pump();
            if (cont_poll()) stop = true;
            if (st_tail != last_tail) { last_tail = st_tail; last_progress = esp_timer_get_time(); }
            if (esp_timer_get_time() - last_progress > 2000000) { why = "stalled"; stop = true; }
        }
        if (stop) break;
        if (!cont_next(&c)) { why = "capture_error"; break; }
        uint32_t c1 = esp_cpu_get_cycle_count(), f = 0, l = 0;
        for (unsigned ln = 0; ln < lines; ln++) {
            uint32_t a0 = esp_cpu_get_cycle_count();
            cs_line(cbuf + ln * per_line, nfft, navg, stride, mode, sh, x, acc, a32, mx, fb + ln * nfft * bpp, &f);
            l += esp_cpu_get_cycle_count() - a0;
            ring_pump();
        }
        l -= f;
        uint32_t e0 = esp_cpu_get_cycle_count();
        uint32_t crc = esp_rom_crc32_le(0, fb, fbytes);
        int hl = snprintf(h, sizeof(h), "SPL %" PRIu32 " %" PRId64 " %u %u %u %" PRIu32 " %08" PRIx32 "\n",
                          seq, c.blk_t0, lines, nfft, mode, c.blk_gap, crc);
        if (!ring_frame(h, hl, fb, fbytes)) overruns++;
        seq++;
        uint32_t e1 = esp_cpu_get_cycle_count();
        cs_cyc[0] += f; cs_cyc[1] += l; cs_cyc[2] += e1 - e0; c.work_cyc += e1 - c1;
        if (cont_poll()) stop = true;
        if (st_tail != last_tail || !ring_used()) { last_tail = st_tail; last_progress = esp_timer_get_time(); }
        if (esp_timer_get_time() - last_progress > 2000000) { why = "stalled"; break; }
    }
    cont_stop(&c);
    ring_close();
    snprintf(h, sizeof(h), "END CSPEC %" PRIu32 " %" PRIu32 " %" PRIu64 " %" PRIu32 " %s\n", seq, c.blocks, c.lost, overruns, why);
    burst_serial_drain_stop();
    reply(h);
    free(cbuf); free(x); free(acc); free(fb);
    return true;
}

static bool cont_command(const char *line) {
    if (!strcmp(line, "CSTAT?")) {
        const cont_t *c = &cont_last; char h[220]; unsigned b = c->blocks ? c->blocks : 1;
        double duty = c->blocks ? (double)c->blocks * c->n / ((double)c->blocks * c->n + (double)c->lost) : 0;
        snprintf(h, sizeof(h), "CSTAT blocks %" PRIu32 " words %u lost %" PRIu64 " maxgap %" PRIu32 " duty %.4f slips 0 late 0"
                 " copy %" PRIu64 " wait %" PRIu64 " work %" PRIu64 " fft %" PRIu32 " lines %" PRIu32 "\n",
                 c->blocks, c->n, c->lost, c->maxgap, duty, c->copy_cyc / b, c->wait_cyc / b, c->work_cyc / b,
                 cs_cyc[0] / b, cs_cyc[1] / b);
        reply(h); return true;
    }
    unsigned rate, nfft, navg, mode = 1, stride = 1; char extra;
    int fields = sscanf(line, "CSPEC %u %u %u %u %u %c", &rate, &nfft, &navg, &mode, &stride, &extra);
    if (fields >= 3 && fields <= 5) {
        if (rate > 5 || nfft < 16 || nfft > CS_NMAX || (nfft & (nfft - 1)) ||
            !navg || navg > 256 || (navg & (navg - 1)) || !stride || stride > 256 || (stride & (stride - 1)) ||
            nfft * navg * stride > CONT_WORDS / 2 || mode > 2 ||
            (CONT_WORDS / (nfft * navg)) * nfft * (mode == 2 ? 2 : 1) > 8192) {
            reply("ERR cspec_args\n"); return true;
        }
        cspec_run(rate, nfft, navg, mode, stride); return true;
    }
    return false;
}
