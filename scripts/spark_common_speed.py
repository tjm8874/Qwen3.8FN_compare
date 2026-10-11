"""Serial native timing comparison; identical saved prompt bytes across engines."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import time
import urllib.request
from strata_speed import Telemetry, save
from tensorfold_speed import exact_prompt


def api(base, route, body=None):
    return urllib.request.urlopen(urllib.request.Request(base + route,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}), timeout=2400)


def metrics(base):
    raw = api(base, "/metrics").read().decode()
    result = {}
    for line in raw.splitlines():
        match = re.match(r'^(vllm:[a-zA-Z0-9_]+)(?:\{[^}]*\})? ([^ ]+)', line)
        if match:
            result[match[1]] = result.get(match[1], 0) + float(match[2])
    return raw, result


def verify_vllm_zero_reuse(usage, delta):
    # Missing usage fields are accepted only with explicit counters proving all
    # input KV was computed once and neither local nor external cache was used.
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens")
    if cached is not None:
        assert cached == 0, usage
    required = {
        "vllm:request_prefill_kv_computed_tokens_count": 1,
        "vllm:request_prefill_kv_computed_tokens_sum": 16384,
        "vllm:prompt_tokens_total": 16384,
        "vllm:prompt_tokens_cached_total": 0,
        "vllm:prefix_cache_hits_total": 0,
        "vllm:external_prefix_cache_hits_total": 0,
    }
    for key, expected in required.items():
        assert key in delta and delta[key] == expected, (key, delta.get(key), expected)
    return {"usage_cached_tokens": cached, "verified_reused_tokens": 0,
        "verification": "native computed-KV and explicit local/external cache counters",
        "counter_deltas": {key: delta[key] for key in required}}


def measure(base, folder, messages, engine):
    folder.mkdir(parents=True, exist_ok=False)
    calibration = json.load(api(base, "/tokenize", {"model": "Qwen3.8-Flash-Next",
        "messages": messages, "chat_template_kwargs": {"enable_thinking": False},
        "add_generation_prompt": True}))
    assert calibration["count"] == 16384, calibration
    save(folder / "calibration.json", calibration)
    body = {"model": "Qwen3.8-Flash-Next", "messages": messages, "max_tokens": 2048,
        "temperature": 0, "top_p": 1, "seed": 16, "stream": True,
        "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False}}
    if engine == "vllm":
        body.update(ignore_eos=True, min_tokens=2048)
    save(folder / "request.json", body)
    before = {}
    if engine == "vllm":
        raw, before = metrics(base)
        assert before.get("vllm:num_requests_running", 0) == before.get("vllm:num_requests_waiting", 0) == 0
        (folder / "metrics-before.prom").write_text(raw)
    started = time.perf_counter()
    first = None
    usage, native, speculative = {}, {}, {}
    chunks, reasoning = [], []
    finish, done = None, False
    with Telemetry(folder / "telemetry.jsonl"), api(base, "/v1/chat/completions", body) as response, (folder / "stream.sse").open("wb") as log:
        for line in response:
            log.write(line)
            log.flush()
            if not line.startswith(b"data:"):
                continue
            if line[5:].strip() == b"[DONE]":
                done = True
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
                        first = time.perf_counter() - started
                    chunks.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                finish = choice.get("finish_reason") or finish
    wall = time.perf_counter() - started
    text = "".join(chunks)
    (folder / "output.txt").write_text(text)
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", native.get("cached"))
    if engine == "vllm":
        for _ in range(30):
            raw, after = metrics(base)
            if after.get("vllm:request_prefill_time_seconds_count", 0) > before.get("vllm:request_prefill_time_seconds_count", 0):
                break
            time.sleep(1)
        (folder / "metrics-after.prom").write_text(raw)
        delta = {k: v - before.get(k, 0) for k, v in after.items()}
        for phase in ("prefill", "decode"):
            assert delta.get("vllm:request_" + phase + "_time_seconds_count") == 1, delta
        pre = delta["vllm:request_prefill_time_seconds_sum"]
        dec = delta["vllm:request_decode_time_seconds_sum"]
        for key in ("vllm:request_prefill_kv_computed_tokens_count", "vllm:request_prefill_kv_computed_tokens_sum",
            "vllm:prompt_tokens_total", "vllm:prompt_tokens_cached_total", "vllm:prefix_cache_hits_total",
            "vllm:external_prefix_cache_hits_total"):
            assert key in before and key in after, ("missing counter baseline", key)
        reuse_proof = verify_vllm_zero_reuse(usage, delta)
        save(folder / "cache-reuse-proof.json", reuse_proof)
        cached = reuse_proof["verified_reused_tokens"]
        rate = 2047 / dec
        native = {"prefill_seconds": pre, "decode_seconds": dec, "metrics_delta": delta}
    else:
        pre = native.get("prefill_seconds", native.get("prefill_s"))
        rate = native.get("tokens_per_second")
    valid = (done and usage.get("prompt_tokens") == 16384 and usage.get("completion_tokens") == 2048
        and cached == 0 and finish == "length" and not reasoning and pre and rate)
    summary = dict(usage=usage, native=native, speculative=speculative, reasoning_content="".join(reasoning),
        finish_reason=finish, done=done, output_sha256=hashlib.sha256(text.encode()).hexdigest())
    save(folder / "response-summary.json", summary)
    row = dict(valid=bool(valid), input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
        reused_tokens=cached, prefill_tps=16384 / pre if pre else None, decode_tps=rate,
        client_ttft_seconds=first, wall_seconds=wall, native_timings=native, finish_reason=finish,
        prompt_sha256=hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
    save(folder / "measurement.json", row)
    assert row["valid"], row
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--engine", choices=("tensorfold", "vllm"), required=True)
    p.add_argument("--case", required=True)
    a = p.parse_args()
    folder = a.root / "data/raw/speed" / a.case
    assert not (folder / "summary.json").exists(), "Preserve completed results"
    prompts = a.root / "benchmark-private/shared-prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(8):
        label = f"warmup-{i+1:02d}" if i < 3 else f"trial-{i-2:02d}"
        prompt_path = prompts / (label + ".json")
        if prompt_path.exists():
            messages = json.loads(prompt_path.read_text())
        else:
            assert a.engine == "tensorfold", "Calibrate and preserve common prompts on TensorFold first"
            messages, _ = exact_prompt(a.base)
            save(prompt_path, messages)
        print(label + " started", flush=True)
        row = measure(a.base, folder / label, messages, a.engine)
        print(json.dumps(row), flush=True)
        if i >= 3:
            rows.append(row)
    result = {field: dict(median=statistics.median([r[field] for r in rows]),
        min=min(r[field] for r in rows), max=max(r[field] for r in rows))
        for field in ("prefill_tps", "decode_tps", "client_ttft_seconds", "wall_seconds")}
    save(folder / "summary.json", dict(case=a.case, repetitions=5, metrics=result))


if __name__ == "__main__":
    main()
