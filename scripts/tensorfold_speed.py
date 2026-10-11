#!/usr/bin/env python3
"""Fixed-length cold-prefill speed measurement using TensorFold's native API."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time
import urllib.request
import uuid
from strata_speed import Telemetry, save


def api(base, route, body=None):
    req = urllib.request.Request(base + route, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=2400)


def exact_prompt(base):
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
    with api(base, "/tokenize", {"prompt": notes, "add_special_tokens": False}) as r:
        encoded = json.load(r)
    ids = encoded.get("tokens", encoded.get("token_ids"))
    assert isinstance(ids, list), encoded
    n = 16384 - 150
    for _ in range(40):
        with api(base, "/detokenize", {"tokens": ids[:n]}) as r:
            decoded = json.load(r)
        text = decoded.get("prompt", decoded.get("text"))
        assert isinstance(text, str), decoded
        messages = [{"role": "user", "content": intro + text + ending}]
        with api(base, "/tokenize", {"model": "Qwen3.8-Flash-Next", "messages": messages,
                 "chat_template_kwargs": {"enable_thinking": False}, "add_generation_prompt": True}) as r:
            actual = json.load(r)
        count = actual["count"]
        if count == 16384:
            return messages, actual
        n += 16384 - count
    raise RuntimeError("Could not calibrate exactly 16384 input tokens")


def measure(base, directory):
    messages, calibration = exact_prompt(base)
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / "calibration.json", calibration)
    payload = {"model": "Qwen3.8-Flash-Next", "messages": messages, "max_tokens": 2048,
               "temperature": 0, "top_p": 1, "seed": 16, "stream": True,
               "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False}}
    save(directory / "request.json", payload)
    start = time.perf_counter()
    first, usage, native, speculative, finish = None, {}, {}, {}, None
    chunks, reasoning = [], []
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
            native = event.get("tensorfold") or native
            speculative = event.get("speculative") or speculative
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content"):
                    if first is None:
                        first = time.perf_counter() - start
                    chunks.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                finish = choice.get("finish_reason") or finish
    wall = time.perf_counter() - start
    text = "".join(chunks)
    (directory / "output.txt").write_text(text)
    save(directory / "response-summary.json", dict(usage=usage, tensorfold=native, speculative=speculative,
         finish_reason=finish, client_ttft_seconds=first, wall_seconds=wall, reasoning_content="".join(reasoning),
         output_sha256=hashlib.sha256(text.encode()).hexdigest()))
    pre = native.get("prefill_s", native.get("prefill_seconds"))
    dec = native.get("decode_s", native.get("decode_seconds"))
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", native.get("cached"))
    output = usage.get("completion_tokens")
    rate = native.get("tokens_per_second") or native.get("decode_tps") or (output / dec if dec else None)
    valid = (usage.get("prompt_tokens") == 16384 and output == 2048 and cached == 0
             and finish == "length" and pre and rate and not reasoning)
    row = dict(valid=bool(valid), input_tokens=usage.get("prompt_tokens"), output_tokens=output,
               reused_tokens=cached, prefill_tps=16384 / pre if pre else None, decode_tps=rate,
               native_timings=native, client_ttft_seconds=first, wall_seconds=wall,
               finish_reason=finish, mtp_acceptance=None)
    if speculative.get("drafted"):
        row["mtp_acceptance"] = speculative["accepted"] / speculative["drafted"]
    elif speculative.get("drafted_tokens"):
        row["mtp_acceptance"] = speculative["accepted_tokens"] / speculative["drafted_tokens"]
    save(directory / "measurement.json", row)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18888")
    ap.add_argument("--root", type=Path, required=True)
    args = ap.parse_args()
    folder = args.root / "data/raw/speed/spark-single"
    assert not (folder / "summary.json").exists(), "Preserve completed run"
    rows = []
    for i in range(8):
        label = f"warmup-{i+1:02d}" if i < 3 else f"trial-{i-2:02d}"
        print(f"{label} started", flush=True)
        row = measure(args.base, folder / label)
        print(json.dumps(row), flush=True)
        if not row["valid"]:
            raise RuntimeError(f"Invalid fixed-length measurement {label}; raw evidence retained")
        if i >= 3:
            rows.append(row)
    metrics = {}
    for field in ("prefill_tps", "decode_tps", "client_ttft_seconds", "wall_seconds", "mtp_acceptance"):
        values = [r[field] for r in rows if r[field] is not None]
        if values:
            metrics[field] = dict(median=statistics.median(values), min=min(values), max=max(values))
    save(folder / "summary.json", dict(case="spark-single", repetitions=5, metrics=metrics))


if __name__ == "__main__":
    main()
