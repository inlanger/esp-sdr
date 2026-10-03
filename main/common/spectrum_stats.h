/* Optional SPS1 wire telemetry shared by snapshot and scalar ring backends. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "sdr_protocol.h"
typedef struct {
    int64_t at;
    uint64_t busy;
    uint32_t ffts,heap_free,heap_largest,cycles_per_us;
} spectrum_stats_t;
void spectrum_stats_init(spectrum_stats_t *s);
void spectrum_stats_emit(spectrum_stats_t *s,unsigned n,unsigned fs,uint32_t ffts,
                         uint32_t abandoned,uint32_t drops,uint32_t late,unsigned queue,
                         bool (*send)(const void *,size_t));
