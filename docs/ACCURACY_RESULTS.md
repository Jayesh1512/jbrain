# Accuracy workflow implementation checks — 26 October 2026

The explicit evidence backend returned the correct requested values for **36/36 EPL and 12/12 planetary development cases when given oracle interpretations**. This establishes that the tools can retrieve and compute these answers. It does **not** establish natural-language interpretation quality, real-model final-answer accuracy, or a result above 97%.

No provider credential was configured, no real model was invoked, and no model winner or measured inference cost is reported. The independently authored holdout remains outstanding. See [the workflow, configuration and release gate](ACCURACY_WORKFLOW.md).

## Backend diagnostics

The questions are the existing, previously inspected `hard-retrieval-v1` fixture. The harness reads their intended typed scopes; expected source values stay in the client and are not passed to the brain. This is deliberately an upper control on backend capability, separate from the old automatic `/query` evaluation.

All EPL categories passed: four controls, six language cases, six compositions, four date comparisons, four implicit opponents, four venue subsets, four league comparisons and four negations. The harness used 110 HTTP evidence calls across 36 workflows. For implicit-opponent cases it reads the opponent from the match before looking up the next fact; it does not inject the expected opponent into the follow-up lookup.

Examples now available through exact tools are Bournemouth's **nine home wins** in 2016/17, Watford's **17 away goals** in 2017/18, Fulham's **11 home defeats** in 2013/14, and Southampton's **four away draws** in 2019/20. Complete league scopes also establish Norwich's five wins versus Watford's eight in 2019/20, and Manchester City's 102 goals versus Liverpool's 101 in 2013/14. These are Premier League results and derived statistics, not player or cup data.

The planetary diagnostic passed four controls, four comparisons, three language cases and one orbital-scope case, using 32 HTTP calls across 12 workflows. It checks both value and basis: Venus's signed sidereal rotation is **−5,832.5 hours**, while its solar day is **2,802 hours**; the Moon's orbital period is **27.3 days around Earth**. The NASA corpus is loaded into a temporary separate brain; it does not alter EPL documents. Source table snapshot: 18 March 2025.

These small deterministic development checks do not measure language understanding. Their perfect result is not a replacement for the prior observed automatic evidence result of 9/36 EPL and 5/12 planetary questions; the workflows differ. Unsupported questions were excluded from this backend-capability score, not counted as correct answers. They remain in the prepared development answer files and must be scored in the real model run.

Reproduce on the prepared local EPL service and existing source snapshots:

```sh
python3 scripts/benchmark_evidence.py --output /tmp/typed-epl.json
python3 scripts/benchmark_evidence.py --domain nasa --output /tmp/typed-nasa.json
```

## Workflow and concurrency checks

The final code passes **78 automated checks**: three Rust unit tests, 43 native integration tests and 32 evaluation/import tests. `cargo fmt --check`, release build and Clippy with warnings denied also pass.

The new provider tests use a real daemon and a **simulated loopback Responses provider**. They exercise reasoning-item replay, strict output requests, typed reads, actual usage reporting, separate verifier calls, fabricated/mismatched citation rejection, a verifier rejecting the home-only/total-points mismatch, bounded repair/tool loops, CLI/direct MCP, no-provider behavior and source revision changes. A delayed simulated model call allows a concurrent write to finish; the changed source causes an unaccepted retryable answer. Simulated behavior is not evidence of real-model semantic accuracy or injection resistance.

Existing tests still cover eight agent processes, reader/writer concurrency, CAS conflicts and merge retries, durable request deduplication, admission/backpressure and forced crash recovery at journal/file/Git/index boundaries. New arithmetic checks distinguish adjacent integers above 2^53, sum 0.1 + 0.2 to exactly 0.3, preserve signed values/units, reject absent or mixed-basis inputs, and distinguish a zero outcome over known fixtures from missing data. The evaluation runner is tested to send only questions, never source gold, and to record each HTTP failure as a miss-capable result. Independent reviews are tied to response hashes.

## Resource measurements

Same local Apple M2/macOS host as earlier reports, journal-only mode, release executable **6.909 MiB**. A 100-document run with four writer clients and 200 reads measured **8.92 MiB empty RSS**, **11.70 MiB after the workload**, **0.816 seconds for 100 writes**, **0.043 seconds for 200 reads**, and **4.22 MiB of stored file lengths**. Five idle seconds showed **0.00 seconds CPU change at `ps` reporting precision**. This is not a hard RAM cap or proof of zero CPU use. Repeated samples vary with allocation and OS paging.

These measurements exclude real hosted inference. The model-enabled path adds at most two active blocking workers, bounded context/evidence and network work, with no local model or embeddings. Its actual active RSS, end-to-end latency, token use and API cost still need live-provider measurement.

On the existing **13,989-document** EPL brain, schema-v5 migration took **24.356 seconds** and a subsequent unchanged restart **2.484 seconds**. The current derived index occupies **256.816 MiB** on disk, versus the prior report's 189.23 MiB. Scalar metadata and contextual passage indexes add storage; they are not loaded wholesale into RAM. The corpus and complete operation history also occupy storage separately.

Every canonical Markdown file was hashed before and after upgrade; all **13,989 files are unchanged**. The combined path/content digest remains `ebbac42a3b13fcd4198636606de6a9e33de056a8cf487a9f4e2823ac86a4c602`. Only derived data was rebuilt. The running final local trial service uses release SHA-256 `f7404c24899dac57934a9ae2f51ddd228cf0fe7a1b6c751d537d42868b34e758` at `http://127.0.0.1:7331`.

Detailed local artifacts are under `.jbrain/evidence-v2/`: `final-summary.json`, `final-typed-epl.json`, `final-typed-nasa.json`, `final-native-resources.json`, and upgrade/session snapshots. These measurements compare JBrain versions/workloads; they are not a GBrain or Slashdocs benchmark.

## Next measurable gate

The inspected development files are prepared at `.jbrain/evidence-v2/development-epl.jsonl` (48 cases: 36 answerable, 12 unsupported) and `development-nasa.jsonl` (16 cases: 12 answerable, four unsupported). They include source gold for independent review. The evaluator sends only `question`; it rejects relabeling cases marked `development_only` as a holdout. Use the matching brain for each domain; the live service contains EPL, while NASA remains separate.

After configuring a provider privately and restarting the daemon, start the real EPL development run:

```sh
python3 scripts/evaluate_ask.py run \
  --cases .jbrain/evidence-v2/development-epl.jsonl \
  --output /tmp/actual-model-development.jsonl
```

Source-review those final answers, compare model configurations on development, then freeze the system before obtaining the independent 1,500-case holdout. The scorer reports correctness, domain/category counts, abstention misses, citation support, actual token use, and a 1,000-questions/day cost projection when dated prices are supplied. **The 97% release gate has not passed.**
