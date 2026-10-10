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
    "cuda-dual": ("PC : PRO 4500 + 5060 Ti", "Strata / IQ3_XXS", "DDR4 64GB / i5-14400"),
    "cuda-pro4500": ("PC : PRO 4500", "Strata / IQ3_XXS", "DDR4 64GB / i5-14400"),
    "cuda-5060ti": ("PC : 5060 Ti 16GB", "Strata / IQ3_XXS", "DDR4 64GB / i5-14400"),
    "macbook-m5max": ("MacBook Pro M5 Max", "MTPLX / Optimized Speed", "128GB / 再起動後に測定"),
    "spark-single": ("DGX Spark 1台", "TensorFold / INT4 top-5", "128GB / 4TB"),
    "ms-s1max": ("MS-S1 Max", "Halogen / 接続後に確認", "接続待ち"),
    "macstudio-m5ultra": ("Mac Studio M5 Ultra", "MLX-Serve / Uncensored 8bit", "256GB / 2TB"),
    "gx10-spark": ("GX10 + DGX Spark", "vLLM / hybrid 4/8bit", "128GB × 2"),
}


def sanitize(text):
    text = re.sub(r"(?<![\d.])(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])", "<PRIVATE_IP>", text)
    text = re.sub(r"/(?:Users|home)/[^/\s\"']+", "<HOME>", text)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    args = ap.parse_args()
    root = args.root.resolve()
    public = root / ".private/publication"
    public.mkdir(parents=True, exist_ok=True)
    targets = json.loads((root / "data/targets.json").read_text())
    rows = []
    for target in targets:
        case = target["id"]
        raw = root / "data/raw/speed" / case
        summary = None
        if (raw / "summary.json").is_file():
            summary = json.loads((raw / "summary.json").read_text())
            assert summary["repetitions"] == 5
            for i in range(1, 6):
                m = json.loads((raw / f"trial-{i:02d}/measurement.json").read_text())
                assert m["valid"] and m["input_tokens"] == 16384 and m["output_tokens"] == 2048 and m["reused_tokens"] == 0
            clean = root / ".private/sanitized-speed" / case
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
                if source.name in ("summary.json", "measurement.json", "engine-settings.json", "tokenizer-calibration.json"):
                    dest = public / "data/raw/speed" / case / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(exported)
            archive = public / "data/raw/speed" / case / "raw.tar.gz"
            archive.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive, "w:gz", compresslevel=9) as tar:
                tar.add(clean, arcname=case)
            save(archive.with_name("archive-manifest.json"), {"files": hashes,
                 "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                 "redaction_rules": ["API/authorization/password fields", "private IPs", "personal home directory prefix", "GPU UUID"],
                 "contents": "all warmups, measured trials, native streams, request payloads, output and available telemetry"})
        ev = root / "data/raw/evaluation" / case / "summary.json"
        evaluation = json.loads(ev.read_text()) if ev.is_file() else None
        row = {"target": case, "hardware": target["hardware"], "engine": target["engine"], "model": target["model"],
               "price_usd": target.get("price_usd"), "price_basis": target.get("price_basis", "not yet verified"),
               "speed_state": "complete" if summary else "pending", "speed_samples": 5 if summary else "",
               "input_tokens": 16384 if summary else "", "output_tokens": 2048 if summary else "",
               "dsh_state": "complete" if evaluation else "pending", "dsh_passed": evaluation["passed"] if evaluation else "",
               "dsh_total": evaluation["total"] if evaluation else ""}
        for metric in ["prefill_tps", "decode_tps", "client_ttft_seconds", "wall_seconds"]:
            for statistic in ["median", "min", "max"]:
                row[metric + "_" + statistic] = summary["metrics"][metric][statistic] if summary else ""
        rows.append(row)
    data = root / "data"
    with (data / "summary.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    save(data / "summary.json", {"snapshot_utc": datetime.now(timezone.utc).isoformat(),
                                 "state": "partial" if any(r["speed_state"] != "complete" or r["dsh_state"] != "complete" for r in rows) else "complete",
                                 "rows": rows})
    table = ["| 構成 | Prefill tps | Decode tps | DSH 成功数 | 参考価格 USD |", "|---|---:|---:|---:|---:|"]
    for row in rows:
        name = LABELS[row["target"]][0]
        speed = row["speed_state"] == "complete"
        table.append(f"| {name} | {row['prefill_tps_median']:,.1f} | {row['decode_tps_median']:.1f} | " if speed else f"| {name} | 未測定 | 未測定 | ")
        table[-1] += (f"{row['dsh_passed']}/{row['dsh_total']}" if row["dsh_state"] == "complete" else "未確定")
        table[-1] += f" | ${row['price_usd']:,.0f} |" if row["price_usd"] is not None else " | 確認中 |"
    (data / "results-table.md").write_text("\n".join(table) + "\n")
    render(root, rows)
    for source in [root / "README.md", root / ".gitignore", *data.glob("*"), *(root / "scripts").glob("*.py"), *(root / "images").glob("*.png")]:
        if not source.is_file():
            continue
        dest = public / source.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    print(json.dumps({"speed_complete": sum(r["speed_state"] == "complete" for r in rows),
                      "dsh_complete": sum(r["dsh_state"] == "complete" for r in rows),
                      "publication_files": sum(p.is_file() for p in public.rglob("*"))}))


def render(root, rows):
    from PIL import Image, ImageDraw, ImageFont
    fontpath = "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"
    def font(size):
        return ImageFont.truetype(fontpath, size)
    image = Image.new("RGB", (1800, 1690), "#f2f5f9")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1800, 220), fill="#112638")
    draw.text((65, 40), "Qwen3.8 Flash Next 実機比較", font=font(58), fill="white")
    draw.text((68, 128), "16,384入力 / 2,048出力 / thinking OFF / 同時1件", font=font(31), fill="#d3e7f4")
    draw.text((68, 175), "途中経過 · 2026-10-10 · 速度は5回の中央値（tps）", font=font(26), fill="#90d7dc")
    columns = [65, 525, 1060, 1285, 1475, 1650]
    labels = ["実機・構成", "エンジン・モデル", "価格 USD", "Prefill", "Decode", "DSH"]
    for x, label in zip(columns, labels):
        draw.text((x, 255), label, font=font(28), fill="#2f485f")
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
        score = f"{row['dsh_passed']}/{row['dsh_total']}" if row["dsh_state"] == "complete" else "未確定"
        draw.text((1650, y + 39), score, font=font(25), fill="#617687")
    for y, text in [(1472, "速度：同じ16K/2Kで3回ウォームアップ後、5回測定。入力再利用0を確認。"),
                    (1514, "DSH：thinking ON / ts-bench 25問。未確定の評価点は掲載しません。"),
                    (1556, "モデル差あり。DGX 1台は専門家削減版。PCは本体$1,000込みの所有者参考価格。"),
                    (1598, "Mac Studio $9,999は所有者参考価格。DGX $6,950は公式表示・在庫なし。"),
                    (1640, "条件・CSV・生ログ：github.com/tjm8874/Qwen3.8FN_compare")]:
        draw.text((65, y), text, font=font(23), fill="#40586b")
    output = root / "images/comparison-progress.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)


if __name__ == "__main__":
    main()
