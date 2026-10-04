/* Exact unsigned max of int16 complex magnitude squared, four bins per group.
 * void s3_power_max_u32(const int16_t *x, uint32_t *peak, unsigned groups4);
 * Both buffers are 16-byte aligned. No reads beyond groups4 * 16 bytes.
 * CMUL.S16 sel2/3 with SAR=0/16 supplies both halves of I^2+Q^2.
 * The full-int16 edge I=Q=-32768 is set explicitly: hardware otherwise yields
 * INT32_MAX for this edge. Its correct unsigned power is 0x80000000 (biased zero).
 * XOR bias makes signed VMAX.S32 equivalent to unsigned maximum.
 */
    .text
    .align  4
    .global s3_power_max_u32
    .type   s3_power_max_u32, @function
s3_power_max_u32:
    entry   a1, 32
    movi    a5, 1
    slli    a5, a5, 31
    ee.movi.32.q q4, a5, 0
    ee.movi.32.q q4, a5, 1
    ee.movi.32.q q4, a5, 2
    ee.movi.32.q q4, a5, 3
    srli    a6, a5, 16
    or      a6, a6, a5             /* packed I=Q=-32768 */
    ee.movi.32.q q5, a6, 0
    ee.movi.32.q q5, a6, 1
    ee.movi.32.q q5, a6, 2
    ee.movi.32.q q5, a6, 3
    loopnez a4, .Lpower_end
    ee.vld.128.ip q0, a2, 16
    ee.vld.128.ip q3, a3, 0
    ssai    0
    ee.cmul.s16 q1, q0, q0, 2
    ee.cmul.s16 q1, q0, q0, 3
    ssai    16
    ee.cmul.s16 q2, q0, q0, 2
    ee.cmul.s16 q2, q0, q0, 3
    ee.xorq q3, q3, q4
    ee.vsl.32 q2, q2
    ee.orq  q1, q1, q2
    ee.xorq q1, q1, q4
    ee.vcmp.eq.s32 q2, q0, q5
    ee.notq q2, q2
    ee.andq q1, q1, q2             /* exact biased zero for the full-scale pair */
    ee.vmax.s32 q1, q1, q3
    ee.xorq q1, q1, q4
    ee.vst.128.ip q1, a3, 16
.Lpower_end:
    retw.n
    .size   s3_power_max_u32, . - s3_power_max_u32
