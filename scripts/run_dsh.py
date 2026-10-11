#!/usr/bin/env python3
"""Run the fixed 25-task DSH suite once; keep errors and score separate."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def retained_rows(output, exercises):
    manifest = json.loads((output / "resume-state.json").read_text())
    count = manifest["validated_completed"]
    assert manifest["method"] == "interrupted_and_resumed" and 0 < count < len(exercises)
    assert manifest["first_segment"]["exercises"] == exercises[:count]
    assert not list((output / "api").glob("*/error.json")), "Retained segment contains gateway errors"
    rows = []
    for exercise in exercises[:count]:
        folder = output / "individual" / exercise
        receipt = folder / "receipt.json"
        assert hashlib.sha256(receipt.read_bytes()).hexdigest() == manifest["first_segment"]["receipts_sha256"][exercise]
        row = json.loads(receipt.read_text())
        files = list(folder.glob("benchmark-*.json"))
        assert row["exercise"] == exercise and row["result_present"] and row["result_file_count"] == len(files) == 1
        assert not row.get("infrastructure_failure") and row["overallSuccess"] in (True, False)
        result = json.loads(files[0].read_text())["results"][0]
        assert result["exercise"] == exercise and result["overallSuccess"] == row["overallSuccess"]
        rows.append(row)
    return rows, manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--tools", type=Path, required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--resume", action="store_true", help="Resume only a hash-verified retained segment")
    args = ap.parse_args()
    case = args.root / ".private/dsh" / args.case
    output = args.root / "data/raw/evaluation" / args.case
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PATH=str(args.tools / "bin") + ":" + str(Path.home() / ".local/bin") + ":" + os.environ["PATH"],
               COREPACK_HOME=str(args.tools / "corepack-cache"), YARN_GLOBAL_FOLDER=str(case / "tools/yarn-global"),
               BUN_INSTALL_CACHE_DIR=str(args.tools / "bun-cache"), DSH_HOME=str(case / "dsh-home"),
               DSH_BIN=str(args.tools / "bin/dsh"), DSH_PATCH_PATH=str(case / "dsh.patch.yml"),
               DSH_EXPECTED_MODEL="benchmark-model", DSH_LOCAL_API_KEY="local",
               DSH_PERMISSION_MODE="workspace-write", DSH_TELEMETRY_MODE="DISABLED")
    exercises = (args.root / "data/exercises.txt").read_text().splitlines()[:args.limit]
    all_rows, resume_manifest = retained_rows(output, exercises) if args.resume else ([], None)
    segment_started = datetime.now(timezone.utc).isoformat()
    initial_count = len(all_rows)
    for exercise in exercises[initial_count:]:
        if list((output / "api").glob("*/error.json")):
            raise RuntimeError("Gateway error evidence present; diagnose before scoring or continuing")
        folder = output / "individual" / exercise
        folder.mkdir(parents=True, exist_ok=True)
        existing = list(folder.glob("benchmark-*.json"))
        if existing:
            raise RuntimeError(f"Preserve existing result, do not silently mix runs: {exercise}")
        command = [str(args.tools / "bin/bun"), "src/index.ts", "--agent", "dsh", "--provider", "local",
                   "--model", "benchmark-model", "--version", "0.1.5-rc.3", "--exercise", exercise,
                   "--timeout", "1200", "--output-format", "json", "--output-dir", str(folder)]
        print(f"{datetime.now(timezone.utc).isoformat()} start {exercise}", flush=True)
        before = time.perf_counter()
        outer_timeout = False
        with (folder / "console.log").open("w") as log:
            process = subprocess.Popen(command, cwd=case / "repo", env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                process.wait(timeout=1350)
            except subprocess.TimeoutExpired:
                outer_timeout = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        files = list(folder.glob("benchmark-*.json"))
        valid = len(files) == 1
        row = json.loads(files[0].read_text())["results"][0] if valid else {}
        receipt = {"exercise": exercise, "overallSuccess": row.get("overallSuccess") if valid else None,
                   "process_returncode": process.returncode, "outer_timeout": outer_timeout,
                   "result_file_count": len(files), "runner_wall_seconds": time.perf_counter() - before,
                   "benchmark_duration_ms": row.get("totalDuration"), "result_present": valid}
        gateway_errors = list((output / "api").glob("*/error.json"))
        if gateway_errors:
            receipt["infrastructure_failure"] = True
            receipt["overallSuccess"] = None
            receipt["gateway_error_count"] = len(gateway_errors)
        (folder / "receipt.json").write_text(json.dumps(receipt, indent=2))
        all_rows.append(receipt)
        (output / "status.json").write_text(json.dumps({"state": "infrastructure_failure" if gateway_errors else "running", "completed": len(all_rows),
                                                       "passed": sum(r["overallSuccess"] is True for r in all_rows),
                                                       "latest": receipt}, indent=2))
        print(json.dumps(receipt), flush=True)
        if gateway_errors:
            raise RuntimeError("Upstream gateway failure; model score is not valid for this task")
        # A missing harness result is an infrastructure failure, not a scored model failure.
        if not valid:
            raise RuntimeError("No unique harness result; diagnose before continuing")
        time.sleep(20)
    summary = {"state": "complete", "total": len(all_rows),
               "passed": sum(r["overallSuccess"] is True for r in all_rows), "rows": all_rows}
    if resume_manifest:
        summary.update(measurement_method="interrupted_and_resumed", retained_segment=resume_manifest,
                       resumed_segment={"started": segment_started, "exercises": exercises[initial_count:]})
    (output / "summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
