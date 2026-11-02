# EPL retrieval quality and latency

> **Update 2026-11-02 (search v2):** `search` now drops common question words and appends `OR` matches when fewer than three notes match every word. The figures below describe the earlier behavior. Current results: [synthetic fixture](evals/retrieval-v1-search-v2.json), [EPL summary](evals/epl-retrieval-search-v2-summary.json).

This report preserves the earlier retrieval baseline. The implemented automatic query path and its separate evaluation are documented in [QUERY_RETRIEVAL.md](QUERY_RETRIEVAL.md) and [QUERY_RESULTS.md](QUERY_RESULTS.md).

The current retriever is fast with compact keywords but poor at understanding questions. Raw questions retrieved complete evidence among the first five documents for **14/66 answerable variants (21.2%)**. Prewritten keyword plans reached **62/66 (93.9%)**; adding bounded link traversal reached **65/66 (98.5%)**. The prepared plans are an assisted control. JBrain does not currently produce them automatically.

The corpus and Rust search implementation were left unchanged. No embeddings, query-rewrite model or answer-generation model ran in the benchmark.

## Dataset and judgments

The frozen [Premier League corpus](EPL_TRIAL.md) contains 13,989 OKF documents, 12,324 matches and 111,384 directed links, covering 1993/94 through 2024/25. [The fixture](../tests/fixtures/epl-retrieval-v1.json) contains 72 query variants: 66 answerable and six outside the corpus scope. The answerable variants represent 60 distinct labeled needs sets; related wording variants are not independent observations.

Questions and plans were authored after ingestion and frozen before quality retrieval. This is a small, public diagnostic, not a held-out dataset or a production accuracy estimate. Keyword plans repair synonyms, aliases and typos manually; their quality is not a measured capability of GPT-4.1 nano or another deployed model. No plans were tuned after inspecting these results.

Expected statistics are independently tallied from preserved CSV rows, rather than copied from summaries. Club-name normalization is shared with the importer. Match scores and available referee/shot/card statistics come from specific CSV records. The evaluator checks the resulting expected evidence against frozen Markdown. Every quality result is read from the service and checked for exact content/revision; supporting quotations are verified in the actual response.

An **evidence group** represents one required fact. Equivalent sources, such as a club-season summary and the season table, are alternatives within that group. Comparisons require both fact groups. **Complete evidence@5** means every required group has a supporting document among the first five results. **Evidence coverage@5** averages the proportion of groups covered. **Hit@5** only requires one group, so it can overstate comparison success. Unknown-answer cases are excluded from these denominators. This is evidence retrieval quality, not generated-answer accuracy.

## Policies

1. **Raw:** search the question verbatim, requesting ten results.
2. **Prewritten keywords:** search the original question and prepared keyword queries, deduplicating identical queries. Fuse rankings with reciprocal rank fusion, `1 / (60 + rank)`, and retain ten results. Most cases add one keyword query; comparisons add two.
3. **Prewritten keywords + links:** retain the first three fused results, append their resolved outgoing links/backlinks once, then fill from remaining fused hits. Deduplicate and cap at ten documents; at most three link API calls.

Policies receive only question text, prepared search strings and API responses. They cannot access expected values, gold IDs, CSVs or the local corpus. Plans contain no target document paths. Graph traversal is a harness workflow using existing APIs, not automatic expansion in the search endpoint.

## Quality

| Policy | Complete evidence@5 | Evidence coverage@5 | Hit@5 | MRR@10 |
| --- | ---: | ---: | ---: | ---: |
| Raw question | 21.2% (14/66) | 21.2% | 21.2% | 0.212 |
| Prewritten keywords | 93.9% (62/66) | 94.7% | 95.5% | 0.909 |
| Prewritten keywords + links | 98.5% (65/66) | 99.2% | 100.0% | 0.919 |

| Category | Variants | Raw complete@5 | Keywords complete@5 | Keywords + links complete@5 |
| --- | ---: | ---: | ---: | ---: |
| Compact keyword controls | 12 | 100.0% | 100.0% | 100.0% |
| Natural season questions | 16 | 0.0% | 87.5% | 100.0% |
| Natural match questions | 12 | 0.0% | 100.0% | 100.0% |
| Aliases | 8 | 25.0% | 100.0% | 100.0% |
| Typos | 6 | 0.0% | 100.0% | 100.0% |
| Paraphrases | 6 | 0.0% | 83.3% | 100.0% |
| Comparisons requiring two facts | 6 | 0.0% | 83.3% | 83.3% |

Raw search requires every query token to match. Ordinary question words and unfamiliar wording cause empty results. All 28 natural season/match questions missed despite the facts being stored.

Prepared keywords missed complete evidence for Chelsea 2016/17, Fulham 2020/21 and Leicester 2015/16 win-total questions, returning individual matches where an aggregate was needed. Links recovered the aggregate documents for these three cases.

The remaining failure compares Liverpool's goals in **2018/19 and 2019/20**: **89 and 85**. Keyword fusion placed the second season summary at rank seven, outside the first five. The fixed graph policy then filled its ten candidates with first-season neighbors and displaced the second season entirely. Link expansion needs to preserve evidence for separate subqueries and budget neighbors carefully.

Raw search returned no results for the six out-of-scope questions. Prepared modes returned nonempty results for **4/6**, including unsupported seasons, a manager question and Everton's official post-deduction points. These related documents do not answer the questions. No answer-generation/abstention model was tested; result emptiness does not establish hallucination behavior.

## Warm HTTP latency

Measured on the same Apple M2/macOS arm64 machine, on loopback HTTP against the running service. Each policy ran 72 variants five times at 1, 4 and 16 concurrent clients: **3,240 measured workflows and 17,760 successful HTTP requests**, excluding warmups and quality checks. There were **zero observed errors**. These are readers of a fixed corpus; no simultaneous writes were included.

Full evidence time includes searches, links and fetching **up to five full documents**. Empty-result workflows fetch none. It excludes manual plan preparation, model calls and answer generation. Each block was warmed once before measurement. Percentiles use nearest rank. Runs are short and do not establish sustained capacity.

| Clients | Policy | Full evidence P50 | Full evidence P95 |
| ---: | --- | ---: | ---: |
| 1 | Raw | 1.78 ms | 11.05 ms |
| 1 | Prewritten keywords | 7.42 ms | 21.41 ms |
| 1 | Keywords + links | 8.53 ms | 23.30 ms |
| 4 | Raw | 6.80 ms | 33.84 ms |
| 4 | Prewritten keywords | 28.46 ms | 54.36 ms |
| 4 | Keywords + links | 33.78 ms | 80.30 ms |
| 16 | Raw | 27.60 ms | 147.28 ms |
| 16 | Prewritten keywords | 123.01 ms | 164.71 ms |
| 16 | Keywords + links | 126.23 ms | 168.68 ms |

Raw latency benefits from frequent empty results. Its nonempty workflows had P50 **5.06 ms** at one client and **104.55 ms** at 16 clients. A fast empty result is not successful retrieval.

At one client, candidate-selection P50/P95 before document fetches was **1.73/10.96 ms** for raw, **5.90/19.67 ms** for keywords and **6.97/21.49 ms** for keywords plus links. Full workflows averaged **2.06, 6.50 and 7.89 HTTP requests**, respectively. The graph mode returned approximately 96,183 response bytes per workflow on average.

At 16 clients, the graph mode completed approximately **126 workflows/second** in its short block. This includes client serialization and fresh HTTP connections. Peak sampled service RSS across measured blocks was **18.703 MiB**. Python benchmark memory, OS cache and the conversational agent are excluded. Sampling is not an OS-reported peak or a hard memory bound.

## Reproduce and inspect

With the EPL service running:

```sh
python3 tests/epl_retrieval_test.py
python3 scripts/benchmark_epl_retrieval.py
```

Defaults are five repetitions at 1, 4 and 16 clients. Override `--repeats`, `--clients`, `--root`, `--fixture` or `--output`. Writes are only report files; service access is read-only. The user-question ledger is untouched.

- [Portable summary, rankings and expected values](evals/epl-retrieval-v1-summary.json).
- [Interactive results](evals/epl-retrieval-v1-report.html).
- Full local trace: `.jbrain/epl-trial/retrieval-eval/results.json`.
- Local source judgments/quotations: `.jbrain/epl-trial/retrieval-eval/gold.json`.
- Local quality run before latency repetitions: `.jbrain/epl-trial/retrieval-eval/quality.json`.

Reports preserve fixture, corpus, evaluator and binary hashes. Seven evaluator tests cover equivalent evidence, incomplete comparisons, unknowns, duplicates, rank fusion, graph bounds, independent arithmetic and percentiles.

The next improvement to test is automatic query planning, intent-aware aggregate selection and balanced expansion across subqueries. These results do not establish the future planner's accuracy, latency or token cost, or performance against GBrain.
