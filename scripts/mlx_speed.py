#!/usr/bin/env python3
"""Measure the already-running MLX-Serve without restarting or changing it."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
import urllib.request
import uuid


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--base", default="http://127.0.0.1:11234")
    args = ap.parse_args()
    out = args.root / "data/raw/speed/macstudio-m5ultra"
    out.mkdir(parents=True, exist_ok=False)

    def api(path, body=None):
        req = urllib.request.Request(args.base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=2400)

    def encode(text):
        return json.load(api("/tokenize", {"content": text, "add_special": False}))["tokens"]

    def decode(ids):
        return json.load(api("/detokenize", {"tokens": ids}))["content"]

    model = json.load(api("/v1/models"))["data"][0]["id"]
    props = json.load(api("/props"))
    save(out / "engine-settings.json", props)
    assert props["settings"]["version"] == "26.9.7-dev", "Running engine changed; inspect before measuring"
    assert props["model_info"]["quantization_bits"] == 8
    assert props["settings"]["max_concurrent"] == 1
    notes = ("A reliable distributed service uses explicit state transitions, bounded retries, "
             "idempotent operations, durable receipts, timeouts and independent validation. "
             "Measure queue latency and service time separately. Backpressure prevents overload. "
             "Recovery requires a checkpoint and a reproducible event log.\n") * 1000
    note_ids = encode(notes)
    ending = ("\nWrite at least 6000 words in English, with 60 numbered sections, about designing "
              "a distributed job scheduler. Cover failure recovery, transactions, leases, retries, "
              "testing, security and monitoring. For each section give a detailed explanation and "
              "an example. Start section 1 now and continue without an introductory summary. "
              "Do not stop before completing all 60 sections.")

    def content_for(body_tokens):
        intro = (f"Request {uuid.uuid4().hex}. You are writing a detailed engineering handbook.\n"
                 "The following reference notes are fictional. Use them as background.\n")
        count = body_tokens - len(encode(intro + ending))
        for _ in range(30):
            text = intro + decode(note_ids[:count]) + ending
            actual = len(encode(text))
            if actual == body_tokens:
                return text
            count += body_tokens - actual
        raise RuntimeError("Cannot construct exact body length")

    def measure(text, tokens, folder):
        folder.mkdir(exist_ok=False)
        payload = {"model": model, "messages": [{"role": "user", "content": text}],
                   "max_tokens": tokens, "temperature": 0, "top_p": 1, "seed": 16,
                   "enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False},
                   "cache_prompt": False, "stream": True, "stream_options": {"include_usage": True}}
        save(folder / "request.json", payload)
        readings = {}
        for name, command in [("swap", ["sysctl", "vm.swapusage"]),
                              ("thermal", ["pmset", "-g", "therm"]),
                              ("memory", ["memory_pressure", "-Q"])]:
            readings[name] = subprocess.run(command, capture_output=True, text=True, timeout=15).stdout
        save(folder / "telemetry-before.json", readings)
        start = time.perf_counter()
        first = None
        usage, timings, finish = {}, {}, None
        parts, reasoning = [], []
        with api("/v1/chat/completions", payload) as response, (folder / "stream.sse").open("wb") as log:
            for raw in response:
                log.write(raw)
                if not raw.startswith(b"data: ") or raw.strip() == b"data: [DONE]":
                    continue
                item = json.loads(raw[6:])
                if item.get("error"):
                    raise RuntimeError(item["error"])
                usage = item.get("usage") or usage
                timings = item.get("timings") or timings
                for choice in item.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        if first is None:
                            first = time.perf_counter() - start
                        parts.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    finish = choice.get("finish_reason") or finish
        text = "".join(parts)
        (folder / "output.txt").write_text(text)
        row = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
               "reused_tokens": timings.get("cached_n"), "prefill_tps": timings.get("prompt_per_second"),
               "decode_tps": timings.get("predicted_per_second"), "native_timings": timings,
               "client_ttft_seconds": first, "wall_seconds": time.perf_counter() - start,
               "finish_reason": finish, "mtp_acceptance": None,
               "output_sha256": hashlib.sha256(text.encode()).hexdigest(), "usage": usage,
               "reasoning_content": "".join(reasoning)}
        row["valid"] = (row["input_tokens"] == 16384 and row["output_tokens"] == tokens and
                        row["reused_tokens"] == 0 and timings.get("prompt_n") == 16384 and
                        finish == "length" and not reasoning)
        save(folder / "measurement.json", row)
        return row

    # Count the running engine's actual wrapper with an unscored calibration request.
    # Its implementation can differ from the checkpoint's Jinja template.
    body_tokens = 16372
    row = measure(content_for(body_tokens), 16, out / "calibration-01")
    assert row["reused_tokens"] == 0 and row["input_tokens"] is not None, row
    overhead = row["input_tokens"] - body_tokens
    save(out / "tokenizer-calibration.json", {"body_tokens": body_tokens,
          "actual_prompt_tokens": row["input_tokens"], "chat_wrapper_tokens": overhead,
          "included_in_speed_statistics": False})
    rows = []
    for index in range(8):
        name = f"warmup-{index + 1:02d}" if index < 3 else f"trial-{index - 2:02d}"
        print(name + " started", flush=True)
        row = measure(content_for(16384 - overhead), 2048, out / name)
        print(name + ": " + json.dumps({k: v for k, v in row.items() if k not in ("usage", "native_timings")}), flush=True)
        if not row["valid"]:
            raise RuntimeError("Invalid fixed-length/cache measurement; preserve and investigate")
        if index >= 3:
            rows.append(row)
    summary = {"case": "macstudio-m5ultra", "repetitions": len(rows), "metrics": {}}
    for key in ["prefill_tps", "decode_tps", "client_ttft_seconds", "wall_seconds"]:
        values = [r[key] for r in rows]
        summary["metrics"][key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
    save(out / "summary.json", summary)
    save(out / "engine-settings-after.json", json.load(api("/props")))


if __name__ == "__main__":
    main()
