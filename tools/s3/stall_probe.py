"""Hardware-only check: pause reading IQS, then recover SPEC in the same session."""
import argparse
from pathlib import Path
from types import SimpleNamespace
import sys
import time

from capture import Receiver, save, spectrum, utc
from esp_sdr.iq import Stream

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', required=True)
parser.add_argument('--out', type=Path, required=True)
args_cli = parser.parse_args()
root = args_cli.out
root.mkdir(parents=True, exist_ok=False)
receiver = None
result = {'utc_start': utc(), 'read_pause_seconds': 5, 'fallback_stop_sent': False,
          'automatic_end': False, 'command_after': False, 'same_session_spectrum_pass': False}
decoder = Stream(128, 0, 16)
try:
    receiver = Receiver(args_cli.port, root)
    receiver.identify(root)
    args = SimpleNamespace(frequency=2438, bandwidth=13, gain=40, profile=0, detector=0, seconds=3)
    save(root/'controls.json', receiver.configure(args))
    receiver.streaming = True
    receiver.write(b'IQS 0 128 16 6 0 2\n')
    result['start_reply'] = receiver.line()
    assert result['start_reply'] == 'IQS 16000000 128 16 0 2 2438'
    started = time.monotonic()
    last_data = started
    paused = False
    with (root/'iqs-stream.bin').open('xb') as raw:
        while decoder.end is None:
            data = receiver.read(min(16384, max(1, receiver.port.in_waiting)))
            raw.write(data)
            for record, payload in decoder.feed(data):
                pass
            if data:
                last_data = time.monotonic()
            elif time.monotonic() - last_data >= .25:
                decoder.recover_end()
            if decoder.frames >= 10 and not paused:
                result['pause_utc'] = utc()
                result['pause_after_frames'] = decoder.frames
                time.sleep(5)
                result['resume_utc'] = utc()
                paused = True
            if decoder.end is None and time.monotonic() - started > 10 and not result['fallback_stop_sent']:
                receiver.write(b'\n')
                result['fallback_stop_sent'] = True
            if time.monotonic() - started > 15:
                raise TimeoutError('No IQSEND; no ordinary commands issued')
    receiver.streaming = False
    result['automatic_end'] = bool(paused and not result['fallback_stop_sent']
                                    and decoder.end['stopped_by_host'])
    result['command_after'] = receiver.command('INFO') == receiver.identity
    out = root/'spectrum-after-stall'
    out.mkdir()
    save(out/'controls.json', receiver.configure(args))
    result['same_session_spectrum'] = spectrum(receiver, args, out)
    result['same_session_spectrum_pass'] = result['same_session_spectrum']['pass']
    receiver.ok('RELEASE')
except Exception as error:
    result['error'] = f'{type(error).__name__}: {error}'
finally:
    if receiver:
        receiver.close()
    result.update(utc_end=utc(), iq_stream=decoder.summary())
    result['pass'] = bool(result['automatic_end'] and result['command_after']
                          and result['same_session_spectrum_pass'] and not result.get('error'))
    save(root/'result.json', result)
    print(result, flush=True)
sys.exit(0 if result['pass'] else 1)
