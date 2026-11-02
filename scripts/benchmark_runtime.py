#!/usr/bin/env python3
"""Measure warm JBrain HTTP and GBrain MCP retrieval on matched EPL questions."""
import argparse
import json
import os
from pathlib import Path
import random
import selectors
import statistics
import subprocess
import threading
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


def process_sample(pid):
    try:
        line = subprocess.check_output(
            ["ps", "-o", "rss=,%cpu=", "-p", str(pid)], text=True).strip()
        rss, cpu = line.split()
        return {"rss_kb": int(rss), "cpu_pct": float(cpu)}
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def process_cpu_seconds(pid):
    def seconds(value):
        total = 0.0
        for part in value.split(":"):
            total = total * 60 + float(part)
        return total

    try:
        user, system = subprocess.check_output(
            ["ps", "-o", "utime=,stime=", "-p", str(pid)], text=True).split()
        return seconds(user) + seconds(system)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


class McpClient:
    def __init__(self, home, key_file):
        env = os.environ.copy()
        for name in ("VOYAGE_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
                     "GBRAIN_DATABASE_URL", "OPENAI_API_KEY"):
            env.pop(name, None)
        env["GBRAIN_HOME"] = str(home)
        env["OPENAI_API_KEY"] = key_file.read_text().strip()
        self.proc = subprocess.Popen(
            ["gbrain", "serve", "--surface", "full", "--access", "read-only"],
            cwd="/private/tmp", env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.serial = 0
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "jbrain-runtime-benchmark", "version": "1"}})
        self.proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.proc.stdin.flush()

    def rpc(self, method, params):
        self.serial += 1
        identity = self.serial
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identity,
                                          "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if not self.selector.select(1):
                continue
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == identity:
                if message.get("error") or message.get("result", {}).get("isError"):
                    raise RuntimeError(f"GBrain MCP {method} returned an error")
                return message["result"]
        raise TimeoutError(f"GBrain MCP {method} timed out")

    def retrieve(self, mode, question):
        args = {"query": question, "source_id": "epl", "limit": 10,
                "snippet_chars": 300, "token_budget": 50000, "fields": "full"}
        if mode == "query":
            args["autocut"] = False
        result = self.rpc("tools/call", {"name": mode, "arguments": args})
        return sum(len(item.get("text", "").encode()) for item in result.get("content", []))

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def jbrain_retrieve(url, mode, question):
    if mode == "query":
        request = urllib.request.Request(
            url + "/v1/query",
            data=json.dumps({"question": question, "limit": 10}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    else:
        request = urllib.request.Request(
            url + "/v1/search?" + urllib.parse.urlencode({"q": question, "limit": 10}))
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return len(payload)


def questions(per_set):
    easy = json.loads((FIXTURES / "epl-retrieval-v1.json").read_text())["cases"]
    hard = json.loads((FIXTURES / "hard-retrieval-v1.json").read_text())["epl"]
    rng = random.Random(42)
    selected = []
    for name, cases in (("easy", easy), ("hard", hard)):
        pool = [case for case in cases if case["category"] not in ("unsupported", "out_of_scope")]
        selected.extend((name, item) for item in rng.sample(pool, min(per_set, len(pool))))
    rng.shuffle(selected)
    return selected


def run(args):
    client = McpClient(args.gbrain_home, args.openai_key_file)
    samples = {"jbrain": [], "gbrain": []}
    rows = []
    stop = threading.Event()

    def sample_processes():
        while not stop.wait(0.25):
            for name, pid in (("jbrain", args.jbrain_pid), ("gbrain", client.proc.pid)):
                sample = process_sample(pid)
                if sample:
                    samples[name].append(sample)

    try:
        selected = questions(args.per_set)
        for _, case in selected[:3]:
            for mode in ("search", "query"):
                jbrain_retrieve(args.url, mode, case["question"])
                client.retrieve(mode, case["question"])
        time.sleep(2)
        idle = {"jbrain": process_sample(args.jbrain_pid),
                "gbrain": process_sample(client.proc.pid)}
        cpu_before = {"jbrain": process_cpu_seconds(args.jbrain_pid),
                      "gbrain": process_cpu_seconds(client.proc.pid)}
        benchmark_began = time.perf_counter()
        monitor = threading.Thread(target=sample_processes, daemon=True)
        monitor.start()
        try:
            for set_name, case in selected:
                row = {"set": set_name, "id": case["id"], "modes": {}}
                for mode in ("search", "query"):
                    row["modes"][mode] = {}
                    for name, fn in (("jbrain", lambda: jbrain_retrieve(args.url, mode, case["question"])),
                                     ("gbrain", lambda: client.retrieve(mode, case["question"]))):
                        began = time.perf_counter()
                        payload_bytes = fn()
                        row["modes"][mode][name] = {
                            "ms": (time.perf_counter() - began) * 1000,
                            "payload_bytes": payload_bytes}
                rows.append(row)
                if len(rows) % 10 == 0:
                    print(f"measured {len(rows)}/{len(selected)}", flush=True)
        finally:
            stop.set()
            monitor.join(timeout=2)
        benchmark_seconds = time.perf_counter() - benchmark_began
        cpu_after = {"jbrain": process_cpu_seconds(args.jbrain_pid),
                     "gbrain": process_cpu_seconds(client.proc.pid)}
        summary = {}
        for name in ("jbrain", "gbrain"):
            summary[name] = {}
            for mode in ("search", "query"):
                values = sorted(row["modes"][mode][name]["ms"] for row in rows)
                summary[name][mode] = {"p50_ms": statistics.median(values),
                                       "p95_ms": values[max(0, (len(values) * 95 + 99) // 100 - 1)],
                                       "max_ms": values[-1]}
            summary[name]["rss_idle_kb"] = idle[name]["rss_kb"] if idle[name] else None
            summary[name]["cpu_idle_pct"] = idle[name]["cpu_pct"] if idle[name] else None
            summary[name]["rss_median_kb"] = statistics.median(
                s["rss_kb"] for s in samples[name]) if samples[name] else None
            summary[name]["rss_peak_kb"] = max(
                [idle[name]["rss_kb"]] + [s["rss_kb"] for s in samples[name]])
            summary[name]["cpu_median_pct"] = statistics.median(
                s["cpu_pct"] for s in samples[name]) if samples[name] else None
            summary[name]["cpu_mean_pct"] = statistics.mean(
                s["cpu_pct"] for s in samples[name]) if samples[name] else None
            summary[name]["cpu_peak_pct"] = max((s["cpu_pct"] for s in samples[name]), default=None)
            summary[name]["cpu_seconds_delta"] = (
                cpu_after[name] - cpu_before[name]
                if cpu_after[name] is not None and cpu_before[name] is not None else None)
            summary[name]["cpu_seconds_per_retrieval"] = (
                summary[name]["cpu_seconds_delta"] / (2 * len(rows))
                if summary[name]["cpu_seconds_delta"] is not None else None)
        result = {"protocol": "Resident processes, three-question warmup, 42-seed mixed EPL sample; "
                              "same questions, source and result cap; JBrain HTTP and GBrain MCP stdio",
                  "per_set": args.per_set, "benchmark_seconds": benchmark_seconds,
                  "rows": rows, "summary": summary}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openai-key-file", type=Path, required=True)
    parser.add_argument("--gbrain-home", type=Path,
                        default=ROOT / ".jbrain/gbrain-comparison/home-clean")
    parser.add_argument("--jbrain-pid", type=int, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:7331")
    parser.add_argument("--per-set", type=int, default=20)
    parser.add_argument("--output", type=Path,
                        default=ROOT / ".jbrain/gbrain-comparison/runtime-benchmark.json")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
