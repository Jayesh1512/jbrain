#!/usr/bin/env python3
"""Small reproducible benchmark. RSS is process resident memory, not a memory cap."""
import argparse
import concurrent.futures
import json
import os
import pathlib
import subprocess
import tempfile
import time
import urllib.request


def request(url, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def sample(pid):
    row = subprocess.check_output(["ps", "-o", "rss=,time=", "-p", str(pid)], text=True).split()
    clock = row[1].split(":")
    seconds = sum(float(part) * (60 ** index) for index, part in enumerate(reversed(clock)))
    return {"rssMiB": round(int(row[0]) / 1024, 2), "cpuSeconds": seconds}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="jbrain")
    parser.add_argument("--documents", type=int, default=100)
    parser.add_argument("--history", choices=["off", "git"], default="off")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide the service command after --")
    # Keep each benchmark run isolated from existing brain data.
    with tempfile.TemporaryDirectory(prefix="jbrain-bench-") as data_dir:
        env = {**os.environ, "JBRAIN_DATA_DIR": data_dir, "JBRAIN_PORT": "0", "JBRAIN_HOST": "127.0.0.1", "JBRAIN_TOKEN": "", "JBRAIN_HISTORY": args.history}
        started = time.perf_counter()
        proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready = json.loads(proc.stdout.readline())
            url = ready["url"]
            startup = time.perf_counter() - started
            time.sleep(1)
            empty = sample(proc.pid)
            body = "shared knowledge memory coordination " * 120

            def write(index):
                return request(url, "/v1/operations", {"requestId": f"bench-{index}", "actor": "benchmark/1", "action": "put", "id": f"bench/note-{index}", "expectedRevision": None, "markdown": f"---\ntype: Note\ntitle: Note {index}\ntags: [benchmark]\n---\n{body}"})

            begin = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(args.documents)))
            write_seconds = time.perf_counter() - begin
            begin = time.perf_counter()
            for index in range(200):
                request(url, f"/v1/concept?id=bench/note-{index % args.documents}")
            read_seconds = time.perf_counter() - begin
            loaded = sample(proc.pid)
            time.sleep(1)
            idle_start = sample(proc.pid)
            time.sleep(5)
            idle_end = sample(proc.pid)
            size = sum(path.stat().st_size for path in pathlib.Path(data_dir).rglob("*") if path.is_file())
            print(json.dumps({"label": args.label, "documents": args.documents, "history": args.history, "startupSeconds": round(startup, 3), "empty": empty, "loaded": loaded, "writeSeconds": round(write_seconds, 3), "read200Seconds": round(read_seconds, 3), "idleCpuSecondsOver5s": round(idle_end["cpuSeconds"] - idle_start["cpuSeconds"], 4), "storageMiB": round(size / 1024 ** 2, 2)}, indent=2))
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


if __name__ == "__main__":
    main()
