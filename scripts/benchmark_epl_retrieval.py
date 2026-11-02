#!/usr/bin/env python3
"""Read-only quality and warm HTTP latency evaluation of the frozen EPL brain."""
import argparse
import collections
import concurrent.futures
import csv
import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import platform
import random
import re
import threading
import time
import urllib.parse
import urllib.request

from epl_trial import NAMES, sample

ROOT = Path(__file__).resolve().parents[1]
MODES = ("raw", "prewritten_keywords", "prewritten_keywords_links")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def read_sources(root):
    manifest = json.loads((root / "sources/manifest.json").read_text())
    result = []
    for item in manifest:
        data = (root / "sources" / item["file"]).read_bytes()
        if sha(data) != item["sha256"]:
            raise ValueError("Frozen CSV hash changed")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252")
        for row_number, row in enumerate(csv.DictReader(io.StringIO(text)), 2):
            if not row.get("HomeTeam"):
                continue
            for fmt in ("%d/%m/%Y", "%d/%m/%y"):
                try:
                    date = datetime.datetime.strptime(row["Date"], fmt).date().isoformat()
                    break
                except ValueError:
                    pass
            else:
                raise ValueError("Invalid source date")
            result.append({"year": item["start_year"], "date": date, "source": item["url"], "row": row_number, "raw": row})
    return result


def independently_tally(rows, club):
    counts = collections.Counter()
    for entry in rows:
        r = entry["raw"]
        if r["HomeTeam"] == club:
            gf, ga = int(r["FTHG"]), int(r["FTAG"])
        elif r["AwayTeam"] == club:
            gf, ga = int(r["FTAG"]), int(r["FTHG"])
        else:
            continue
        counts["played"] += 1
        counts["goals_for"] += gf
        counts["goals_against"] += ga
        counts["won" if gf > ga else "drawn" if gf == ga else "lost"] += 1
    result = {key: counts[key] for key in ("played", "won", "drawn", "lost", "goals_for", "goals_against")}
    result["points"] = result["won"] * 3 + result["drawn"]
    result["goal_difference"] = result["goals_for"] - result["goals_against"]
    return result


def load_documents(root):
    result = {}
    with (root / "documents.jsonl").open() as f:
        for line in f:
            doc = json.loads(line)
            doc["body"] = doc["markdown"].split("\n---\n", 1)[1]
            if sha(doc["markdown"].encode()) != doc["revision"]:
                raise ValueError("Frozen Markdown revision mismatch")
            result[doc["id"]] = doc
    return result


def build_group(need, sources, docs):
    candidates = {}

    def accept(identity, quote):
        if identity not in docs or quote not in docs[identity]["body"]:
            raise ValueError(f"CSV-derived expected evidence absent: {identity}: {quote}")
        candidates[identity] = [quote]

    if need["kind"] == "season_stat":
        year, club, raw_name, metric = need["year"], need["club"], need["source_club"], need["metric"]
        rows = [r for r in sources if r["year"] == year and raw_name in (r["raw"]["HomeTeam"], r["raw"]["AwayTeam"])]
        if len(rows) not in (38, 42):
            raise ValueError("Expected club-season fixtures absent")
        stats = independently_tally(rows, raw_name)
        season = f"{year}-{str(year + 1)[-2:]}"
        cs = f"epl/club-seasons/{season}/{slug(club)}"
        text = (f"{club} played {stats['played']} matches: won {stats['won']}, drew {stats['drawn']}, lost {stats['lost']}. "
                f"Goals scored: {stats['goals_for']}; goals conceded: {stats['goals_against']}; "
                f"goal difference: {stats['goal_difference']}. Points earned from match results: {stats['points']}.")
        accept(cs, text)
        table = "| " + f"[{club}](/{cs}.md) | " + " | ".join(str(stats[k]) for k in ("played", "won", "drawn", "lost", "goals_for", "goals_against", "goal_difference", "points")) + " |"
        accept("epl/seasons/" + season, table)
        # A records document can independently supply points/W/D/L for its listed seasons.
        if metric in ("points", "won", "drawn", "lost"):
            for line in docs["epl/records"]["body"].splitlines():
                if f"(/{cs}.md):" in line:
                    expected = f": {stats['points']} points, {stats['won']} wins, {stats['drawn']} draws, {stats['lost']} losses."
                    if not line.endswith(expected):
                        raise ValueError("CSV-derived record statistics disagree")
                    accept("epl/records", line)
        years = {r["year"] for r in sources if raw_name in (r["raw"]["HomeTeam"], r["raw"]["AwayTeam"])}
        if len(years) == 1:
            accept("epl/clubs/" + slug(club), text)
        expected = stats[metric]
    elif need["kind"] == "match":
        rows = [r for r in sources if r["date"] == need["date"] and r["raw"]["HomeTeam"] == need["source_home"] and r["raw"]["AwayTeam"] == need["source_away"]]
        if len(rows) != 1:
            raise ValueError("Expected match not uniquely present")
        entry = rows[0]
        r = entry["raw"]
        home, away = NAMES.get(r["HomeTeam"], r["HomeTeam"]), NAMES.get(r["AwayTeam"], r["AwayTeam"])
        season = f"{entry['year']}-{str(entry['year'] + 1)[-2:]}"
        identity = f"epl/matches/{season}/{entry['date']}-{slug(home)}-vs-{slug(away)}"
        metric = need["metric"]
        fields = {"score": ("FTHG", "FTAG"), "half_time": ("HTHG", "HTAG"), "corners": ("HC", "AC"),
                  "shots": ("HS", "AS"), "yellow_cards": ("HY", "AY")}
        if metric == "referee":
            expected = r["Referee"]
            quote = f"Referee: {expected}."
        else:
            a, b = fields[metric]
            expected = {"home": home, "away": away, "home_value": int(r[a]), "away_value": int(r[b])}
            if metric in ("score", "half_time"):
                quote = ("Full-time score: " if metric == "score" else "Half-time score: ") + f"{r[a]}-{r[b]}."
            else:
                label = {"corners": "Corners", "shots": "Shots", "yellow_cards": "Yellow cards"}[metric]
                quote = f"{label}: {home} {r[a]}, {away} {r[b]}."
        accept(identity, quote)
        if metric == "score":
            row = f"- [{entry['date']} {home} {r['FTHG']}-{r['FTAG']} {away}](/{identity}.md)"
            for c in (home, away):
                accept(f"epl/club-seasons/{season}/{slug(c)}", row)
            if row in docs["epl/records"]["body"]:
                accept("epl/records", row)
    else:
        raise ValueError("Unsupported expected-evidence kind")
    return {"need": need, "expected_value": expected, "candidates": candidates,
            "source_rows": [{"resource": r["source"], "row": r["row"]} for r in rows]}


def build_gold(fixture, sources, docs):
    seen = set()
    result = {}
    for case in fixture["cases"]:
        if case["id"] in seen or not case["question"].strip() or not case["keyword_queries"]:
            raise ValueError("Duplicate or invalid question")
        seen.add(case["id"])
        if any("/epl/" in q or "epl/" in q for q in case["keyword_queries"]):
            raise ValueError("Plans must not contain target IDs")
        result[case["id"]] = [build_group(n, sources, docs) for n in case["needs"]]
    return result


def evidence_score(ranked, groups):
    if len(ranked) != len(set(ranked)):
        raise ValueError("Duplicate ranked IDs")
    if not groups:
        return None
    relevant = {identity for g in groups for identity in g["candidates"]}
    result = {}
    for k in (1, 5, 10):
        top = set(ranked[:k])
        covered = sum(bool(top & set(g["candidates"])) for g in groups)
        result[f"hit@{k}"] = int(bool(top & relevant))
        result[f"evidence_coverage@{k}"] = covered / len(groups)
        result[f"complete_evidence@{k}"] = int(covered == len(groups))
    result["mrr@10"] = next((1 / rank for rank, identity in enumerate(ranked[:10], 1) if identity in relevant), 0.0)
    return result


class MeasuredApi:
    def __init__(self, url):
        self.url, self.calls = url, []

    def get(self, path, params):
        started = time.perf_counter()
        with urllib.request.urlopen(self.url + path + "?" + urllib.parse.urlencode(params), timeout=60) as response:
            raw = response.read()
        value = json.loads(raw)
        self.calls.append({"path": path, "params": params, "ms": (time.perf_counter() - started) * 1000, "bytes": len(raw)})
        return value


def fused_rank(lists):
    scores, first = collections.defaultdict(float), {}
    for values in lists:
        for rank, identity in enumerate(values, 1):
            first.setdefault(identity, len(first))
            scores[identity] += 1 / (60 + rank)
    return sorted(scores, key=lambda identity: (-scores[identity], first[identity]))[:10]


def retrieve(api, question, keyword_queries, mode):
    # This function has no source corpus, expected values, candidate IDs or judgments.
    queries = [question] if mode == "raw" else list(dict.fromkeys([question, *keyword_queries]))
    lists = [[r["id"] for r in api.get("/v1/search", {"q": q, "limit": 10})["results"]] for q in queries]
    direct = lists[0] if mode == "raw" else fused_rank(lists)
    if mode != "prewritten_keywords_links":
        return direct
    seeds = direct[:3]
    ranked = seeds[:]
    for identity in seeds:
        if len(ranked) >= 10:
            break
        links = api.get("/v1/links", {"id": identity})
        for target in links["outgoing"] + links["incoming"]:
            if target.get("resolved", True) and target["id"] not in ranked:
                ranked.append(target["id"])
                if len(ranked) == 10:
                    break
    for identity in direct:
        if len(ranked) == 10:
            break
        if identity not in ranked:
            ranked.append(identity)
    return ranked


def pipeline(url, case, mode, fetch=5):
    api = MeasuredApi(url)
    start = time.perf_counter()
    try:
        ranked = retrieve(api, case["question"], case["keyword_queries"], mode)
        selected = time.perf_counter()
        documents = {identity: api.get("/v1/concept", {"id": identity}) for identity in ranked[:fetch]}
        finish = time.perf_counter()
        return {"id": case["id"], "mode": mode, "ranked": ranked, "documents": documents, "calls": api.calls,
                "selection_ms": (selected - start) * 1000, "read_ms": (finish - selected) * 1000,
                "evidence_ms": (finish - start) * 1000, "error": None}
    except Exception as e:
        return {"id": case["id"], "mode": mode, "ranked": [], "documents": {}, "calls": api.calls,
                "selection_ms": None, "read_ms": None, "evidence_ms": (time.perf_counter() - start) * 1000,
                "error": f"{type(e).__name__}: {e}"}


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    def percentile(p):
        return values[max(0, math.ceil(len(values) * p) - 1)]
    return {"n": len(values), "mean": sum(values) / len(values), "p50": percentile(.5), "p95": percentile(.95), "p99": percentile(.99), "max": values[-1]}


def quality_summary(rows):
    positive = [r for r in rows if r["metrics"] is not None]
    unknown = [r for r in rows if r["metrics"] is None]
    keys = positive[0]["metrics"] if positive else []
    return {"queries": len(rows), "answerable": len(positive), "unsupported": len(unknown),
            "metrics": {k: sum(r["metrics"][k] for r in positive) / len(positive) for k in keys},
            "unknown_nonempty_rate": sum(bool(r["ranked"]) for r in unknown) / len(unknown) if unknown else None,
            "errors": sum(r["error"] is not None for r in rows)}


def quality(url, fixture, gold, docs):
    modes = {}
    for mode in MODES:
        rows = []
        for case in fixture["cases"]:
            result = pipeline(url, case, mode, fetch=10)
            for identity, actual in result["documents"].items():
                if actual["revision"] != docs[identity]["revision"] or actual["markdown"] != docs[identity]["markdown"]:
                    raise ValueError("Live source differs from frozen corpus: " + identity)
            groups = gold[case["id"]]
            verified = []
            for group in groups:
                found = {}
                for identity, quotes in group["candidates"].items():
                    if identity in result["documents"]:
                        if not all(q in result["documents"][identity]["body"] for q in quotes):
                            raise ValueError("Required evidence missing from actual returned document")
                        found[identity] = quotes
                verified.append(found)
            result.pop("documents")
            result.update({"question": case["question"], "category": case["category"], "keyword_queries": case["keyword_queries"],
                           "metrics": evidence_score(result["ranked"], groups), "verified_group_evidence": verified})
            rows.append(result)
        categories = collections.defaultdict(list)
        for row in rows:
            categories[row["category"]].append(row)
        modes[mode] = {"summary": quality_summary(rows), "categories": {k: quality_summary(v) for k, v in categories.items()}, "queries": rows}
        print(mode, json.dumps(modes[mode]["summary"]), flush=True)
    return modes


def timed_run(url, pid, cases, mode, workers, repeats):
    # Warm every case once per mode/concurrency block; warmups are outside measurements.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        warm = list(pool.map(lambda c: pipeline(url, c, mode), cases))
    if any(r["error"] for r in warm):
        raise RuntimeError("Latency warmup failed")
    jobs = cases * repeats
    random.Random(1701).shuffle(jobs)
    samples, stop = [sample(pid)], threading.Event()

    def monitor():
        while not stop.wait(.25):
            samples.append(sample(pid))

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    started = time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(lambda c: pipeline(url, c, mode), jobs))
        elapsed = time.perf_counter() - started
    finally:
        stop.set()
        watcher.join()
    samples.append(sample(pid))
    compact = []
    for row in rows:
        row.pop("documents")
        compact.append(row)
    return {"mode": mode, "concurrent_clients": workers, "repeats_per_query": repeats, "workflows": len(rows),
            "errors": sum(bool(r["error"]) for r in rows), "elapsed_seconds": elapsed,
            "workflows_per_second": len(rows) / elapsed,
            "selection_ms": distribution([r["selection_ms"] for r in rows]),
            "read_ms": distribution([r["read_ms"] for r in rows]),
            "evidence_ms": distribution([r["evidence_ms"] for r in rows]),
            "nonempty_evidence_ms": distribution([r["evidence_ms"] for r in rows if r["ranked"]]),
            "empty_evidence_ms": distribution([r["evidence_ms"] for r in rows if not r["ranked"]]),
            "mean_http_calls": sum(len(r["calls"]) for r in rows) / len(rows),
            "mean_response_bytes": sum(sum(c["bytes"] for c in r["calls"]) for r in rows) / len(rows),
            "service_before": samples[0], "service_after": samples[-1],
            "service_cpu_seconds": samples[-1]["cpu_seconds"] - samples[0]["cpu_seconds"],
            "max_sampled_rss_mib": max(s["rss_mib"] for s in samples), "rows": compact}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / ".jbrain/epl-trial")
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/epl-retrieval-v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".jbrain/epl-trial/retrieval-eval")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 4, 16])
    args = parser.parse_args()
    if args.repeats < 1 or any(n < 1 for n in args.clients):
        parser.error("repeats and clients must be positive")
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    corpus = json.loads((args.root / "corpus.json").read_text())
    config = json.loads((args.root / "session.json").read_text())
    if sha((args.root / "documents.jsonl").read_bytes()) != fixture["corpus_sha256"] or corpus["documents_sha256"] != fixture["corpus_sha256"]:
        raise ValueError("This fixture requires the frozen original EPL corpus")
    api = MeasuredApi(config["url"])
    if api.get("/health", {})["concepts"] != corpus["documents"]:
        raise ValueError("Live corpus count differs")
    docs = load_documents(args.root)
    sources = read_sources(args.root)
    gold = build_gold(fixture, sources, docs)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=2) + "\n")
    report = {"schemaVersion": 1, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "dataset": corpus, "fixture_sha256": sha(fixture_bytes), "evaluator_sha256": sha(Path(__file__).read_bytes()),
              "binary_sha256": config["binary_sha256"], "platform": platform.platform(),
              "policy": {"raw": "Verbatim question; direct top 10.",
                         "prewritten_keywords": "Original question plus prewritten keyword plans; RRF(k=60), top 10. Plans are authored assisted controls, not a running agent/model.",
                         "prewritten_keywords_links": "Same RRF seeds; retain top 3, append resolved outgoing links/backlinks of those seeds once, then remaining direct results; cap 10."},
              "timing_scope": "Warm loopback HTTP with new urllib requests; selection includes raw/keyword search and links; evidence time additionally reads up to five complete documents. No model planning or answer-generation time. Closed-loop Python clients; no writes during measurement.",
              "quality_scope": "Labeled evidence availability, not generated answer accuracy. Alternatives count as the same evidence group. Unknowns are reported as nonempty results, not model abstention. Public post-ingestion authored diagnostic, not held out; correlated variants.",
              "quality": quality(config["url"], fixture, gold, docs), "latency": []}
    (args.output / "quality.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for workers in args.clients:
        for mode in MODES:
            result = timed_run(config["url"], config["pid"], fixture["cases"], mode, workers, args.repeats)
            report["latency"].append(result)
            print(f"{mode}, clients={workers}: evidence p50={result['evidence_ms']['p50']:.3f}ms, p95={result['evidence_ms']['p95']:.3f}ms, errors={result['errors']}", flush=True)
            (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("Saved", args.output, flush=True)


if __name__ == "__main__":
    main()
