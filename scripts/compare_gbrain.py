#!/usr/bin/env python3
"""Compare page evidence on frozen EPL questions with an isolated GBrain."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request

from benchmark_epl_retrieval import build_gold, evidence_score, load_documents, read_sources

ROOT = Path(__file__).resolve().parents[1]
TRIAL = ROOT / ".jbrain/epl-trial"
DEFAULT_GBRAIN_HOME = ROOT / ".jbrain/gbrain-comparison/home-clean"
DEFAULT_OUTPUT = ROOT / ".jbrain/gbrain-comparison/evidence-results.json"


def gbrain_environment(home, openai_key_file=None):
    env = os.environ.copy()
    env["GBRAIN_HOME"] = str(home)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY",
                 "GOOGLE_API_KEY", "GBRAIN_DATABASE_URL"):
        env.pop(name, None)
    if openai_key_file:
        env["OPENAI_API_KEY"] = openai_key_file.read_text().strip()
    return env


def gbrain_call(home, mode, question, search_mode, openai_key_file=None, expand=False):
    argv = ["gbrain", mode, question, "--limit", "10", "--source-id", "epl",
            "--token-budget", "50000", "--snippet-chars", "300", "--json", "--timings",
            "--mode", search_mode]
    if mode == "query":
        argv.extend(("--autocut", "false"))
        if expand:
            argv.append("--expand")
    started = time.perf_counter()
    proc = subprocess.run(argv, cwd="/private/tmp", env=gbrain_environment(home, openai_key_file),
                          capture_output=True, text=True, timeout=90)
    elapsed = (time.perf_counter() - started) * 1000
    if proc.returncode:
        raise RuntimeError(f"GBrain {mode} failed with exit {proc.returncode}")
    response = json.loads(proc.stdout)
    if not openai_key_file and response.get("cost", {}).get("calls", 0) != 0:
        raise RuntimeError("GBrain made a provider call; aborting keyless comparison")
    ranked = list(dict.fromkeys(row["slug"] for row in response["results"]
                                if row.get("source_id") == "epl"))
    return ranked, {"cli_ms": elapsed, "timings_ms": response.get("timings_ms"),
                    "cost": response.get("cost"), "meta": response.get("meta")}


def jbrain_call(url, mode, question):
    started = time.perf_counter()
    if mode == "query":
        request = urllib.request.Request(url + "/v1/query",
            data=json.dumps({"question": question, "limit": 10}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    else:
        request = urllib.request.Request(url + "/v1/search?" +
            urllib.parse.urlencode({"q": question, "limit": 10}))
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    elapsed = (time.perf_counter() - started) * 1000
    ranked = list(dict.fromkeys(row["id"] for row in data["results"]))
    return ranked, {"http_ms": elapsed}


def run(args):
    docs = load_documents(args.trial)
    sources = read_sources(args.trial)
    easy_fixture = json.loads((ROOT / "tests/fixtures/epl-retrieval-v1.json").read_text())
    hard_fixture = json.loads((ROOT / "tests/fixtures/hard-retrieval-v1.json").read_text())
    easy_gold = build_gold(easy_fixture, sources, docs)
    cases = [("easy", case, easy_gold[case["id"]]) for case in easy_fixture["cases"]]
    hard_gold = json.loads(args.hard_gold.read_text())
    cases.extend(("hard", case, hard_gold[case["id"]]) for case in hard_fixture["epl"])
    if args.case_set:
        cases = [item for item in cases if item[0] == args.case_set]
    if args.answerable_only:
        cases = [item for item in cases if item[2]]
    if args.max_cases:
        cases = cases[:args.max_cases]
    prior = json.loads(args.reuse_jbrain.read_text()) if args.reuse_jbrain else None
    output = {"protocol": "Same EPL Markdown, original question only, top five distinct pages; GBrain provider calls enabled only when a key file is supplied",
              "jbrain_url": args.url, "gbrain_home": str(args.gbrain_home),
              "gbrain_version": subprocess.check_output(["gbrain", "--version"], text=True).strip(),
              "gbrain_search_mode": args.gbrain_mode,
              "gbrain_expand": args.gbrain_expand,
              "gbrain_query_only": args.gbrain_query_only,
              "case_set": args.case_set,
              "answerable_only": args.answerable_only,
              "openai_key_available": bool(args.openai_key_file),
              "reuse_jbrain": str(args.reuse_jbrain) if args.reuse_jbrain else None,
              "corpus_sha256": hashlib.sha256((args.trial / "documents.jsonl").read_bytes()).hexdigest(),
              "fixtures_sha256": {name: hashlib.sha256((ROOT / "tests/fixtures" / name).read_bytes()).hexdigest()
                                   for name in ("epl-retrieval-v1.json", "hard-retrieval-v1.json")},
              "rows": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if prior and (prior["corpus_sha256"] != output["corpus_sha256"] or
                  prior["fixtures_sha256"] != output["fixtures_sha256"]):
        raise ValueError("Prior JBrain result does not match corpus and fixtures")
    if args.resume and args.output.exists():
        saved = json.loads(args.output.read_text())
        for key in ("corpus_sha256", "fixtures_sha256", "gbrain_version",
                    "gbrain_search_mode", "openai_key_available"):
            if saved[key] != output[key]:
                raise ValueError(f"Cannot resume: {key} changed")
        for key in ("gbrain_expand", "gbrain_query_only", "answerable_only"):
            if saved.get(key, False) != output[key]:
                raise ValueError(f"Cannot resume: {key} changed")
        if saved.get("case_set") != output["case_set"]:
            raise ValueError("Cannot resume: case_set changed")
        output["rows"] = saved["rows"]
    for number, (set_name, case, groups) in enumerate(cases, 1):
        if number <= len(output["rows"]):
            saved = output["rows"][number - 1]
            if saved["id"] != case["id"] or saved["question"] != case["question"]:
                raise ValueError("Cannot resume: question order changed")
            continue
        row = {"set": set_name, "id": case["id"], "category": case["category"],
               "question": case["question"], "answerable": bool(groups), "systems": {}}
        for system in ("jbrain", "gbrain"):
            for mode in ("search", "query"):
                if args.gbrain_query_only and (system, mode) != ("gbrain", "query"):
                    continue
                if system == "jbrain":
                    if prior:
                        old = next((item for item in prior["rows"] if item["id"] == case["id"]), None)
                        if old is None or old["question"] != case["question"]:
                            raise ValueError("Prior JBrain row does not match question")
                        row["systems"][f"jbrain_{mode}"] = old["systems"][f"jbrain_{mode}"]
                        continue
                    ranked, timing = jbrain_call(args.url, mode, case["question"])
                else:
                    ranked, timing = gbrain_call(args.gbrain_home, mode, case["question"],
                                                 args.gbrain_mode, args.openai_key_file,
                                                 args.gbrain_expand and mode == "query")
                unknown = [identity for identity in ranked if identity not in docs]
                if unknown:
                    raise ValueError(f"{system} returned unknown source page: {unknown[0]}")
                row["systems"][f"{system}_{mode}"] = {"ranked": ranked[:10],
                    "metrics": evidence_score(ranked, groups), **timing}
        output["rows"].append(row)
        args.output.write_text(json.dumps(output, indent=2) + "\n")
        if number % 10 == 0 or number == len(cases):
            print(f"scored {number}/{len(cases)}", flush=True)
    for set_name in ("easy", "hard"):
        selected = [r for r in output["rows"] if r["set"] == set_name and r["answerable"]]
        keys = ("gbrain_query",) if args.gbrain_query_only else (
            "jbrain_search", "gbrain_search", "jbrain_query", "gbrain_query")
        print(set_name, {key: sum(r["systems"][key]["metrics"]["complete_evidence@5"] for r in selected)
                              for key in keys},
              "of", len(selected), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, default=TRIAL)
    parser.add_argument("--gbrain-home", type=Path, default=DEFAULT_GBRAIN_HOME)
    parser.add_argument("--gbrain-mode", choices=("conservative", "balanced", "tokenmax"), default="balanced")
    parser.add_argument("--hard-gold", type=Path, default=ROOT / ".jbrain/hard-eval-v1/gold.json")
    parser.add_argument("--url", default="http://127.0.0.1:7331")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--openai-key-file", type=Path, help="Private key file; its contents stay out of result artifacts")
    parser.add_argument("--reuse-jbrain", type=Path, help="Reuse matching JBrain rows from a prior result")
    parser.add_argument("--resume", action="store_true", help="Continue an interrupted matching output file")
    parser.add_argument("--gbrain-expand", action="store_true", help="Force GBrain query expansion")
    parser.add_argument("--gbrain-query-only", action="store_true", help="Score only GBrain query")
    parser.add_argument("--case-set", choices=("easy", "hard"), help="Restrict to one fixture set")
    parser.add_argument("--answerable-only", action="store_true", help="Skip unsupported-control cases")
    parser.add_argument("--max-cases", type=int, default=0, help="Run only the first N cases for a smoke test")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
