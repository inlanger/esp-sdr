#pragma once
/* RX tuning adapted from ESPsoup 085c752e and ESP-SDR e2b35dc5.
 * Keep Soup's driver preparation for upper 5 GHz, then restore the exact MHz
 * with the upstream calibrated path. No diagnostic 802.11p/PLL experiments. */
#include "esp_wifi.h"
extern void phy_set_chanfreq(unsigned mhz, unsigned mode);
extern void phy_set_rf_freq_offset(unsigned crystal, unsigned mhz, int offset);
extern void phy_change_channel(int mhz, int a, int b, int c);
extern unsigned char phy_param[];
static int c5_khz_offset;
static unsigned c5_tune_method = 4; /* 0 direct, 2 driver, 4 auto */
static bool c5_tune_method_valid(unsigned m) { return m == 0 || m == 2 || m == 4; }
static void c5_restore_chan(unsigned mhz, unsigned mode) {
    phy_set_chanfreq(mhz, mode);
    if (c5_khz_offset) phy_set_rf_freq_offset(phy_param[49], mhz, c5_khz_offset);
}
static void c5_set_chan(unsigned mhz, unsigned mode) {
    unsigned method = c5_tune_method;
    if (method == 4) method = mhz > 3000 && !c5_khz_offset ? 2 : 0;
    if (method == 2) {
        unsigned ch = mhz > 3000 ? 140 : mhz < 2412 ? 1 : mhz > 2472 ? 13 : (mhz - 2407 + 2) / 5;
        (void)esp_wifi_set_channel((unsigned char)ch, WIFI_SECOND_CHAN_NONE);
        phy_change_channel((int)mhz, 1, 0, 0);
    } else c5_restore_chan(mhz, mode);
}
#define phy_chip_set_chan c5_set_chan
