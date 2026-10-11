#!/usr/bin/env python3
"""Inference-host controller, including automatic DeepSeek restoration."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from strata_speed import save

ROOT = Path(__file__).resolve().parents[1]
RECIPE = Path('<HOME>/llm/qwen38-single-benchmark')
PRIVATE = ROOT / '.private'
KEY = (PRIVATE / 'control-key').read_text().strip()
STATE = {'phase': 'starting'}
FINISHED = threading.Event()


def state(phase, **fields):
    STATE.update(phase=phase, updated=datetime.now(timezone.utc).isoformat(), **fields)
    save(PRIVATE / 'controller-state.json', STATE)
    print(json.dumps(STATE), flush=True)


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=kwargs.pop('timeout', 180), **kwargs)


def gx(*args):
    return run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'gx10-65ce.local', *args])


def health(url, timeout):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.load(r)
            if r.status == 200:
                return data
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError('Service readiness timeout: ' + url)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self):
        return self.client_address[0] in ('<PRIVATE_IP>', '127.0.0.1', '<PRIVATE_IP>') and self.headers.get('Authorization') == 'Bearer ' + KEY

    def do_GET(self):
        if not self.allowed():
            return self.respond(403, {'error': 'forbidden'})
        self.respond(200, STATE)

    def do_POST(self):
        if not self.allowed() or self.path != '/finish':
            return self.respond(403, {'error': 'forbidden'})
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        STATE['dsh_result'] = body
        FINISHED.set()
        self.respond(200, {'accepted': True})


def interrupted(*_):
    raise SystemExit('Interrupted; restoring DeepSeek')


def main():
    (ROOT / 'data/raw/speed/spark-single').mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    server = ThreadingHTTPServer(('<PRIVATE_IP>', 18889), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Existing inference containers are retained, so restoration uses their exact configuration.
    head = subprocess.check_output(['docker', 'inspect', 'dsv41-exl3-head'])
    worker = subprocess.check_output(['ssh', 'gx10-65ce.local', 'docker inspect dsv41-exl3-worker'])
    save(PRIVATE / 'deepseek-head-before.json', json.loads(head))
    save(PRIVATE / 'deepseek-worker-before.json', json.loads(worker))
    assert json.loads(head)[0]['State']['Running'] and json.loads(worker)[0]['State']['Running']
    touched = False
    failure = None
    try:
        touched = True
        state('stopping_deepseek')
        run(['docker', 'stop', '-t', '60', 'dsv41-exl3-head'])
        gx('docker stop -t 60 dsv41-exl3-worker')
        env = dict(os.environ, IMAGE='ghcr.io/miaai-lab/qwen3.8-flash-next-single-dgx-spark-tensorfold:zig-db28187-4144696f21e3',
                   HF_CACHE=str(RECIPE / 'hf-cache'), PREPARE='0', VISION='0', PARALLEL='1',
                   CONTEXT='131072', MAX_TOKENS='16384', PORT='18888', HOST='<PRIVATE_IP>',
                   CONTAINER_NAME='qwen38-single-benchmark-tf',
                   STATE_DIR=str(PRIVATE / 'recipe-state'), LOG_DIR=str(PRIVATE / 'recipe-logs'))
        state('loading_tensorfold')
        with (PRIVATE / 'recipe-start.log').open('w') as log:
            run(['bash', './start.sh'], cwd=RECIPE, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=2100)
        folder = ROOT / 'data/raw/speed/spark-single'
        folder.mkdir(parents=True, exist_ok=True)
        settings = {k: env[k] for k in ('IMAGE', 'PARALLEL', 'VISION', 'CONTEXT', 'MAX_TOKENS')}
        settings['timing_definition'] = {'prefill': 'prompt_tokens / tensorfold.prefill_seconds',
            'decode': 'native tensorfold.tokens_per_second = (generated_tokens - 1) / internal elapsed seconds from prefilled_ns to finished_ns; prefill_seconds and total seconds have separate clock scopes',
            'cached_tokens': 'usage.prompt_tokens_details.cached_tokens', 'mtp_acceptance': 'speculative.accepted / speculative.drafted'}
        save(folder / 'engine-settings.json', settings)
        state('speed_running')
        with (PRIVATE / 'speed.log').open('w') as log:
            run([sys.executable, '-u', str(ROOT / 'scripts/tensorfold_speed.py'), '--root', str(ROOT),
                 '--base', 'http://<PRIVATE_IP>:18888'], stdout=log, stderr=subprocess.STDOUT, timeout=3600)
        state('dsh_ready')
        if not FINISHED.wait(timeout=12 * 3600):
            raise RuntimeError('DSH completion deadline expired')
        if not STATE.get('dsh_result', {}).get('success'):
            raise RuntimeError('DSH controller reported failure')
    except BaseException as error:
        failure = repr(error)
        state('benchmark_failed', error=failure)
    finally:
        if touched:
            state('restoring_deepseek')
            subprocess.run(['docker', 'logs', 'qwen38-single-benchmark-tf'], stdout=(ROOT / 'data/raw/speed/spark-single/engine.log').open('w'), stderr=subprocess.STDOUT)
            subprocess.run(['docker', 'stop', '-t', '30', 'qwen38-single-benchmark-tf'], timeout=90)
            try:
                gx('docker start dsv41-exl3-worker')
                run(['docker', 'start', 'dsv41-exl3-head'])
                models = health('http://127.0.0.1:8888/v1/models', 1800)
                save(PRIVATE / 'deepseek-restored.json', dict(models=models, verified=datetime.now(timezone.utc).isoformat()))
                state('failed_restored' if failure else 'complete_restored', error=failure)
            except BaseException as error:
                state('restoration_failed', error=repr(error), benchmark_error=failure)
        server.shutdown()
    if failure or STATE['phase'] == 'restoration_failed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
