#!/usr/bin/env python3
"""Wait for exact speed completion, then run the authorized M3U DSH phase."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    root = ap.parse_args().root.resolve()
    private = root / '.private'
    remote = '<HOME>/llm-lab/benchmarks/Qwen3.8FN_compare'
    tunnel = None
    engine_pid = None
    state_file = private / 'macbook-dsh-dispatch-state.json'
    state = {'dispatcher_pid': os.getpid()}

    def save(phase, **fields):
        state.update(fields)
        state.update(phase=phase, updated=datetime.now(timezone.utc).isoformat())
        state_file.write_text(json.dumps(state, indent=2)+'\n')
        print(json.dumps({'phase': phase, **fields}), flush=True)

    try:
        save('waiting_for_speed')
        deadline = time.monotonic() + 10800
        while True:
            speed_state = json.loads((private / 'macbook-controller-state.json').read_text())
            if speed_state['phase'] == 'failed':
                raise RuntimeError('Speed failed; DSH will not start')
            if speed_state['phase'] == 'speed_complete_dsh_pending':
                break
            if time.monotonic() > deadline:
                raise TimeoutError('Speed completion deadline exceeded')
            time.sleep(10)
        raw = root / 'data/raw/speed/macbook-m5max'
        assert json.loads((raw/'summary.json').read_text())['repetitions'] == 5
        for folder in [*[f'warmup-{i:02d}' for i in range(1, 4)],
                       *[f'trial-{i:02d}' for i in range(1, 6)]]:
            row = json.loads((raw/folder/'measurement.json').read_text())
            assert row['valid'] and row['input_tokens'] == 16384
            assert row['output_tokens'] == 2048 and row['reused_tokens'] == 0
        engine_pid = speed_state['engine_pid']
        with (private/'macbook-dsh-tunnel.log').open('x') as log:
            tunnel = subprocess.Popen(['ssh', '-N', '-o', 'BatchMode=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ExitOnForwardFailure=yes',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                '-R', '127.0.0.1:19380:127.0.0.1:19380', 'M3U'],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            save('tunnel_starting', tunnel_pid=tunnel.pid)
            time.sleep(2)
            assert tunnel.poll() is None, 'Reverse tunnel failed'
            command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', 'M3U',
                f'python3 -u {remote}/scripts/macbook_dsh.py --root {remote} '
                '--tools <HOME>/llm-lab/benchmarks/ts-bench-v1/tools']
            with (private/'macbook-dsh-controller.log').open('x') as dsh_log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                         stdout=dsh_log, stderr=subprocess.STDOUT)
                save('dsh_running', ssh_controller_pid=child.pid)
                result = child.wait(timeout=43200)
            if result != 0:
                raise RuntimeError('Remote DSH failed; preserve all evidence')
            save('dsh_complete_collection_pending')
    except BaseException as error:
        save('failed', error=repr(error))
        raise
    finally:
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            tunnel.wait(timeout=30)
            state['tunnel_stopped'] = True
        if engine_pid is not None:
            command = subprocess.run(['ps', '-p', str(engine_pid), '-o', 'command='],
                                     text=True, capture_output=True).stdout
            expected = str(private/'mtplx-venv/bin/mtplx')
            if expected in command and '--port 19380' in command:
                os.kill(engine_pid, signal.SIGTERM)
                state['engine_stop_requested'] = True
            elif not command.strip():
                state['engine_already_stopped'] = True
            else:
                state['engine_stop_command_mismatch'] = True
        state_file.write_text(json.dumps(state, indent=2)+'\n')


if __name__ == '__main__':
    main()
