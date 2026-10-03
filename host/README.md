# ESP-SDR host protocol module

Install from this checkout: `python -m pip install ./host`. The package has no
runtime dependencies beyond the Python standard library. It does not open ports,
reset a board or issue receiver commands.

```python
from esp_sdr.spectrum import Decoder, Audit
from esp_sdr.iq import Stream

spectrum = Decoder()
audit = Audit(bins=256, detector=0)  # continuous SPEC profiles only
# For each actual byte block read from your transport or a saved stream:
# for kind, record, offset in spectrum.feed(block):
#     audit.add(kind, record)
# After SPECEND: audit.summary(spectrum)

iq = Stream(decimation=64, shift=0, bits=16)
# for record, payload in iq.feed(block):
#     ...  # store the payload together with its sample index and stream offset
# After IQSEND: iq.summary()
```

`Decoder` understands SPC1 spectra, SPS1 telemetry and SPECEND. It preserves wire
units, flags and byte offsets. `Audit` is deliberately a continuous-profile audit:
snapshot flag bit 3 and snapshot gaps do not pass its integrity criterion. Decode
snapshot records with `Decoder`, then account for their declared acquisition gaps
in the caller. Do not apply the continuous verdict to a snapshot profile.

`Stream` handles IQS1 with **8 or 16 bits**, CRC, sample indices, output drops,
input loss and IQSEND. It is extracted from the measured S3 collector. It does
not implement packed IQ4. Consume the entire iterator returned by `feed()`.
`recover_end()` may be called only after receive idle (the S3 collector waits
at least 250 ms): it recognizes a complete plausible IQSEND behind a truncated
last frame. Discarded bytes remain counted as loss. Command access requires a
confirmed end; a missing end requires reconnection, never speculative commands.

The package does not choose RF frequency, bandwidth, gain, USB pins, sample rate
or a DSP backend. Query CAPS, LIMITS?, TRANSPORT? and SPECINFO? in board-specific
control code. The existing S3 control implementation is in [tools/s3](../tools/s3/README.md).

Wire references: [spectrum](../docs/spectrum.md), [IQ stream](../docs/iq-stream.md),
[firmware declarations](../components/sdr_protocol/include/sdr_protocol.h).
Packaging follows the [setuptools pyproject documentation](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html).
