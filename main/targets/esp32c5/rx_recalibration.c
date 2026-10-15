/* C5-only extraction from ESPARGOS/esp-sdr e2b35dc593a378e4c5eb69cb8db59313117c9838:
 * 7876b65 receiver DC refresh, 77ffc12 startup IQ preservation, 5ede49b recovery
 * after background PHY DC measurements. Original project: GPL-3.0-or-later. */
#include "rx_recalibration.h"
#include "esp_private/phy.h"
#include "soc/soc.h"
#include <sys/lock.h>
#include <stdatomic.h>
#include <stdint.h>

static unsigned measurement_mhz;
static atomic_uint dc_generation;
static unsigned calibrated_generation;
static bool calibrated;
extern unsigned char phy_param[];
extern void __real_phy_chip_set_chan_ana(unsigned);
extern void phy_set_rx_gain_cal_dc(unsigned, unsigned, void *, void *);
extern void phy_adc_rate_cal_rxdc(void);
extern void phy_set_rx_gain_table(unsigned, unsigned);

void __wrap_phy_chip_set_chan_ana(unsigned mhz) {
    __real_phy_chip_set_chan_ana(measurement_mhz ? measurement_mhz : mhz);
}

bool rx_recalibration_stale(void) {
    return !calibrated || atomic_load(&dc_generation) != calibrated_generation;
}

void rx_recalibrate(unsigned mhz) {
    _lock_t lock = phy_get_lock();
    _lock_acquire(&lock);
    measurement_mhz = mhz;
    REG_CLR_BIT(0x600a702cu, 1u << 23); /* Release forced gain during the DC sweep. */
    __real_phy_chip_set_chan_ana(mhz);
    unsigned band = phy_param[42];
    uint32_t *flags = (void *)(phy_param + 148);
    /* Keep IQ-valid bit 0x400. Clearing it would enable transmitting loopback
     * calibration at a user retune. DC measurements themselves are RX-only. */
    *flags &= ~0x280u;
    if (band) {
        phy_set_rx_gain_cal_dc(0, 1, phy_param + 384, phy_param + 912);
    } else {
        phy_set_rx_gain_cal_dc(0, 1, phy_param + 312, phy_param + 896);
        phy_adc_rate_cal_rxdc();
        phy_set_rx_gain_cal_dc(1, 1, phy_param + 348, phy_param + 904);
    }
    *flags |= 0x480u;
    phy_set_rx_gain_table(mhz, 0);
    measurement_mhz = 0;
    calibrated_generation = atomic_load(&dc_generation);
    calibrated = true;
    _lock_release(&lock);
}

/* The pinned C5 PHY's four word-sized arguments are forwarded unchanged. */
extern void __real_phy_set_rx_gain_cal_dc(uintptr_t, uintptr_t, uintptr_t, uintptr_t);
void __wrap_phy_set_rx_gain_cal_dc(uintptr_t a, uintptr_t b, uintptr_t c, uintptr_t d) {
    __real_phy_set_rx_gain_cal_dc(a, b, c, d);
    atomic_fetch_add(&dc_generation, 1);
}
