# Automatic query results — 2026-10-26

The [subsequent difficult evaluation](HARD_RETRIEVAL_EVAL.md) found that qualifiers and clause bindings can be lost while `complete` remains true. Treat completion as provisional and verify the original question scope.

The deterministic Rust query path was evaluated against the same frozen EPL corpus: **13,989 OKF documents, 12,324 matches, 111,384 links**, covering 1993/94–2024/25. No source documents were rewritten, and no model or embedding calls were made.

## Evidence quality

| Diagnostic | Complete document evidence in first 5 | Correct requested facts and quotes in actual query response | Unsupported questions kept below `complete` |
| --- | ---: | ---: | ---: |
| Original 72-question set | 66/66 answerable | 66/66 | 6/6 |
| New 17-question validation set | 12/12 answerable | 12/12 | 5/5 |

The original raw-search baseline retrieved complete document evidence for **14/66 (21.2%)**. Manually prepared keywords plus link traversal reached **65/66 (98.5%)**. The new endpoint receives only the original question and returns its own plan; it does not receive fixture keyword queries, expected answers, or target IDs. Liverpool's two-season comparison now reserves evidence for both seasons.

Expected statistics are independently tallied from the frozen source CSVs. The evaluator verifies the actual response's revision, requested metric value and supporting excerpt against source-derived gold, rather than counting a relevant document ID alone. The first implementation found all documents but misinterpreted one comparison's second metric; the stricter response check caught it. A regression test now covers inherited “goals” in “Chelsea concede ... City score ...”. Final measurements use the corrected binary.

The original diagnostic was public during implementation and informed vocabulary/grammar fixes. The new set was written before the first EPL query run and adds other seasons/entities, multiple measures, a shared-season comparison, an ISO date and unsupported cases. Its author knew the original questions and corpus; it is **not an independent blind benchmark**. Its wording and facts are also correlated with the domain adapter. These results establish behavior on these cases, not 100% general retrieval or answer accuracy. No final-answer generation is evaluated.

One unsupported query per set still returns related candidates. Both remain below `complete`; candidates alone must never be treated as proof. Generic prose, unfamiliar domains, indirect relations, intricate qualifiers and complex temporal questions still require agent verification or a caller-supplied plan.

## Warm latency and resources

Apple M2, release binary, local HTTP, fresh urllib requests, no model time, no writes. Each block uses all 72 original question variants five times: 360 measured requests per concurrency, 1080 total. Warmups are excluded. The endpoint includes interpretation, selection, fact/provenance assembly and excerpts in one HTTP response; there are no subsequent document reads in the timed workflow.

| Concurrent clients | P50 | P95 | Throughput | Sampled service RSS maximum |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 7.28 ms | 17.12 ms | 116 queries/s | 13.41 MiB |
| 4 | 19.54 ms | 34.09 ms | 188 queries/s | 13.47 MiB |
| 16 | 75.00 ms | 105.29 ms | 203 queries/s | 13.59 MiB |

All 1080 measured requests succeeded. Service CPU time across the three blocks was **5.82 seconds**; average response size was approximately **1.88 kB**. These are short warm samples, not sustained capacity limits. RSS excludes agents, benchmark clients and OS filesystem cache; it is a sampled process value, not a hard memory ceiling. The subsequent full-document verification used **15.625 MiB RSS**. It confirmed every canonical Markdown/revision and all 111,384 extracted edges. Other workloads can use more memory.

The previous manually prepared keyword-plus-links workflow had P50/P95 **8.53/23.30 ms** at one client and **126.23/168.68 ms** at 16 clients. It made multiple HTTP calls and read full documents, so this compares end-to-end workflows with different work and payload sizes, not isolated search algorithms. Raw search was faster when returning empty results; that is not equivalent successful retrieval. None of these measurements benchmarks GBrain or Slashdocs.

There are **zero separate model API calls** and no model billing from this endpoint. At 1,000 questions/day, this measured mix implies roughly 5.4 service CPU seconds/day for retrieval alone; ingestion, writes, agents, hosting and final-answer generation are separate. The conversational coding agent's own usage is unmeasured.

The release executable is about **6.6 MiB**. The current derived index occupies **189.23 MiB** on disk, in addition to canonical files and the operation journal. Metadata/fact indexes use extra disk storage; SQLite page-cache targets remain 256 KiB per connection. The EPL schema upgrade rebuilt only derived data in **16.819 seconds** on this machine. Subsequent restarts reuse the index. Old ingestion measurements in the trial session describe the earlier binary and are not a new-ingestion benchmark of this version.

## Correctness checks

`cargo test --locked`, `cargo clippy --all-targets -- -D warnings`, 30 native integration tests, and 21 importer/evaluation tests pass. Integration coverage includes simultaneous reads/writes, exact fact-to-revision consistency, CAS conflicts, aliases removed on update/delete, multiple comparison metrics, fact bases, ambiguous names, stale/conflicting evidence, bounded graph filters, direct MCP/CLI, schema migration and forced crash recovery at each write boundary. Separate read calls may observe later revisions; one query assembles evidence under the published-index lock.

## Reproduce

```sh
cargo build --release --locked
python3 -m unittest discover -s tests -p '*suite.py' -v
python3 -m unittest discover -s tests -p '*_test.py' -v
# Reuse the running upgraded EPL service recorded in session.json:
python3 scripts/benchmark_query.py --repeats 5
python3 scripts/epl_trial.py verify
```

Full traces: `.jbrain/epl-trial/query-eval/results.json`. Portable summary: [epl-query-v2-summary.json](evals/epl-query-v2-summary.json). Fixture hashes, binary hash and unchanged corpus SHA-256 are recorded. Original baseline artifacts are retained separately under `retrieval-eval/`. The actual user-question ledger remains reserved for user questions.

See [the query contract and limits](QUERY_RETRIEVAL.md) for producer metadata, evidence statuses, optional plans and interface details.
