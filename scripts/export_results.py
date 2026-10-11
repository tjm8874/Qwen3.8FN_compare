#!/usr/bin/env python3
"""Export measured results, a readable table, and sanitized raw-log archives."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile


LABELS = {
    "cuda-dual": ("PC : PRO 4500 + 5060 Ti", "Strata / IQ3_XXS", "DDR4 64GB / i5-14400 2枚挿し"),
    "cuda-pro4500": ("PC : PRO 4500 32GB", "Strata / IQ3_XXS", "DDR4 64GB / i5-14400"),
    "cuda-5060ti": ("PC : 5060 Ti 16GB", "Strata / IQ3_XXS", "DDR4 64GB / i7-13700"),
    "macbook-m5max": ("MacBook Pro M5 Max", "MLX-Serve / iQ-MLX 4.7bpw", "128GB / 2TB / ファン最大"),
    "spark-single": ("DGX Spark 1台", "vLLM 0.30.0 / INT4 top-5", "128GB / 4TB"),
    "ms-s1max": ("MS-S1 Max", "Halogen / W4B + overlay", "128GB / 2TB"),
    "macstudio-m5ultra": ("Mac Studio M5 Ultra", "MLX-Serve / iQ-MLX 4.7bpw", "256GB / 2TB / 自動ファン"),
    "gx10-spark": ("GX10 + DGX Spark", "vLLM 0.30.0 / INT4 top-5", "128GB × 2 / ケーブル込み"),
}


def sanitize(text):
    text = re.sub(r"(?<![\d.])(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])", "<PRIVATE_IP>", text)
    text = re.sub(r"/(?:Users|home)/[^/\s\"']+", "<HOME>", text)
    text = re.sub(r"0000:0000:0000:0000:0000:ffff:[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", "<PRIVATE_GID>", text)
    text = re.sub(r"[A-Za-z]:\\Users\\[^\\\s\"']+", "<WINDOWS_HOME>", text)
    text = re.sub(r"GPU-[0-9a-fA-F-]{32,}", "<GPU_UUID>", text)
    return text


def sanitize_json(value):
    if isinstance(value, dict):
        return {k: ("<REDACTED>" if re.search(r"api[_-]?key|authorization|password|secret|serial_number", k, re.I)
                    else sanitize_json(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_json(v) for v in value]
    if isinstance(value, str):
        return sanitize(value)
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def archive_logs(archive, clean, case, hashes, contents):
    """Keep an existing verified archive byte-for-byte when its files are unchanged."""
    manifest = archive.with_name("archive-manifest.json")
    if archive.is_file() and manifest.is_file():
        previous = json.loads(manifest.read_text())
        if (previous["files"] == hashes and
                previous["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()):
            return
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz", compresslevel=9) as tar:
        tar.add(clean, arcname=case)
    save(manifest, {"files": hashes,
         "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
         "redaction_rules": ["API/authorization/password fields", "private IPs", "personal home directory prefix", "GPU UUID"],
         "contents": contents})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    args = ap.parse_args()
    root = args.root.resolve()
    public = root / ".private/publication"
    public.mkdir(parents=True, exist_ok=True)
    targets = json.loads((root / "data/targets.json").read_text())
    exercises = (root / "data/exercises.txt").read_text().splitlines()
    rows = []
    for target in targets:
        case = target["id"]
        speed_case = target.get("speed_case", case)
        evaluation_case = target.get("evaluation_case", case)
        raw = root / "data/raw/speed" / speed_case
        summary = None
        reported = target.get("speed_evidence") == "owner_supplied_report"
        summary_file = raw / ("reported-summary.json" if reported else "summary.json")
        if summary_file.is_file():
            summary = json.loads(summary_file.read_text())
            assert summary["case"] == speed_case, "Selected recipe and measured case must match"
            assert summary["repetitions"] == 5
            if reported:
                assert summary["evidence_level"] == "owner_supplied_report" and summary["independently_validated_raw_logs"] is False
                assert summary["reported_protocol"] == {"warmups":3,"trials":5,"input_tokens":16384,"output_tokens":2048,"reused_tokens":0,"thinking":False,"temperature":0,"seed":16,"concurrency":1}
            else:
                for i in range(1, 6):
                    m = json.loads((raw / f"trial-{i:02d}/measurement.json").read_text())
                    assert m["valid"] and m["input_tokens"] == 16384 and m["output_tokens"] == 2048 and m["reused_tokens"] == 0
            clean = root / ".private/sanitized-speed" / speed_case
            clean.mkdir(parents=True, exist_ok=True)
            hashes = {}
            for source in sorted(raw.rglob("*")):
                if not source.is_file():
                    continue
                rel = source.relative_to(raw)
                destination = clean / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                original = source.read_bytes()
                if source.suffix == ".json":
                    body = json.dumps(sanitize_json(json.loads(original)), ensure_ascii=False, indent=2) + "\n"
                else:
                    body = sanitize(original.decode())
                exported = body.encode()
                destination.write_bytes(exported)
                hashes[str(rel)] = {"original_sha256": hashlib.sha256(original).hexdigest(),
                                    "public_sha256": hashlib.sha256(exported).hexdigest(),
                                    "redacted": original != exported}
                if source.name in ("summary.json", "reported-summary.json", "comparison-report.txt", "measurement.json", "engine-settings.json", "tokenizer-calibration.json", "provenance.json", "model-manifest.json"):
                    dest = public / "data/raw/speed" / speed_case / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(exported)
            archive = public / "data/raw/speed" / speed_case / "raw.tar.gz"
            archive_logs(archive, clean, speed_case, hashes, "owner supplied comparison report; raw trial logs pending" if reported else "all warmups, measured trials, native streams, request payloads, output and available telemetry")
        ev = root / "data/raw/evaluation" / evaluation_case / "summary.json"
        evaluation = json.loads(ev.read_text()) if ev.is_file() else None
        if evaluation:
            assert evaluation["state"] == "complete" and evaluation["total"] == 25
            assert [r["exercise"] for r in evaluation["rows"]] == exercises
            assert all(r["result_present"] and r["result_file_count"] == 1 for r in evaluation["rows"])
            assert evaluation["passed"] == sum(r["overallSuccess"] is True for r in evaluation["rows"])
            raw_eval = ev.parent
            clean_eval = root / ".private/sanitized-evaluation" / evaluation_case
            clean_eval.mkdir(parents=True, exist_ok=True)
            hashes_eval = {}
            for source in sorted(raw_eval.rglob("*")):
                if not source.is_file():
                    continue
                rel = source.relative_to(raw_eval)
                destination = clean_eval / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                original = source.read_bytes()
                if source.suffix == ".json":
                    body = json.dumps(sanitize_json(json.loads(original)), ensure_ascii=False, indent=2) + "\n"
                else:
                    body = sanitize(original.decode())
                exported = body.encode()
                destination.write_bytes(exported)
                hashes_eval[str(rel)] = {"original_sha256": hashlib.sha256(original).hexdigest(),
                                         "public_sha256": hashlib.sha256(exported).hexdigest(),
                                         "redacted": original != exported}
                if source.name in ("summary.json", "prepared.json", "receipt.json") or source.name.startswith("benchmark-"):
                    dest = public / "data/raw/evaluation" / evaluation_case / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(exported)
            archive_eval = public / "data/raw/evaluation" / evaluation_case / "raw.tar.gz"
            archive_logs(archive_eval, clean_eval, evaluation_case, hashes_eval, "DSH25 benchmark results, agent logs, actual upstream requests and responses")
        row = {"target": case, "hardware": target["hardware"], "engine": target["engine"], "model": target["model"],
               "speed_case": speed_case, "evaluation_case": evaluation_case,
               "price_usd": target.get("price_usd"), "price_basis": target.get("price_basis", "not yet verified"),
               "speed_state": ("reported" if reported else "complete") if summary else "pending",
               "speed_evidence": "owner_supplied_report" if reported else ("raw_logs_validated" if summary else "pending"), "speed_samples": 5 if summary else "",
               "input_tokens": 16384 if summary else "", "output_tokens": 2048 if summary else "",
               "dsh_state": "complete" if evaluation else "pending", "dsh_passed": evaluation["passed"] if evaluation else "",
               "dsh_total": evaluation["total"] if evaluation else ""}
        row["task_seconds_mean"] = ""
        row["task_duration_metric"] = ""
        if evaluation:
            durations = [r.get("benchmark_duration_ms") for r in evaluation["rows"]]
            assert len(durations) == 25 and all(isinstance(v, (int, float)) and v >= 0 for v in durations)
            assert all(not r.get("infrastructure_failure") for r in evaluation["rows"])
            row["task_seconds_mean"] = sum(durations) / 25000
            row["task_duration_metric"] = "ts-bench totalDuration: all25 tasks including model failures; lower is better"
        for metric in ["prefill_tps", "decode_tps", "client_ttft_seconds", "wall_seconds"]:
            for statistic in ["median", "min", "max"]:
                row[metric + "_" + statistic] = summary["metrics"][metric][statistic] if summary else ""
        row["scaling_reference_case"] = ""
        for metric in ["prefill_tps", "decode_tps", "wall_seconds"]:
            row[metric + "_ratio_vs_single"] = ""
        if case == "gx10-spark" and summary:
            reference_case = "spark-single-vllm-common-retry"
            reference = json.loads((root / "data/raw/speed" / reference_case / "summary.json").read_text())
            row["scaling_reference_case"] = reference_case
            for metric in ["prefill_tps", "decode_tps", "wall_seconds"]:
                row[metric + "_ratio_vs_single"] = summary["metrics"][metric]["median"] / reference["metrics"][metric]["median"]
        rows.append(row)
    data = root / "data"
    with (data / "summary.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    save(data / "summary.json", {"snapshot_utc": datetime.now(timezone.utc).isoformat(),
                                 "state": "partial" if any(r["speed_state"] not in ("complete", "reported") or r["dsh_state"] != "complete" for r in rows) else "complete",
                                 "rows": rows})
    table = ["| 構成 | Prefill tps | Decode tps | タスク秒数 ↓ | 成功数（補足） | 参考価格 USD |", "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        name = LABELS[row["target"]][0]
        speed = row["speed_state"] in ("complete", "reported")
        table.append(f"| {name} | {row['prefill_tps_median']:,.1f} | {row['decode_tps_median']:.1f} | " if speed else f"| {name} | 未測定 | 未測定 | ")
        table[-1] += (f"{row['task_seconds_mean']:.1f} | " if row["task_seconds_mean"] != "" else "未測定 | ")
        table[-1] += (f"{row['dsh_passed']}/{row['dsh_total']}" if row["dsh_state"] == "complete" else "未確定")
        table[-1] += f" | ${row['price_usd']:,.0f} |" if row["price_usd"] is not None else " | 確認中 |"
    (data / "results-table.md").write_text("\n".join(table) + "\n")
    render(root, rows)
    for source in [root / "README.md", root / ".gitignore", *data.glob("*"), *(root / "scripts").glob("*.py"), *(root / "images").glob("*.png")]:
        if not source.is_file():
            continue
        dest = public / source.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".py":
            dest.write_text(sanitize(source.read_text()))
        else:
            shutil.copyfile(source, dest)
    print(json.dumps({"speed_complete": sum(r["speed_state"] in ("complete", "reported") for r in rows),
                      "dsh_complete": sum(r["dsh_state"] == "complete" for r in rows),
                      "publication_files": sum(p.is_file() for p in public.rglob("*"))}))


def render(root, rows):
    from PIL import Image, ImageDraw, ImageFont
    fontpath = "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"
    def font(size):
        return ImageFont.truetype(fontpath, size)
    image = Image.new("RGB", (1800, 1856), "#f2f5f9")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1800, 220), fill="#112638")
    draw.text((65, 40), "Qwen3.8 Flash Next 実機比較", font=font(58), fill="white")
    draw.text((68, 128), "16,384入力 / 2,048出力 / thinking OFF / 同時1件", font=font(31), fill="#d3e7f4")
    draw.text((68, 175), "途中経過 · " + datetime.now(timezone.utc).strftime("%Y-%m-%d") + " · 速度は5回の中央値（tps）", font=font(26), fill="#90d7dc")
    columns = [65, 525, 1060, 1285, 1475, 1650]
    labels = ["実機・構成", "エンジン・モデル", "価格 USD", "Prefill", "Decode", "タスク秒数↓"]
    for x, label in zip(columns, labels):
        draw.text((x if label != "タスク秒数↓" else 1615, 255), label, font=font(28 if label != "タスク秒数↓" else 23), fill="#2f485f")
    for i, row in enumerate(rows):
        y = 312 + i * 142
        draw.rounded_rectangle((42, y, 1758, y + 130), radius=12, fill="white")
        name, recipe, detail = LABELS[row["target"]]
        draw.text((65, y + 24), name, font=font(30), fill="#122e42")
        draw.text((65, y + 79), detail, font=font(24), fill="#617687")
        draw.text((525, y + 35), recipe, font=font(26), fill="#264154")
        price = f"${row['price_usd']:,.0f}" if row["price_usd"] is not None else "確認中"
        draw.text((1060, y + 38), price, font=font(28), fill="#38546a")
        for x, key in [(1285, "prefill_tps_median"), (1475, "decode_tps_median")]:
            value = row[key]
            text = f"{value:,.0f}" if key.startswith("prefill") and value != "" else (f"{value:.1f}" if value != "" else "—")
            draw.text((x, y + 29), text, font=font(43), fill="#087e8b" if value != "" else "#9aabb9")
        task = f"{row['task_seconds_mean']:.1f}" if row["task_seconds_mean"] != "" else "—"
        draw.text((1640, y + 35), task, font=font(31), fill="#617687")
    short_labels = {"cuda-dual": "2GPU PC", "cuda-pro4500": "PRO 4500", "cuda-5060ti": "5060 Ti", "macbook-m5max": "MacBook", "spark-single": "Spark", "ms-s1max": "MS-S1", "macstudio-m5ultra": "M5 Ultra", "gx10-spark": "2台"}
    successes = "成功数：" + "、".join(f"{short_labels[row['target']]} {row['dsh_passed']}/{row['dsh_total']}" for row in rows if row['dsh_state'] == 'complete') + "。未完了は空欄。"
    for y, text in [(1472, "速度：同じ16K/2Kで3回ウォームアップ後、5回測定。入力再利用0を確認。"),
                    (1514, "タスク秒数：ts-bench 25問の平均（失敗を含む） / Lower is better / thinking ON。"),
                    (1556, successes),
                    (1598, "MacBook $6,699は申告実売。Mac Studio $9,999は参考価格。DGX $6,950は在庫なし。"),
                    (1640, "2台構成：GX10 $6,950 + DGX $6,950 + 接続ケーブル $200 = $14,100。"),
                    (1682, "条件・CSV・生ログ：github.com/tjm8874/Qwen3.8FN_compare")]:
        draw.text((65, y), text, font=font(23), fill="#40586b")
    pair = next((r for r in rows if r["target"] == "gx10-spark"), None)
    if pair and pair["scaling_reference_case"]:
        text = f"同一vLLM・モデルで1台→2台：Prefill {pair['prefill_tps_ratio_vs_single']:.3f}倍 / Decode {pair['decode_tps_ratio_vs_single']:.3f}倍。"
        draw.text((65, 1724), text, font=font(23), fill="#087e8b")
    draw.text((65, 1766), "5060 Ti：別Windows PCの提供集計、PCIe5.0 x8。原ログは添付後に照合。", font=font(23), fill="#40586b")
    draw.text((65, 1808), "モデル・量子化・エンジン差あり。PCは本体$1,000込みの参考予算。", font=font(23), fill="#40586b")
    output = root / "images/comparison-progress.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)


if __name__ == "__main__":
    main()
