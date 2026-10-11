"""Run one isolated, pinned DSH25 fixture through its own recording gateway."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import urllib.request
import time

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--tools', type=Path, required=True)
    ap.add_argument('--case', required=True)
    ap.add_argument('--upstream', required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--port', type=int, required=True)
    a = ap.parse_args()
    out = a.root / 'data/raw/evaluation' / a.case
    fixture = a.root / '.private/dsh' / a.case
    state_file = a.root / '.private' / (a.case + '-dsh-state.json')
    assert not state_file.exists(), 'Preserve a prior execution'
    assert not list((out/'individual').glob('*/receipt.json'))
    prepared = json.loads((out/'prepared.json').read_text())
    assert prepared['DSH_version'] == '0.1.5-rc.3'
    assert prepared['revisions'] == {'ts_bench':'2b3bc944eb5728b5cb24d00e19371b595d528847', 'exercism':'ab2d84eda572061e10a8e1ac8c1a662fa859284e'}
    assert prepared['exercises_sha256'] == hashlib.sha256((a.root/'data/exercises.txt').read_bytes()).hexdigest()
    assert f'127.0.0.1:{a.port}/v1' in (fixture/'dsh.patch.yml').read_text()
    with socket.socket() as s: s.bind(('127.0.0.1', a.port))
    with urllib.request.urlopen(a.upstream+'/v1/models', timeout=15) as r: models = json.load(r)
    assert any(x['id']==a.model for x in models['data'])
    (out/'engine-models.json').write_text(json.dumps(models, indent=2))
    children = []
    def save(phase, **fields):
        state_file.write_text(json.dumps(dict(phase=phase,pid=os.getpid(),case=a.case,
            updated=datetime.now(timezone.utc).isoformat(),**fields),indent=2)+'\n')
    def interrupt(*_): raise InterruptedError('DSH controller interrupted')
    signal.signal(signal.SIGTERM, interrupt); signal.signal(signal.SIGINT, interrupt)
    try:
        gateway = subprocess.Popen([sys.executable,'-u',str(a.root/'scripts/dsh_gateway_vllm_context.py'),
            '--upstream',a.upstream,'--model',a.model,'--port',str(a.port),'--output',str(out/'api')])
        children.append(gateway)
        time.sleep(1)
        assert gateway.poll() is None
        runner = subprocess.Popen([sys.executable,'-u',str(a.root/'scripts/run_dsh.py'),
            '--root',str(a.root),'--tools',str(a.tools),'--case',a.case], start_new_session=True)
        children.append(runner)
        save('running',gateway_pid=gateway.pid,runner_pid=runner.pid,gateway_port=a.port)
        deadline=time.monotonic()+12*3600
        while runner.poll() is None:
            assert not list((out/'api').glob('*/error.json')), 'Gateway rejected a request; stop immediately and retain invalid run'
            assert gateway.poll() is None, 'Gateway exited'
            assert time.monotonic()<deadline, 'Suite deadline'
            time.sleep(3)
        assert runner.returncode==0, 'DSH stopped; evidence retained, no score published'
        summary=json.loads((out/'summary.json').read_text())
        assert summary['state']=='complete' and summary['total']==25
        assert not list((out/'api').glob('*/error.json'))
        assert all(x['result_present'] and not x.get('infrastructure_failure') and not x['outer_timeout'] for x in summary['rows'])
        save('complete_collection_pending',passed=summary['passed'],total=25)
    except BaseException as error:
        save('failed_evidence_retained',error=repr(error));raise
    finally:
        for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,signal.SIG_IGN)
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=40)
                except subprocess.TimeoutExpired:child.kill();child.wait()

if __name__=='__main__':main()
