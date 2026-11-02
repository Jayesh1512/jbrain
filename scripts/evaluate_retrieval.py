#!/usr/bin/env python3
"""Offline retrieval evaluation against an isolated native JBrain service; stdlib only."""
import argparse
import collections
import concurrent.futures
import contextlib
import datetime
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import selectors
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CUTOFFS = (1, 5, 10)


def score(ranked, relevance, required):
    """Score unique document IDs. Unjudged documents have relevance zero."""
    if len(ranked) != len(set(ranked)):
        raise ValueError("Duplicate document IDs would inflate retrieval metrics")
    relevant = {key for key, grade in relevance.items() if grade > 0}
    if not relevant:
        return None  # Unknown-answer cases are reported separately.
    result = {}
    for k in CUTOFFS:
        top = ranked[:k]
        found = len(set(top) & relevant)
        dcg = sum((2 ** relevance.get(key, 0) - 1) / math.log2(rank + 2)
                  for rank, key in enumerate(top))
        ideal = sum((2 ** grade - 1) / math.log2(rank + 2)
                    for rank, grade in enumerate(sorted(relevance.values(), reverse=True)[:k]))
        result[f"hit@{k}"] = int(found > 0)
        result[f"recall@{k}"] = found / len(relevant)
        result[f"precision@{k}"] = found / k
        result[f"ndcg@{k}"] = dcg / ideal if ideal else 0.0
        result[f"evidence_complete@{k}"] = int(set(required).issubset(top))
    result["mrr@10"] = next((1 / rank for rank, key in enumerate(ranked[:10], 1) if key in relevant), 0.0)
    return result


def validate_fixture(fixture):
    if fixture.get("schemaVersion") != 1:
        raise ValueError("Expected fixture schemaVersion 1")
    documents = fixture["documents"]
    by_id = {doc["id"]: doc for doc in documents}
    if len(by_id) != len(documents) or not documents:
        raise ValueError("Document IDs must be unique; corpus must not be empty")
    seen = set()
    if not fixture["queries"]:
        raise ValueError("At least one query is required")
    for query in fixture["queries"]:
        if query["id"] in seen or not query["query"].strip():
            raise ValueError("Query IDs must be unique and query strings nonempty")
        seen.add(query["id"])
        relevant = query["relevance"]
        evidence = query["evidence"]
        if not set(relevant).issubset(by_id):
            raise ValueError(f"Unknown relevance document in {query['id']}")
        if any(type(grade) is not int or grade not in (1, 2) for grade in relevant.values()):
            raise ValueError("Relevance grades must be 1 (supporting) or 2 (direct evidence)")
        if not set(evidence).issubset(relevant) or bool(evidence) != bool(relevant):
            raise ValueError(f"Answerable queries need labeled required evidence: {query['id']}")
        for identity, quotes in evidence.items():
            if not quotes or any(not quote or quote not in by_id[identity]["body"] for quote in quotes):
                raise ValueError(f"Required quote absent from source body: {query['id']} / {identity}")
    return by_id


def markdown(doc):
    # JSON strings and arrays are valid YAML scalars/flow collections.
    header = {key: doc[key] for key in ("type", "title", "description", "tags", "aliases") if key in doc}
    return "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in header.items()) + "\n---\n" + doc["body"] + "\n"


def distractors(count):
    """Deterministic topic-overlapping negatives, with no evaluated factual answers."""
    names = ("Aster", "Birch", "Cobalt", "Dahlia", "Ember", "Flint", "Grove", "Helix", "Iris", "Juniper")
    topics = (
        ("database", "Database storage and connection pooling were discussed; no platform selection was recorded."),
        ("retention", "Log retention and metric deletion were discussed; no retention interval was approved."),
        ("security", "Credential rotation and remote access were reviewed; no policy change was approved."),
        ("budget", "Infrastructure spending and meal reimbursement were discussed; no amount was approved."),
        ("deployment", "Release rollback and snapshot recovery were reviewed; no operating procedure was approved."),
        ("camera", "Video streaming and battery charge cycles were reviewed; no device specification was approved."),
    )
    for index in range(count):
        name = names[index % len(names)]
        topic, text = topics[(index // len(names)) % len(topics)]
        yield {"id": f"noise/{name.lower()}-{index:04d}", "type": "Note",
               "title": f"{name} {topic} review {index}", "description": "Unapproved planning note for a different project.",
               "tags": [topic, "planning"], "aliases": [],
               "body": f"{name} review entry {index}. {text} This note is unrelated to the named production projects."}


class Api:
    def __init__(self, url):
        self.url = url

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)

    def search(self, query):
        return self.request("/v1/search?" + urllib.parse.urlencode({"q": query, "limit": 10}))["results"]

    def read(self, identity):
        return self.request("/v1/concept?" + urllib.parse.urlencode({"id": identity}))

    def write(self, doc, request_id, revision=None):
        return self.request("/v1/operations", {"requestId": request_id, "actor": "retrieval-evaluation/1",
                            "action": "put", "id": doc["id"], "expectedRevision": revision, "markdown": markdown(doc)})


@contextlib.contextmanager
def isolated_service(binary):
    with tempfile.TemporaryDirectory(prefix="jbrain-retrieval-") as directory:
        env = {**os.environ, "JBRAIN_DATA_DIR": directory, "JBRAIN_HOST": "127.0.0.1", "JBRAIN_PORT": "0",
               "JBRAIN_TOKEN": "", "JBRAIN_HISTORY": "off", "JBRAIN_MAX_PENDING": "64",
               "JBRAIN_MAX_PENDING_BYTES": "8388608", "JBRAIN_TEST_PAUSE_PHASE": ""}
        proc = subprocess.Popen([str(binary), "serve"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                if not selector.select(30):
                    raise RuntimeError("Service did not become ready in 30 seconds")
                line = proc.stdout.readline()
            if not line:
                raise RuntimeError(proc.stderr.read())
            ready = json.loads(line)
            if ready.get("event") != "ready":
                raise RuntimeError(f"Unexpected readiness event: {ready}")
            yield Api(ready["url"])
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


def freshness_checks(api):
    doc = {"id": "eval-probe/freshness", "title": "Freshness probe", "type": "Note", "body": "oldversioncanary"}
    first = api.write(doc, "eval-probe-create")
    doc["body"] = "newversioncanary"
    api.write(doc, "eval-probe-update", first["revision"])
    updated = api.read(doc["id"])
    checks = {"updated_content_visible": "newversioncanary" in updated["body"],
              "updated_term_searchable": any(item["id"] == doc["id"] for item in api.search("newversioncanary")),
              "old_term_removed": not api.search("oldversioncanary")}
    api.request("/v1/operations", {"requestId": "eval-probe-delete", "actor": "retrieval-evaluation/1", "action": "delete",
                                  "id": doc["id"], "expectedRevision": updated["revision"]})
    checks["deleted_term_removed"] = not api.search("newversioncanary")
    if not all(checks.values()):
        raise RuntimeError(f"Freshness checks failed: {checks}")
    return checks


def retrieve(api, query, mode):
    """Policies read query strings and search/link responses; never judgments or quotes."""
    direct = api.search(query)
    if mode == "search":
        return [item["id"] for item in direct], 1
    # Keep three initial hits, expand only their resolved links, then fill from search.
    seeds = direct[:3]
    ranked = [item["id"] for item in seeds]
    calls = 1
    for seed in seeds:
        if len(ranked) >= 10:
            break
        links = api.request("/v1/links?" + urllib.parse.urlencode({"id": seed["id"]}))
        calls += 1
        for item in links["outgoing"] + links["incoming"]:
            if item.get("resolved", True) and item["id"] not in ranked:
                ranked.append(item["id"])
                if len(ranked) >= 10:
                    break
    for item in direct:
        if len(ranked) >= 10:
            break
        if item["id"] not in ranked:
            ranked.append(item["id"])
    return ranked, calls


def summarize(rows):
    positive = [row for row in rows if row["metrics"] is not None]
    unknown = [row for row in rows if row["metrics"] is None]
    latency = sorted(row["latency_ms"] for row in rows)
    metrics = {key: sum(row["metrics"][key] for row in positive) / len(positive)
               for key in positive[0]["metrics"]} if positive else {}
    return {"queries": len(rows), "answerable_queries": len(positive), "unanswerable_queries": len(unknown),
            "metrics": metrics,
            "unknown_empty_rate": sum(not row["retrieved"] for row in unknown) / len(unknown) if unknown else None,
            "latency_ms": {"p50": latency[(len(latency) - 1) // 2], "p95": latency[max(0, math.ceil(len(latency) * .95) - 1)]},
            "mean_retrieval_http_calls": sum(row["retrieval_http_calls"] for row in rows) / len(rows)}


def evaluate(api, fixture, documents, mode, workers):
    @functools.lru_cache(maxsize=None)
    def checked_read(identity):
        actual = api.read(identity)
        if actual["markdown"] != markdown(documents[identity]):
            raise RuntimeError(f"Retrieved source does not match the admitted document: {identity}")
        return actual["body"]

    def evaluate_query(query):
        started = time.perf_counter()
        ranked, calls = retrieve(api, query["query"], mode)
        latency = (time.perf_counter() - started) * 1000
        # Read actual returned evidence; validation calls are outside retrieval latency.
        bodies = {identity: checked_read(identity) for identity in ranked}
        verified = {identity: all(quote in bodies[identity] for quote in quotes)
                    for identity, quotes in query["evidence"].items() if identity in bodies}
        if not all(verified.values()):
            raise RuntimeError(f"Required evidence missing from retrieved source: {query['id']}")
        return {"id": query["id"], "category": query["category"], "query": query["query"],
                "question": query.get("question", query["query"]), "relevance": query["relevance"],
                "required_evidence": query["evidence"], "retrieved": ranked, "verified_evidence": verified,
                "missing_at_5": sorted(set(query["evidence"]) - set(ranked[:5])),
                "metrics": score(ranked, query["relevance"], query["evidence"]),
                "latency_ms": round(latency, 3), "retrieval_http_calls": calls}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(evaluate_query, fixture["queries"]))
    categories = collections.defaultdict(list)
    for row in rows:
        categories[row["category"]].append(row)
    return {"summary": summarize(rows), "categories": {key: summarize(value) for key, value in categories.items()}, "queries": rows}


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/release/jbrain")
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/retrieval-v1.json")
    parser.add_argument("--distractors", type=int, default=200)
    parser.add_argument("--workers", type=positive_integer, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.distractors < 0:
        parser.error("--distractors must be nonnegative")
    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error("Build the native service first: cargo build --release --locked")
    raw = args.fixture.read_bytes()
    fixture = json.loads(raw)
    documents = validate_fixture(fixture)
    corpus = [*fixture["documents"], *distractors(args.distractors)]
    by_id = {doc["id"]: doc for doc in corpus}
    if len(by_id) != len(corpus):
        parser.error("Distractor IDs collide with fixture documents")
    with isolated_service(binary) as api:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda pair: api.write(pair[1], f"eval-seed-{pair[0]}"), enumerate(corpus)))
        freshness = freshness_checks(api)
        if api.request("/health")["concepts"] != len(corpus):
            raise RuntimeError("Corpus size does not match admitted concepts")
        modes = {mode: evaluate(api, fixture, by_id, mode, args.workers) for mode in ("search", "search_links")}
    report = {"schemaVersion": 1, "dataset": fixture["name"], "description": fixture["description"],
              "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "fixture_sha256": hashlib.sha256(raw).hexdigest(),
              "corpus_sha256": hashlib.sha256(json.dumps(corpus, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "platform": platform.platform(), "workers": args.workers, "document_count": len(corpus),
              "core_documents": len(documents), "distractors": args.distractors, "freshness": freshness,
              "policy": {"search": "Verbatim query, 10 direct search results.",
                         "search_links": "Verbatim query, retain top 3 hits, visit only their resolved outgoing/incoming links, then remaining direct hits; cap 10 documents and 4 retrieval HTTP calls. No model or query rewrite."},
              "modes": modes}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"Saved {args.output}")
    for mode, result in modes.items():
        summary = result["summary"]
        metrics = summary["metrics"]
        print(f"{mode}: {summary['answerable_queries']} answerable / {summary['unanswerable_queries']} unknown queries")
        if metrics:
            print(f"  Hit@1={metrics['hit@1']:.1%}, Recall@5={metrics['recall@5']:.1%}, "
                  f"MRR@10={metrics['mrr@10']:.3f}, complete evidence@5={metrics['evidence_complete@5']:.1%}")
        for category, group in result["categories"].items():
            if group["metrics"]:
                print(f"  {category}: Recall@5={group['metrics']['recall@5']:.1%} ({group['answerable_queries']} cases)")
    return report


if __name__ == "__main__":
    main()
