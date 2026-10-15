/* The modem owns this entire SRAM bank while adctrig runs. Heap reservation
 * alone does not prevent a future firmware's static sections growing into it. */
ASSERT(_iram_end <= 0x40820000, "C5 RF dump overlaps IRAM")
ASSERT(_data_end <= 0x40820000, "C5 RF dump overlaps data")
ASSERT(_bss_end <= 0x40820000, "C5 RF dump overlaps BSS")
/* Soup's snapshot spectrum workspace: the upper quarter of the already
 * reserved bank, above SPEC captures (<=2048 words). BIGCAP may overwrite it;
 * spectrum.c rebuilds its FFT table/window before every SPEC run. */
SECTIONS {
    .c5_spectrum 0x40838000 (NOLOAD) : ALIGN(16) {
        *(.c5_spectrum)
        _c5_spectrum_end = ABSOLUTE(.);
    }
}
ASSERT(SIZEOF(.c5_spectrum) <= 0x8000, "C5 spectrum workspace exceeds reserved bank slot")
ASSERT(_c5_spectrum_end <= 0x40840000, "C5 spectrum workspace exceeds dump bank")
