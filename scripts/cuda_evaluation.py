#!/usr/bin/env python3
"""Run on the harness Mac. Wait for speed tests, evaluate CUDA cases, restore Strata."""
import argparse
import json
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import urllib.request


def remote(command, check=True):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "cudaserver", command], check=check,
                          capture_output=True, text=True, timeout=180)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--tools", type=Path, required=True)
    args = ap.parse_args()
    root = args.root
    def terminate(_signum, _frame):
        raise SystemExit("Interrupted; restoring original Strata")
    signal.signal(signal.SIGTERM, terminate)
    while True:
        state = remote("systemctl --user show qwen38-strata-speed-main -p ActiveState -p Result").stdout
        if "ActiveState=active" not in state and "ActiveState=deactivating" not in state:
            if "Result=success" not in state:
                raise RuntimeError("Speed controller failed; inspect evidence before evaluation")
            break
        time.sleep(15)
    # Do not start evaluation unless all three complete speed receipts exist.
    remote("test -f /data/Qwen3.8FN_compare/data/raw/speed/cuda-dual/summary.json && "
           "test -f /data/Qwen3.8FN_compare/data/raw/speed/cuda-pro4500/summary.json && "
           "test -f /data/Qwen3.8FN_compare/data/raw/speed/cuda-5060ti/summary.json")
    originally_active = remote("systemctl --user is-active --quiet strata-iq3_xxs.service", check=False).returncode == 0
    private = root / ".private"
    keyfile = private / "strata-api-key"
    key = remote("cat /data/Strata/.api-key").stdout.strip()
    keyfile.write_text(key)
    keyfile.chmod(0o600)
    tunnel = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                               "-N", "-L", "127.0.0.1:19261:127.0.0.1:8190", "cudaserver"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    gateway = None
    unit = None
    try:
        remote("systemctl --user stop strata-iq3_xxs.service")
        for case, gpus in [("cuda-dual", [0, 1]), ("cuda-pro4500", [0]), ("cuda-5060ti", [1])]:
            unit = "qwen38-strata-dsh-" + case
            folder = root / "data/raw/evaluation" / case
            script = ("import json,pathlib; "
                      "p=pathlib.Path('/data/Qwen3.8FN_compare'); "
                      "c=json.load(open('/data/Strata/strata-iq3_xxs.json')); "
                      f"c.update(gpu={gpus!r},port=8190,open_browser=False,"
                      f"log=str(p/'data/raw/evaluation/{case}/engine.log')); "
                      "c.pop('layer_split',None); "
                      f"(p/'data/raw/evaluation/{case}').mkdir(parents=True,exist_ok=True); "
                      f"f=p/'.private/dsh-{case}.json'; f.write_text(json.dumps(c)); f.chmod(0o600)")
            remote("/data/Strata/.venv/bin/python -c " + shlex.quote(script))
            cfg = "/data/Qwen3.8FN_compare/.private/dsh-" + case + ".json"
            remote(f"systemd-run --user --unit={unit} --property=UMask=0077 --property=WorkingDirectory=/data/Strata "
                   f"/data/Strata/.venv/bin/python /data/Strata/serve/server.py --engine strata --config {cfg} --port 8190")
            started = time.monotonic()
            while True:
                if tunnel.poll() is not None:
                    raise RuntimeError("Evaluation tunnel exited")
                if time.monotonic() - started > 1500:
                    raise RuntimeError("Evaluation server did not become ready")
                try:
                    req = urllib.request.Request("http://127.0.0.1:19261/v1/models", headers={"Authorization": "Bearer " + key})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        models = json.load(r)
                    if any(m.get("id") == "qwen3.8-flash-next-iq3_xxs" for m in models.get("data", [])):
                        break
                except Exception:
                    pass
                time.sleep(3)
            gateway = subprocess.Popen([sys.executable, str(root / "scripts/dsh_gateway.py"), "--upstream",
                                        "http://127.0.0.1:19261", "--model", "qwen3.8-flash-next-iq3_xxs",
                                        "--api-key-file", str(keyfile), "--output", str(folder / "api")],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            time.sleep(1)
            if gateway.poll() is not None:
                raise RuntimeError("Evaluation gateway exited")
            print("Starting DSH25 " + case, flush=True)
            run = subprocess.run([sys.executable, "-u", str(root / "scripts/run_dsh.py"), "--root", str(root),
                                  "--tools", str(args.tools), "--case", case])
            gateway.terminate()
            gateway.wait(timeout=30)
            gateway = None
            remote("systemctl --user stop " + unit)
            unit = None
            if run.returncode:
                raise RuntimeError(f"DSH infrastructure failure in {case}; remaining cases not started")
    finally:
        if gateway and gateway.poll() is None:
            gateway.terminate()
            gateway.wait(timeout=30)
        tunnel.terminate()
        tunnel.wait(timeout=30)
        if unit:
            remote("systemctl --user stop " + unit, check=False)
        if originally_active:
            remote("systemctl --user start strata-iq3_xxs.service")


if __name__ == "__main__":
    main()
