# S3 receive-only R&D tools

These are the measured local S3 scripts, now versioned in the firmware fork.
Their wire decoders live in the installable [host package](../../host/README.md).
The control code deliberately still requires S3 burst protocol 6, uses integer
2400–2483 MHz tuning, and checks advertised gain/bandwidth limits. IQS collection
is restricted to the measured 16 MS/s input, decimation 64/128, IQ8/IQ16 modes.
The package itself neither selects those settings nor opens serial ports.

From the repository root, with Python 3.10 or later:

```sh
python -m pip install ./host
python -m pip install -r tools/s3/requirements.txt
python tools/s3/capture.py identity --port "$S3_PORT" --out "$RUN/identity"
python tools/s3/trace_profiles.py --port "$S3_PORT" --out "$RUN/profiles"
python tools/s3/capture_iqs.py --port "$S3_PORT" --out "$RUN/iq16"   --decimation 64 --bits 16 --shift 0 --mode 2 --seconds 600   --frequency 2438 --bandwidth 13 --gain 40
python tools/s3/capture.py spectrum --port "$S3_PORT" --out "$RUN/spectrum"   --frequency 2442 --bandwidth 13 --gain 40 --profile 0 --seconds 600
python tools/s3/replay_spectrum.py "$RUN/spectrum" --out "$RUN/replay.json"
```

Set S3_PORT to the actually enumerated port and RUN to a new local experiment
directory **outside the checkout**. Every output directory must be new. Profile 0
in the example must be checked against that receiver's saved SPECINFO response.
Save the firmware commit, host commit/package files and environment versions with
each run. Native USB is required for S3 SPEC/IQS. Opening the observed serial port
resets this board; commands wait for boot and a matching SYNC nonce.

- `capture.py`: identity, 20 separate IQ snapshots, one continuous SPEC run, or
  20 stop/configure/restart cycles. Snapshots are never labeled continuous IQ.
- `capture_iqs.py`: IQ8/IQ16 streaming with 4096–65536-byte host reads, raw bytes,
  payload export, per-frame indices, CRC/loss accounting and confirmed IQSEND.
- `trace_profiles.py`: runs the unchanged repository `tools/check_spectrum.py`
  for every advertised profile/detector, with raw serial evidence on failure.
- `check_profiles.py`: alternate wrapper using a fresh identity file and saving
  upstream stdout/stderr. An aborted matrix does not pass unreported profiles.
- `stall_probe.py --port PORT --out DIR`: deliberately stops reading IQ16/125k
  for 5 seconds, checks automatic end and command/SPEC recovery in the same
  session. The paused IQ capture is expected to lose data; recovery is a separate
  verdict. It uses LO 2438 MHz, BW 13 MHz and manual gain index 40.
- `analyze_iq.py`, `analyze_spectrum.py`, `replay_iq.py`, `replay_spectrum.py`:
  analysis/replay of measured data in original units. No calibrated dBm claims.
- `event.py`: timestamp an operator-confirmed source action; does not control it.
- `uart_diagnostics.py`: bounded RING/IQ10 diagnostics. RING bank statistics do
  not replace a continuous SPEC measurement.
- `darwin_baud64.py`: observed macOS WCH baud-rate compatibility workaround.
- `restart_check.py`: UART EN reset check, requires the separately installed
  esptool 5.4. This is not a physical USB disconnect/reconnect test.

Raw stream, IQ payload, index CSV and command/boot logs belong together. A payload
file with missing sample indices must not be interpreted as a gapless recording.
After CRC resynchronization, loss stays in the report. No ordinary commands are
sent until a valid SPECEND/IQSEND is established; otherwise reconnect.

Experiment journals, reports and recordings stay local and are excluded from Git.
These commands are the maintained copies for subsequent experiments.
