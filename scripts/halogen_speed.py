#!/usr/bin/env python3
"""Exact 16K cold-prefill measurement with Halogen's native timing fields."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import threading
import time
import urllib.request
import uuid


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def api(base, route, body=None):
    request = urllib.request.Request(base.rstrip("/") + route,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(request, timeout=2400)


def exact_prompt(base, model):
    intro = f"Request {uuid.uuid4().hex}. Write a detailed engineering handbook.\n"
    notes = ("A reliable distributed service uses explicit state transitions, bounded retries, "
             "idempotent operations, durable receipts, timeouts and independent validation. "
             "Measure queue latency and service time separately. Backpressure prevents overload. "
             "Recovery requires a checkpoint and a reproducible event log.\n") * 1000
    ending = ("\nWrite at least 6000 words in English, with 60 numbered sections, about designing "
              "a distributed job scheduler. Cover failure recovery, transactions, leases, retries, "
              "testing, security and monitoring. For each section give a detailed explanation and "
              "an example. Start section 1 now and continue without an introductory summary. "
              "Do not stop before completing all 60 sections.")
    history = []

    def count(n):
        messages = [{"role": "user", "content": intro + notes[:n] + ending}]
        # Both endpoints render the same one-user chat with thinking disabled.
        # Count the running API's template, then require the completion usage
        # to confirm that the chat endpoint processed the identical length.
        with api(base, "/v1/messages/count_tokens", {
                "model": model, "messages": messages,
                "thinking": {"type": "disabled"}, "max_tokens": 2048}) as response:
            result = json.load(response)
        history.append({"note_characters": n, "input_tokens": result["input_tokens"]})
        return messages, result["input_tokens"]

    low, high, last = 0, len(notes), 0
    while low <= high:
        last = (low + high) // 2
        messages, size = count(last)
        if size == 16384:
            return messages, history
        if size < 16384:
            low = last + 1
        else:
            high = last - 1
    # Token merges can make the count locally nonmonotonic at a text cut.
    for n in range(max(0, last - 64), min(len(notes), last + 64) + 1):
        messages, size = count(n)
        if size == 16384:
            return messages, history
    raise RuntimeError("Cannot calibrate exactly16384 tokens; no measurement started")


class Telemetry:
    def __init__(self, path):
        self.path, self.stop = path, threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        with self.path.open("w") as out:
            while not self.stop.is_set():
                row = {"unix_seconds": time.time(), "memory": {}, "sensors": {}}
                try:
                    for line in Path("/proc/meminfo").read_text().splitlines():
                        name, value = line.split(":", 1)
                        if name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                            row["memory"][name] = value.strip()
                    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
                        name = (hwmon / "name").read_text().strip()
                        if name not in ("amdgpu", "k10temp"):
                            continue
                        values = {}
                        for pattern in ("temp*_input", "power*_average", "power*_input"):
                            for sensor in hwmon.glob(pattern):
                                try:
                                    values[sensor.name] = int(sensor.read_text().strip())
                                except (OSError, ValueError):
                                    pass
                        row["sensors"][name] = values
                except OSError as error:
                    row["telemetry_error"] = str(error)
                out.write(json.dumps(row) + "\n")
                out.flush()
                self.stop.wait(2)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=5)


def measurement_row(usage, native, finish, reasoning, first, wall):
    cached = native.get("cache_n")
    rate_fields = ("prompt_per_second", "predicted_per_second")
    positive = all(isinstance(native.get(k), (int, float)) and
                   math.isfinite(native[k]) and native[k] > 0 for k in rate_fields)
    valid = (usage.get("prompt_tokens") == 16384 and usage.get("completion_tokens") == 2048
             and native.get("prompt_n") == 16384 and native.get("predicted_n") == 2048
             and cached == 0 and finish == "length" and not reasoning
             and usage.get("completion_tokens_details", {}).get("reasoning_tokens") == 0
             and positive and native.get("prompt_ms", 0) > 0 and native.get("predicted_ms", 0) > 0)
    row = {"valid": bool(valid), "input_tokens": usage.get("prompt_tokens"),
           "output_tokens": usage.get("completion_tokens"), "reused_tokens": cached,
           "prefill_tps": native.get("prompt_per_second"),
           "decode_tps": native.get("predicted_per_second"), "native_timings": native,
           "client_ttft_seconds": first, "wall_seconds": wall,
           "finish_reason": finish, "mtp_acceptance": None}
    if native.get("draft_n"):
        row["mtp_acceptance"] = native["draft_n_accepted"] / native["draft_n"]
    return row


def measure(base, model, directory):
    messages, calibration = exact_prompt(base, model)
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / "calibration.json", calibration)
    payload = {"model": model, "messages": messages, "max_tokens": 2048,
               "temperature": 0, "top_p": 1, "seed": 16, "stream": True,
               "enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False},
               "stream_options": {"include_usage": True}}
    save(directory / "request.json", payload)
    started = time.perf_counter()
    first, usage, native, finish = None, {}, {}, None
    content, reasoning = [], []
    with Telemetry(directory / "telemetry.jsonl"), api(base, "/v1/chat/completions", payload) as response, (directory / "stream.sse").open("wb") as log:
        for line in response:
            log.write(line)
            log.flush()
            if not line.startswith(b"data:") or line.strip().endswith(b"[DONE]"):
                continue
            event = json.loads(line[5:])
            if event.get("error"):
                raise RuntimeError(event["error"])
            usage = event.get("usage") or usage
            native = event.get("timings") or native
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content"):
                    if first is None:
                        first = time.perf_counter() - started
                    content.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                finish = choice.get("finish_reason") or finish
    wall = time.perf_counter() - started
    text = "".join(content)
    (directory / "output.txt").write_text(text)
    save(directory / "response-summary.json", {"usage": usage, "timings": native,
         "finish_reason": finish, "reasoning_content": "".join(reasoning),
         "output_sha256": hashlib.sha256(text.encode()).hexdigest()})
    row = measurement_row(usage, native, finish, reasoning, first, wall)
    save(directory / "measurement.json", row)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8731")
    ap.add_argument("--root", type=Path, required=True)
    args = ap.parse_args()
    with api(args.base, "/health") as response:
        health = json.load(response)
    assert health["status"] == "ok" and health["slots"] == 1, health
    assert health["prompt_cache"]["enabled"] is False, "Launch with HALOGEN_PROMPT_CACHE=0"
    folder = args.root / "data/raw/speed/ms-s1max"
    assert not folder.exists(), "Preserve existing evidence; archive incomplete attempts explicitly"
    save(folder / "health-before.json", health)
    rows = []
    for i in range(8):
        label = f"warmup-{i+1:02d}" if i < 3 else f"trial-{i-2:02d}"
        print(f"{label} started", flush=True)
        row = measure(args.base, health["model"], folder / label)
        print(json.dumps(row), flush=True)
        if not row["valid"]:
            raise RuntimeError(f"Invalid fixed-length measurement{label}; evidence retained")
        if i >= 3:
            rows.append(row)
    metrics = {}
    for name in ("prefill_tps", "decode_tps", "client_ttft_seconds", "wall_seconds", "mtp_acceptance"):
        values = [r[name] for r in rows if r[name] is not None]
        if values:
            metrics[name] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
    save(folder / "summary.json", {"case": "ms-s1max", "repetitions": 5, "metrics": metrics})


if __name__ == "__main__":
    main()
