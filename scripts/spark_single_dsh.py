#!/usr/bin/env python3
"""Harness-host runner; no permanent SSH credentials to the inference host."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--tools', type=Path, required=True)
    ap.add_argument('--host', required=True)
    args = ap.parse_args()
    key = (args.root / '.private/control-key').read_text().strip()

    def control(body=None):
        req = urllib.request.Request('http://' + args.host + ':18889' + ('/finish' if body is not None else '/status'),
              data=json.dumps(body).encode() if body is not None else None,
              headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    started = time.monotonic()
    while True:
        try:
            state = control()
            phase = state['phase']
            if phase == 'dsh_ready':
                break
            if phase in ('benchmark_failed', 'restoring_deepseek', 'failed_restored', 'restoration_failed'):
                raise RuntimeError('Inference controller failed: ' + repr(state))
        except OSError:
            if time.monotonic() - started > 7200:
                raise
        if time.monotonic() - started > 7200:
            raise RuntimeError('Speed phase exceeded readiness deadline')
        time.sleep(5)
    gateway = None
    success = False
    try:
        gateway = subprocess.Popen([sys.executable, '-u', str(args.root / 'scripts/dsh_gateway.py'),
           '--upstream', 'http://' + args.host + ':18888', '--model', 'Qwen3.8-Flash-Next',
           '--output', str(args.root / 'data/raw/evaluation/spark-single/api')])
        time.sleep(1)
        assert gateway.poll() is None, 'Gateway failed'
        print('Starting DSH25 spark-single', flush=True)
        result = subprocess.run([sys.executable, '-u', str(args.root / 'scripts/run_dsh.py'), '--root', str(args.root),
                   '--tools', str(args.tools), '--case', 'spark-single'])
        success = result.returncode == 0
    finally:
        if gateway is not None and gateway.poll() is None:
            gateway.terminate()
            gateway.wait(timeout=30)
        control({'success': success})
    if not success:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
