# Premier League question trial

This is a real-data trial of JBrain's existing Rust service. The corpus is frozen before the user asks questions. No query rewriter or ingestion model has been added.

The subsequent [retrieval quality and latency benchmark](EPL_RETRIEVAL_RESULTS.md) tests 72 query variants and 1, 4 and 16 concurrent read clients, while preserving this corpus.

## Scope

The source is [Football-Data.co.uk's historical English results](https://www.football-data.co.uk/englandm.php), specifically the `E0.csv` files for seasons **1993/94 through 2024/25**. For example: [2024/25 source CSV](https://www.football-data.co.uk/mmz4281/2425/E0.csv). Raw files, download timestamps and SHA-256 hashes are preserved locally.

- 12,324 completed matches, 32 seasons and 51 clubs.
- 13,989 OKF documents: individual matches, club-season results, season tables, club histories, head-to-head summaries, dataset records and coverage notes.
- 111,384 distinct directed links within the generated corpus.
- Available match statistics such as half-time scores, shots and cards are included when present in the source. Availability differs by season.
- The inaugural 1992/93 season, 2025/26 onward, cups, player statistics, managers, transfers, bookmaker odds and live updates are outside this snapshot.

All statistical summaries are deterministic calculations from the CSV rows. Team names are normalized with a small explicit alias dictionary. Relationships follow structured team/season identifiers. This importer does not establish that JBrain can discover correct relationships in arbitrary prose.

**Points are earned points before deductions or administrative adjustments. Generated tables are not official final standings.** Longest streaks, head-to-head totals and records refer only to the covered matches. Summaries cite their underlying datasets; they are not independent corroboration.

## Local files and service

The dedicated service listens at `http://127.0.0.1:7331` (MCP: `/mcp`). It stores only this trial's knowledge. It was started with history off and remains running for questions.

```text
.jbrain/epl-trial/
  sources/              # The 32 source CSVs and download/hash manifest
  documents.jsonl       # Frozen generated Markdown and revision hashes
  corpus.json           # Coverage, counts and corpus/generator hashes
  brain/                # Canonical OKF files, derived index, durable journal
  session.json          # PID, service URL, startup and ingestion measurements
  receipts.jsonl        # Successful write receipts
  verification.json     # Full readback and sampled link-API checks
  questions.jsonl       # Created when the first user question is searched
  service.log
```

The entire `.jbrain/` directory is ignored by Git. Source data is not added to the repository. Stop the service before moving or backing up `brain/`. To stop this trial, send `SIGTERM` to the PID recorded in `session.json`, after checking that it is still the trial process. To restart after it stops:

```sh
python3 scripts/epl_trial.py start
```

The default CLI also connects to this service:

```sh
./target/release/jbrain health
./target/release/jbrain get epl/overview
```

## How questions will be tested

For each user question, run the automatic query path and inspect its interpreted plan, clause coverage, qualifications and revision-cited evidence. Answer from those returned sources. For unsupported interpretation, an agent can use an explicit plan or additional searches/reads; record these separately. Do not fill gaps with online or memorized football facts.

```sh
python3 scripts/epl_trial.py ask 'USER QUESTION'
```

The logger now defaults to `--mode query`, making one deterministic `POST /v1/query` request. Use `--mode raw` to reproduce the earlier literal-question workflow. Additional `--search`, `--read` and `--links` requests remain available and are logged as agent-assisted work. Direct reads must come from observed results, linked documents or IDs supplied by the user. No benchmark query is added to the real user-question ledger.

The raw JSON records question wording, keyword searches, result rankings, returned source content/revisions, link responses, per-request timings, total response bytes and service RSS/CPU samples. Fields for the final answer and quality judgments start as `null`; the tool does not invent a correctness score. Preserve retrieval observations when adding judgments or answers.

Once there are questions and judgments, report raw-query evidence retrieval separately from agent-assisted answer correctness. Include failures and unknowns. User questions form a useful exploratory trial; a small, adaptively selected set is not a general accuracy estimate.

## Initial run

On 2026-10-25, the existing Apple M2 release binary ingested all 13,989 concepts through four HTTP clients in **138.747 seconds**, with **32.72 service CPU seconds**. Service RSS rose from **8.922 MiB** to **13.203 MiB** after writes; the maximum sampled during ingestion was **13.328 MiB**. Service storage file lengths totaled **220,228,460 bytes** immediately after ingestion, including the durable operation journal and derived index. The source CSVs separately total 3,059,560 bytes, and canonical Markdown totals 23,068,204 bytes.

Full readback verified every document's Markdown/revision and all 111,384 extracted edges. Resolution and backlink API checks sampled 81 source documents (162 requests). Service RSS after full verification was **16.422 MiB**; the ingestion memory sample is not an all-workloads peak.

Two of three keyword smoke searches returned the particular expected summary in the first ten results. `Manchester City 2017 18` returned relevant individual matches but placed the expected club-season summary at rank **23**, outside the first ten. This is a known ranking miss, preserved in `ranking-diagnostic.json`; it was not tuned away before the question trial. Graph integrity and successful ingestion do not establish retrieval accuracy.

The question logger was exercised end to end against this service with a temporary ledger, checking original search, document read and links. The actual user-question ledger remains empty until the first question.

## Resource and cost interpretation

Ingestion uses four HTTP clients and the normal serialized writer, with full durability enabled. The service's RSS/CPU are sampled before and after ingestion and after batches of 100 writes. The maximum is a sampled high-water mark, not an operating-system peak measurement. Source validation reads every stored document, checks exact Markdown/revision/link targets, and samples the link API's resolution and backlinks.

Recorded wall time and storage are measurements of this machine and corpus. CPU excludes the importer, question client and conversational agent; process CPU sampling has 0.01-second reporting precision. Storage counts file lengths, not allocated filesystem blocks. Python conversion and verification happen outside the Rust service.

There are **zero separate model API calls from the importer, brain or question logger**. The conversational coding agent still uses a model to conduct this work, plan searches and answer; its actual token usage is not exposed by this harness. Token fields remain unknown, not zero. No measured dollar cost or GPT-4.1-nano accuracy should be inferred. Future API billing requires actual provider/model token usage and verified prices. A comparison with GBrain requires importing equivalent content and running the same questions and features there; this setup does not run GBrain.

## Rebuild or verify

The data snapshot is retained in `sources/`. To generate a fresh independent trial directory without changing the running corpus:

```sh
python3 tests/epl_trial_test.py
python3 scripts/epl_trial.py --root /tmp/epl-rebuild prepare \
  --source-dir .jbrain/epl-trial/sources
```

This validates source hashes, result/score consistency, dates and complete double-round-robin seasons (22 teams for 1993/94 and 1994/95, then 20). The date validator accounts for 2019/20's late finish. Duplicate IDs and missing generated link targets are rejected.

To check the running corpus again, outside timed question runs:

```sh
python3 scripts/epl_trial.py verify
```

The seven importer tests cover historical/modern season completeness, malformed rows, duplicate fixtures, the late 2019/20 dates, home/away statistics, cross-season unbeaten runs, deterministic revisions and graph closure. Those tests preserve the original importer. See [automatic retrieval](QUERY_RETRIEVAL.md) and [its separate evaluation](QUERY_RESULTS.md) for the new Rust query path.
