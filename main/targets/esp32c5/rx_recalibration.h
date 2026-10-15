#pragma once
#include <stdbool.h>
/* C5 only. Refresh DC with captures stopped; retain startup IQ correction. */
void rx_recalibrate(unsigned mhz);
bool rx_recalibration_stale(void);
