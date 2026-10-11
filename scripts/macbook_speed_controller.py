#!/usr/bin/env python3
"""Run the authorized clean-boot MTPLX speed phase; retain the engine for DSH."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    private = root / '.private'
    python = private / 'mtplx-venv/bin/python'
    checkpoint = private / 'mtplx-models/Youssofal--Qwen3.8-Flash-Next-MTPLX-Optimized-Speed'
    assert not (root / 'data/raw/speed/macbook-m5max').exists(), 'Preserve existing measurement'
    authorization = json.loads((private / 'macbook-clean-boot.json').read_text())
    assert authorization['owner_confirmed_clean_restart']
    state_file = private / 'macbook-controller-state.json'
    command = [str(private / 'mtplx-venv/bin/mtplx'), 'serve', '--model', str(checkpoint),
               '--profile', 'turbo', '--depth', '3', '--mtp', '--host', '127.0.0.1',
               '--port', '19380', '--no-auth', '--memory-limit', '120GiB',
               '--scheduler-mode', 'serial', '--max-active-requests', '1',
               '--ssd-session-cache', 'off', '--agent-rewrites', 'off',
               '--reasoning', 'auto', '--no-stats-footer', '--warmup-tokens', '0',
               '--context-window', '32768', '--max-tokens', '16384', '--fan-mode', 'default']
    state = {'phase': 'starting', 'controller_pid': os.getpid(), 'engine_command': command}

    def save(phase, **fields):
        state.update(fields)
        state.update(phase=phase, updated=datetime.datetime.now(datetime.timezone.utc).isoformat())
        state_file.write_text(json.dumps(state, indent=2) + '\n')
        print(json.dumps({'phase': phase, **fields}), flush=True)

    with (private / 'macbook-engine.log').open('x') as engine_log:
        engine = subprocess.Popen(command, stdout=engine_log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, start_new_session=True)
        save('starting', engine_pid=engine.pid)
        try:
            deadline = time.monotonic() + 1500
            while True:
                if engine.poll() is not None:
                    raise RuntimeError('MTPLX exited during startup; see engine log')
                try:
                    with urllib.request.urlopen('http://127.0.0.1:19380/health', timeout=10) as response:
                        health = json.load(response)
                    with urllib.request.urlopen('http://127.0.0.1:19380/v1/models', timeout=10) as response:
                        models = json.load(response)
                    if models.get('data'):
                        (private / 'macbook-health-ready.json').write_text(json.dumps(health, indent=2)+'\n')
                        break
                except (OSError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError('MTPLX readiness deadline exceeded')
                time.sleep(5)
            save('speed_running')
            with (private / 'macbook-speed.log').open('x') as speed_log:
                speed = subprocess.Popen([str(python), str(root / 'scripts/mtplx_speed.py'),
                    '--root', str(root), '--checkpoint', str(checkpoint)],
                    stdout=speed_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                save('speed_running', speed_pid=speed.pid)
                result = speed.wait(timeout=10800)
            if result != 0:
                raise RuntimeError('Speed measurement failed; preserve logs')
            summary = json.loads((root / 'data/raw/speed/macbook-m5max/summary.json').read_text())
            assert summary['repetitions'] == 5
            save('speed_complete_dsh_pending', summary=summary, engine_retained_for_dsh=True)
        except BaseException as exc:
            save('failed', error=str(exc))
            engine.terminate()
            try:
                engine.wait(timeout=90)
            except subprocess.TimeoutExpired:
                save('failed', error=str(exc), engine_stop_timeout=True)
            raise


if __name__ == '__main__':
    main()
