#!/usr/bin/env python3
"""Run the fixed DSH25 suite through an existing temporary loopback tunnel."""
import argparse
from datetime import datetime, timezone
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
    ap.add_argument('--upstream', default='http://127.0.0.1:18731')
    args = ap.parse_args()
    output = args.root / 'data/raw/evaluation/ms-s1max'
    assert not (output / 'summary.json').exists(), 'Preserve existing complete results'
    assert not list((output / 'individual').glob('*/receipt.json')), 'Preserve prior attempts'
    with urllib.request.urlopen(args.upstream + '/health', timeout=15) as r:
        health = json.load(r)
    assert health['status'] == 'ok' and health['version']['engine'] == '0.17.5'
    assert health['chat_template']['thinking_control'] and health['slots'] == 1
    (output / 'engine-health.json').write_text(json.dumps(health, indent=2))
    state_path = args.root / '.private/ms-s1-dsh-controller-state.json'

    def state(phase, **fields):
        data = dict(phase=phase, updated=datetime.now(timezone.utc).isoformat(), **fields)
        state_path.write_text(json.dumps(data, indent=2))
        print(json.dumps(data), flush=True)

    gateway = None
    try:
        gateway = subprocess.Popen([sys.executable, '-u', str(args.root / 'scripts/dsh_gateway.py'),
            '--upstream', args.upstream, '--model', 'Qwen3.8-Flash-Next',
            '--output', str(output / 'api')])
        time.sleep(1)
        assert gateway.poll() is None, 'Gateway failed'
        state('running', gateway_pid=gateway.pid)
        result = subprocess.run([sys.executable, '-u', str(args.root / 'scripts/run_dsh.py'),
            '--root', str(args.root), '--tools', str(args.tools), '--case', 'ms-s1max'])
        assert result.returncode == 0, 'DSH failed; evidence retained'
        summary = json.loads((output / 'summary.json').read_text())
        assert summary['state'] == 'complete' and summary['total'] == 25
        state('complete', passed=summary['passed'], total=25)
    except BaseException as error:
        state('failed', error=repr(error))
        raise
    finally:
        if gateway is not None and gateway.poll() is None:
            gateway.terminate()
            gateway.wait(timeout=30)


if __name__ == '__main__':
    main()
