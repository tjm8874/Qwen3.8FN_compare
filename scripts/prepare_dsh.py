#!/usr/bin/env python3
"""Create independent APFS clones of the already verified, pinned DSH/ts-bench fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--seed", type=Path, required=True)
    ap.add_argument("--tools", type=Path, required=True)
    ap.add_argument("--cases", nargs="+", required=True)
    args = ap.parse_args()
    assert len((args.root / "data/exercises.txt").read_text().splitlines()) == 25
    for case in args.cases:
        target = args.root / ".private/dsh" / case
        target.mkdir(parents=True, exist_ok=False)
        subprocess.run(["cp", "-cR", str(args.seed / "repo"), str(target / "repo")], check=True)
        exrepo = target / "repo/exercism-typescript"
        gitdir = Path(subprocess.check_output(["git", "-C", str(exrepo), "rev-parse", "--absolute-git-dir"], text=True).strip())
        assert gitdir.resolve().is_relative_to(target.resolve()), "Never reset a shared source checkout"
        subprocess.run(["git", "-C", str(exrepo), "reset", "--hard", "HEAD"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(exrepo), "clean", "-fd"], check=True, stdout=subprocess.DEVNULL)
        assert not subprocess.check_output(["git", "-C", str(exrepo), "status", "--porcelain"], text=True).strip()
        (target / "tools").mkdir()
        subprocess.run(["cp", "-cR", str(args.seed / "tools/yarn-global"), str(target / "tools/yarn-global")], check=True)
        (target / "dsh-home").mkdir()
        patch = """- id: agent-default-model
  config:
    provider: local-benchmark
    model: benchmark-model
- id: llm-pi-ai
  config:
    providers:
      local-benchmark:
        apiKeyEnv: DSH_LOCAL_API_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:19260/v1
        compat:
          supportsDeveloperRole: false
          maxTokensField: max_tokens
        models:
          - id: benchmark-model
            contextWindow: 32768
            maxTokens: 16384
- id: session-persistence-jsonl
  config:
    root: !!js dshHomePath('sessions')
    compression: none
"""
        (target / "dsh.patch.yml").write_text(patch)
        revisions = {label: subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
                     for label, path in [("ts_bench", target / "repo"), ("exercism", exrepo)]}
        assert revisions == {"ts_bench": "2b3bc944eb5728b5cb24d00e19371b595d528847",
                             "exercism": "ab2d84eda572061e10a8e1ac8c1a662fa859284e"}
        versions = {}
        env = dict(os.environ, PATH=str(args.tools / "bin") + ":" + os.environ["PATH"],
                   COREPACK_HOME=str(args.tools / "corepack-cache"), YARN_GLOBAL_FOLDER=str(target / "tools/yarn-global"))
        for tool in ("tsc", "jest", "tstyche"):
            r = subprocess.run([str(args.tools / "bin/corepack"), "yarn", "exec", tool, "--version"], cwd=exrepo,
                               env=env, capture_output=True, text=True)
            assert r.returncode == 0, (tool, r.stderr)
            versions[tool] = r.stdout.strip()
        installed = json.loads((args.tools / "dsh/node_modules/@deepseek-ai/dsh/package.json").read_text())
        assert installed["version"] == "0.1.5-rc.3"
        receipt = {"case": case, "revisions": revisions, "tool_versions": versions, "DSH_version": installed["version"],
                   "exercises_sha256": hashlib.sha256((args.root / "data/exercises.txt").read_bytes()).hexdigest(),
                   "dsh_patch": patch, "clean_exercises": True}
        dest = args.root / "data/raw/evaluation" / case
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "prepared.json").write_text(json.dumps(receipt, indent=2))
        print("Prepared " + case, flush=True)


if __name__ == "__main__":
    main()
