# C5 receiver in this fork

`FWOPT?` identifies this receiver as `inlanger-c5-1`, not an ESPsoup release.
The implementation is a receive-only subset adapted from
[ESPsoup 085c752e](https://github.com/pit711/espsoup-firmware/tree/085c752e51127832f1f9259efe4a60a8ec0f8635)
with C5 DC calibration from
[ESP-SDR e2b35dc5](https://github.com/ESPARGOS/esp-sdr/tree/e2b35dc593a378e4c5eb69cb8db59313117c9838)
(prerequisite `7876b65`, fixes `77ffc12` and `5ede49b`). GPL-3.0-or-later;
Soup copyright notices are retained in the imported engine and FFT code.
This is adaptation of those implementations, not a new capture or FFT design.

Build with the `esp32c5` target and the SDK revision in `firmware-targets.json`.
The measured-gap calculation requires the existing fixed 240 MHz CPU setting.
This code adds no board GPIO/antenna-switch setup. XIAO uses its external antenna.

## Commands

Existing `C5SDR 6 burst 16380`, `DATA`, `SPEC`, `SPC1` and `SPS1` wire formats
stay unchanged. `CAPS` advertises only the implemented additions:
`BIGCAP CAPT FREQK FWOPT CONT CSPEC TUNEM`. `LIMITS?` additionally reports
`maxSamples:32704`, `freqStepHz:1000` and supported CSPEC FFT sizes/modes.
Do not infer a working RF band from the permissive `RANGE?` software bounds.

| Command | Result |
|---|---|
| `CAP`, `CAP16`, `CAP20`, `RXRUN` | Up to 32704 pairs, fast register capture by default; existing `DATA` header. |
| `CAPT16 n rate`, `CAPT20 n rate` | `DATAT n crc us t0_us seq lag` followed by IQ8 or packed IQ10. `t0_us` is the dump trigger timestamp; `lag` leading samples were discarded for two-half captures. |
| `ENGINE FAST`, `ENGINE STOCK`, `ENGINE?` | Compare register capture with vendor capture. STOCK applies only to ordinary captures ≤16380; CAPT and larger captures always use FAST. |
| `FREQK kHz`, `FREQK?` | Integer-kHz LO request. `FREQ MHz` resets the fractional offset. |
| `TUNEM 0/2/4`, `TUNEM?` | Direct / Wi-Fi driver preparation / auto (default). Auto uses driver preparation above 3000 MHz unless fractional offset is nonzero. |
| `CSPEC rate nfft navg [mode [stride]]` | Continuous block capture and spectrum, with gaps; mode 0 mean, 1 max (default), 2 both. |
| `CSTAT?` | Last CSPEC capture counters and cycle costs. |

Rate indices 0–5 correspond to 80/40/20/10/8/4 MS/s. These lower hardware
rates drop samples, with no digital antialias filter. CSPEC `nfft` is a power
of two from 16 to 256; `navg` and `stride` must also be powers of two, with
`nfft*navg*stride <= 4096`. Output frame size is bounded by the transmit ring.

The tuning sequence combines Soup's upper-5-GHz driver preparation with the
upstream exact-frequency DC refresh and final channel restore. Diagnostic
TUNEM modes 3/5 and the old reference-channel workaround are not retained.
Forced TUNEM 2 rejects a nonzero FREQK residual (and vice versa), rather than
silently ignoring it. DC is measured at the rounded integer MHz; exact-kHz
DC calibration and the usable RF range of this combined path remain hardware
measurement questions. No sensitivity or 5.8 GHz reception claim follows
from a successful command response.

## CSPEC framing and accounting

Start reply: `OK CSPEC fs nfft navg mode words lines`. Each frame:
`SPL seq t_us lines nfft mode gap crc`, followed by `lines*nfft` bytes
(or twice that for mode 2: each line's mean bins followed by max bins).
Bins have the Soup frequency orientation, from −fs/2 to +fs/2 with host Q
negation. Values are relative power in 0.5 dB steps, including the FFT shift
compensation; they are not calibrated dBm.

`gap` counts estimated missing input samples before that block from the CPU
trigger-cycle difference. `CSTAT duty` is captured samples divided by captured
plus gap samples; processing fraction is additionally divided by `stride`.
Frame sequence skips / END overruns report transport drops separately.
CSTAT `slips` and `late` stay zero: no multi-board CGRID scheduler is included.

Send `STOP\n`, keep reading every complete binary payload until
`END CSPEC seq blocks lost overruns reason`, then send configuration commands.
Any complete line on the owning port requests stop and is consumed, not
executed. A host that stalls/disconnects can lose the last partial frame and
must resynchronize/reconnect; it must not assume the binary stream ended.

Background PHY DC measurements are detected through the upstream generation
wrapper. CSPEC refreshes DC only after releasing the dump bank, before the
next trigger. That pause is included in the next gap. The already completed
block can contain a calibration transition: this does not guarantee that every
returned block was calibrated throughout its capture. Startup IQ coefficients
are retained; user retunes do not run transmitting IQ loopback calibration.
The ordinary SDK startup calibration is unchanged.

## Local integration fixes and limits

- Two-half copies for 16381–16383 pairs avoid an unsigned tail-length underflow
  in the imported engine. Maximum-size 32704 captures use the same Soup algorithm.
- The run-line accumulator is cleared on stop so a partial old line cannot
  leak into a subsequent CSPEC run.
- Large or malformed CSPEC/FREQK arguments are bounded before multiplication.
- No DDC, STREAM, CPKT, VFM, GFSK, PARLIO, TX or Soup application code is included.
  Existing S3 behavior and P4 firmware are outside this change.

Hardware results and firmware hashes live in the project's existing local R&D
report, not in this source repository. Host protocol checks establish framing
and delivery; RF sensitivity needs a known source.
