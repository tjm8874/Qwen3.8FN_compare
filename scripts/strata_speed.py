#!/usr/bin/env python3
"""Measure Strata natively; run on the inference host, never publish its config/key."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import uuid


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def encoder(strata, config):
    sys.path[:0] = [str(strata), str(strata / "tools")]
    import strata_tokenizer as ST
    from serve.server import Service, MockEngine, ChatTemplate
    args = config["args"]
    pack = Path(args[args.index("--pack") + 1])
    tpath = Path(config.get("tokenizer", pack / "tokenizer"))
    vocab = json.loads((tpath / "vocab.json").read_text())
    tokens = [None] * len(vocab)
    for token, index in vocab.items():
        tokens[index] = token
    tok = ST.Tokenizer(tokens, (tpath / "merges.txt").read_text().split("\n"),
                       json.loads((tpath / "token_type.json").read_text()))
    service = Service(MockEngine(tok, "unused"), tok, ChatTemplate(tpath / "chat_template.jinja"))
    return tok, service


def prompt(tok, service, target, nonce):
    intro = (f"Request {nonce}. You are writing a detailed engineering handbook.\n"
             "The following reference notes are fictional. Use them as background.\n")
    notes = ("A reliable distributed service uses explicit state transitions, bounded retries, "
             "idempotent operations, durable receipts, timeouts and independent validation. "
             "Measure queue latency and service time separately. Backpressure prevents overload. "
             "Recovery requires a checkpoint and a reproducible event log.\n") * 1000
    ending = ("\nWrite at least 6000 words in English, with 60 numbered sections, about designing "
              "a distributed job scheduler. Cover failure recovery, transactions, leases, retries, "
              "testing, security and monitoring. For each section give a detailed explanation and "
              "an example. Start section 1 now and continue without an introductory summary. "
              "Do not stop before completing all 60 sections.")
    ids = tok.encode(notes)
    count = target - len(service.encode_prompt([{"role": "user", "content": intro + ending}], None,
                                               {"enable_thinking": False}))
    for _ in range(100):
        text = intro + tok.decode(ids[:count]) + ending
        messages = [{"role": "user", "content": text}]
        actual = len(service.encode_prompt(messages, None, {"enable_thinking": False}))
        if actual == target:
            return messages
        count += target - actual
    raise RuntimeError("Cannot construct exact input length")


class Telemetry:
    def __init__(self, path):
        self.path, self.end = path, threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        with self.path.open("w") as out:
            while not self.end.is_set():
                gpu = subprocess.run(["nvidia-smi", "--query-gpu=index,name,temperature.gpu,power.draw,memory.used,utilization.gpu",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15)
                memory = {line.split(":")[0]: line.split(":")[1].strip() for line in Path("/proc/meminfo").read_text().splitlines()
                          if line.startswith(("MemAvailable:", "MemTotal:", "SwapTotal:", "SwapFree:"))}
                out.write(json.dumps({"unix_seconds": time.time(), "gpu_csv": gpu.stdout.strip(),
                                      "gpu_returncode": gpu.returncode, "memory": memory}) + "\n")
                out.flush()
                self.end.wait(2)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.end.set()
        self.thread.join(timeout=20)


def request(url, key, body=None, timeout=2400):
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def measure(base, key, payload, directory):
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / "request.json", payload)
    start = time.perf_counter()
    first = None
    usage, timings, finish = None, None, None
    fragments, reasoning = [], []
    with Telemetry(directory / "telemetry.jsonl"), request(base + "/v1/chat/completions", key, payload) as response, (directory / "stream.sse").open("wb") as log:
        for line in response:
            log.write(line)
            log.flush()
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            event = json.loads(line[6:])
            if event.get("error"):
                raise RuntimeError(event["error"])
            usage = event.get("usage") or usage
            timings = event.get("timings") or timings
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content"):
                    if first is None:
                        first = time.perf_counter() - start
                    fragments.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                finish = choice.get("finish_reason") or finish
    wall = time.perf_counter() - start
    output = "".join(fragments)
    (directory / "output.txt").write_text(output)
    save(directory / "response-summary.json", {"usage": usage, "timings": timings, "finish_reason": finish,
                                               "client_ttft_seconds": first, "wall_seconds": wall,
                                               "reasoning_content": "".join(reasoning),
                                               "output_sha256": hashlib.sha256(output.encode()).hexdigest()})
    valid = bool(usage and timings and usage.get("prompt_tokens") == 16384
                 and usage.get("completion_tokens") == payload["max_tokens"]
                 and timings.get("cache_n") == 0 and timings.get("prompt_n") == 16384
                 and finish == "length" and not reasoning)
    row = {"valid": valid, "input_tokens": (usage or {}).get("prompt_tokens"),
           "output_tokens": (usage or {}).get("completion_tokens"), "reused_tokens": (timings or {}).get("cache_n"),
           "prefill_tps": (timings or {}).get("prompt_per_second"),
           "decode_tps": (timings or {}).get("predicted_per_second"),
           "native_timings": timings, "client_ttft_seconds": first, "wall_seconds": wall,
           "finish_reason": finish, "mtp_acceptance": None}
    if timings and timings.get("draft_n"):
        row["mtp_acceptance"] = timings["draft_n_accepted"] / timings["draft_n"]
    save(directory / "measurement.json", row)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strata", type=Path, default=Path("/data/Strata"))
    ap.add_argument("--config", type=Path, default=Path("/data/Strata/strata-iq3_xxs.json"))
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--cases", nargs="+", default=["cuda-dual", "cuda-pro4500", "cuda-5060ti"])
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--prepare-only", action="store_true")
    args = ap.parse_args()
    config = json.loads(args.config.read_text())
    key = config["api_key"]
    tok, service = encoder(args.strata, config)
    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    prepared = []
    for i in range(args.repeats + 1):
        prepared.append(prompt(tok, service, 16384, uuid.uuid4().hex))
    private = root / ".private"
    private.mkdir(mode=0o700, exist_ok=True)
    save(private / "prompts.json", prepared)
    print(f"Prepared {len(prepared)} prompts, each exactly 16384 input tokens including template", flush=True)
    if args.prepare_only:
        return
    gpu_map = {"cuda-dual": [0, 1], "cuda-pro4500": [0], "cuda-5060ti": [1]}
    originally_active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "strata-iq3_xxs.service"]).returncode == 0
    process = None
    try:
        subprocess.run(["systemctl", "--user", "stop", "strata-iq3_xxs.service"], check=True)
        for case in args.cases:
            case_root = root / "data/raw/speed" / case
            if (case_root / "summary.json").exists():
                raise RuntimeError(f"Preserve existing run: {case}")
            case_root.mkdir(parents=True, exist_ok=True)
            cfg = dict(config, gpu=gpu_map[case], port=8190, open_browser=False,
                       log=str(case_root / "engine.log"))
            if len(cfg["gpu"]) == 1:
                cfg.pop("layer_split", None)
            cfgpath = private / (case + ".json")
            save(cfgpath, cfg)
            cfgpath.chmod(0o600)
            safe = {k: v for k, v in cfg.items() if not any(x in k.lower() for x in ("key", "password", "token"))}
            safe.pop("log", None)
            safe.pop("cwd", None)
            safe.pop("exe", None)
            safe.pop("lib_dirs", None)
            save(case_root / "engine-settings.json", safe)
            start = time.monotonic()
            with (case_root / "server.log").open("w") as console:
                process = subprocess.Popen([sys.executable, str(args.strata / "serve/server.py"), "--engine", "strata",
                                            "--config", str(cfgpath), "--port", "8190"],
                                           cwd=args.strata, stdout=console, stderr=subprocess.STDOUT)
                base = "http://127.0.0.1:8190"
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"{case} server exited {process.returncode}; see server.log")
                    if time.monotonic() - start > 1500:
                        raise RuntimeError("Server load timeout")
                    try:
                        with request(base + "/health", key, timeout=3) as r:
                            health = json.load(r)
                        if health.get("loaded"):
                            break
                    except Exception:
                        pass
                    time.sleep(2)
                save(case_root / "health.json", health)
                print(f"{case} loaded in {time.monotonic() - start:.1f}s", flush=True)
                rows = []
                for i, messages in enumerate(prepared):
                    payload = {"model": cfg["model_name"], "messages": messages, "max_tokens": 128 if i == 0 else 2048,
                               "temperature": 0, "seed": 16, "stream": True, "stream_options": {"include_usage": True},
                               "chat_template_kwargs": {"enable_thinking": False}}
                    label = "warmup" if i == 0 else f"trial-{i:02d}"
                    print(f"{case} {label} started", flush=True)
                    row = measure(base, key, payload, case_root / label)
                    print(f"{case} {label}: {json.dumps(row)}", flush=True)
                    if not row["valid"]:
                        raise RuntimeError(f"Invalid fixed-length run: {case}/{label}; retain raw evidence")
                    if i:
                        rows.append(row)
                summary = {"case": case, "repetitions": len(rows), "metrics": {}}
                for field in ("prefill_tps", "decode_tps", "client_ttft_seconds", "wall_seconds", "mtp_acceptance"):
                    values = [row[field] for row in rows if row[field] is not None]
                    if values:
                        summary["metrics"][field] = dict(median=statistics.median(values), min=min(values), max=max(values))
                save(case_root / "summary.json", summary)
                process.terminate()
                process.wait(timeout=90)
                process = None
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if originally_active:
            subprocess.run(["systemctl", "--user", "start", "strata-iq3_xxs.service"], check=True)


if __name__ == "__main__":
    main()
