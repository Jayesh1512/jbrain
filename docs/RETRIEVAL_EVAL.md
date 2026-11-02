# Retrieval evaluation

> **Update 2026-11-02 (search v2):** `search` now drops common question words and appends `OR` matches when fewer than three notes match every word. The figures below describe the earlier behavior. Current results: [synthetic fixture](evals/retrieval-v1-search-v2.json), [EPL summary](evals/epl-retrieval-search-v2-summary.json).

This report preserves the earlier retrieval baseline. The implemented automatic query path and its separate evaluation are documented in [QUERY_RETRIEVAL.md](QUERY_RETRIEVAL.md) and [QUERY_RESULTS.md](QUERY_RESULTS.md).

The current retriever works for compact keyword queries but misses most wording changes. On this synthetic fixture, direct search has **43.5% mean Recall@5**; a bounded search-and-links workflow has **48.1%**. Neither mode retrieved evidence for the 12 verbatim natural-language questions or the six typo queries. This is a baseline for improving retrieval, not a production accuracy estimate.

## Reproduce

From the repository root, with Rust and Python 3 installed:

```sh
cargo build --release --locked
python3 tests/retrieval_metrics_test.py
python3 scripts/evaluate_retrieval.py --output /tmp/jbrain-retrieval.json
python3 scripts/evaluate_retrieval.py --distractors 1000 --output /tmp/jbrain-retrieval-large.json
```

The evaluator starts a temporary native service, seeds it through HTTP, runs the queries with four concurrent workers, then stops the service and removes its temporary storage. It does not access your existing brain. All queries run against a fixed corpus after seeding; ranking during concurrent corpus changes is not tested here. No model, embeddings, API key or extra Python package is needed. Evaluation adds no dependencies to the Rust service.

Recorded runs from 2026-10-24 on macOS arm64:

- [226-document baseline](evals/retrieval-v1-baseline.json): 26 core documents and 200 deterministic distractors.
- [1,026-document run](evals/retrieval-v1-1000-distractors.json): the same core documents and 1,000 distractors.
- [Interactive case report](evals/retrieval-v1-report.html).

Each JSON report includes fixture, complete corpus, evaluator and binary hashes, policies, metrics and every query's expected documents, retrieved ranking, verified evidence and missing evidence. Latency is recorded for diagnosis; this small evaluation is not a load benchmark.

## What is tested

[The fixture](../tests/fixtures/retrieval-v1.json) contains fictional projects, people, policies, incidents and operational notes. It includes aliases, links, an archived database proposal, an old budget, diacritics and Japanese text. Deterministic planning notes add overlapping vocabulary without adding answers.

There are 60 queries: 54 answerable and six with no answer in this corpus. Each answerable query has manually labeled evidence-bearing documents and exact source quotations. The evaluator checks those quotations against the fixture and reads actual returned documents to verify their content. Unjudged documents count as irrelevant. Labels concern evidence for the particular question, rather than general topical similarity.

Two fixed retrieval policies receive only the query and service responses; neither uses relevance labels or expected answers to retrieve documents:

1. **Direct search:** send the query verbatim to `/v1/search`, requesting ten results.
2. **Search + links:** keep the first three search results, append their resolved outgoing links and backlinks once, then fill any remaining places with direct results. Deduplicate and stop at ten documents. This uses at most four retrieval HTTP calls. Link traversal is implemented in the evaluation harness to simulate an agent using existing APIs; it is not automatic expansion in the search endpoint.

The six multi-document cases deliberately use a compact entity or alias as the search query, paired with a separate human question requiring two documents. They test evidence expansion from a known starting entity. They do not test whether an agent can turn a natural-language question into that starting query. Other categories send their query wording unchanged. Source-validation reads are excluded from retrieval call counts and timing.

## Baseline results

Recall@5 means the fraction of labeled relevant documents present among the first five results, averaged equally across answerable queries. Both corpus sizes produced the same scores below.

| Query category | Cases | Direct Recall@5 | Search + links Recall@5 |
| --- | ---: | ---: | ---: |
| Compact keywords | 12 | 100.0% | 100.0% |
| Aliases, prefixes and Unicode | 8 | 87.5% | 87.5% |
| Verbatim natural-language questions | 12 | 0.0% | 0.0% |
| Paraphrases | 10 | 10.0% | 10.0% |
| Typos | 6 | 0.0% | 0.0% |
| Multiple documents, entity query | 6 | 58.3% | 100.0% |
| **All answerable queries** | **54** | **43.5%** | **48.1%** |

| Aggregate metric | Direct search | Search + links |
| --- | ---: | ---: |
| Hit@1 | 46.3% | 46.3% |
| Hit@5 | 48.1% | 48.1% |
| Mean Recall@5 | 43.5% | 48.1% |
| Complete required evidence@5 | 38.9% (21/54) | 48.1% (26/54) |
| nDCG@5 | 0.440 | 0.471 |
| MRR@10 | 0.472 | 0.472 |

For multi-document cases specifically, complete required evidence@5 improved from **1/6 to 6/6** with links. Hit@5 alone would hide the improvement: a query already counts as a hit when only one of its two relevant documents is found.

All six unknown-answer queries returned no results in both modes. This is an empty-result rate, not a measure of a model's abstention or hallucination behavior. Four separate freshness checks passed at both corpus sizes: updated content was readable, its new term was searchable, its old term disappeared, and deletion removed its new term.

## Failure examples and implications

- `Atlas primary database` finds the current architecture document first. `Which database does Atlas use in production?` returns nothing, although its evidence exists.
- `Who owns Atlas?` returns nothing; `Atlas owns` finds the owner document. Search requires every query token to match a word prefix, including words such as `who`. It does not remove question words, stem words, correct spelling or expand synonyms.
- `Atals database` returns nothing. Prefix matching does not repair a misspelled entity.
- The Japanese substring query `避難場所` misses its document. The current tokenizer does not provide general substring matching for continuous Japanese text.
- The alias query `atlaspay` finds the project, but direct search does not provide the owner's city. Following its person link supplies the second document needed for that question.

The next changes worth testing are bounded question normalization, relaxed term matching with exact entity preference, alias/spelling recovery and better link selection. Synonym and multilingual handling need their own checks. These can be investigated without adding an embedding model. Preserve this fixture as a regression set, then use a separate held-out set of real questions to assess generalization.

## Metric definitions and limits

The runner reports cutoffs of 1, 5 and 10. **Hit@K** is one if any relevant document appears. **Recall@K** is the proportion of relevant documents found. **Precision@K** divides the relevant results by K, including when fewer results are returned. **Complete required evidence@K** is one only if every required evidence document appears. **MRR@10** is the reciprocal rank of the first relevant result, or zero. **nDCG@K** discounts lower-ranked results using gain `2^grade - 1` and `log2(rank + 1)` with ranks starting at one. Grades can be 1 (supporting) or 2 (direct evidence); this fixture currently uses grade 2.

Answerable-query metrics are macro averages over the 54 queries. Unknown-answer cases are excluded and reported separately. The metric unit tests use hand-calculated rankings, including partial evidence and empty results.

This is a small, public, hand-authored diagnostic set. Its queries and judgments were authored with the corpus, and its category mix determines the overall score. The additional distractors are repetitive notes on unrelated named projects; unchanged scores at 1,026 documents do not establish robustness against realistic near-duplicates, contradictory revisions or millions of documents. The fixture includes an archived proposal and budget but does not exhaustively evaluate temporal reasoning. No generated answers, external agent query planner, GBrain installation or real personal corpus was evaluated. The results measure document retrieval, not end-to-end answer accuracy or a comparison with GBrain.

To evaluate your workload, copy the fixture schema, supply your own OKF documents, real questions and manually checked evidence quotes, and pass `--fixture /path/to/fixture.json --distractors 0`. Keep a separate set of questions that you do not inspect while changing retrieval. Avoid committing private knowledge or evaluation outputs containing it.
