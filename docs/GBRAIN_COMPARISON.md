# Matched EPL retrieval comparison — 2026-10-26

This is a same-corpus, same-question comparison between JBrain and GBrain. It measures **which stored pages appear in the first five distinct results**, not whether an LLM ultimately answers correctly. GBrain was tested first without embeddings and then with its vector, keyword and graph paths active.

Both systems indexed the same frozen 13,989 EPL Markdown documents. JBrain had 111,384 parsed links; the isolated GBrain import reported 13,989 imported pages, zero errors, and extracted the same 111,384 links. GBrain was installed locally at **0.60.402.0** and used an isolated PGLite brain. JBrain used the existing Rust/SQLite trial service and neither `/v1/search` nor `/v1/query` called Luna.

## Keyless baseline

The initial GBrain run used **balanced** search with no embeddings, query expansion, model key or paid reranker. Every retrieval call reported zero provider calls.

The frozen familiar fixture has 66 answerable and six unsupported question variants. The harder EPL fixture has 36 answerable and 12 unsupported questions. Source-derived gold defines one or more required evidence groups per question; an equivalent season table or club summary can satisfy a group. **Complete page evidence@5** requires at least one valid source page for *every* group among the first five distinct pages. The unsupported cases are kept in the trace but have no positive page-recall denominator. Both systems received only the original question, a ten-result cap, and the same source scope; no manually prepared keywords, gold IDs or expected values were passed to retrieval.

| Retrieval entry point | Familiar complete@5 | Hard complete@5 |
| --- | ---: | ---: |
| JBrain raw `/v1/search` | 14/66 (21.2%) | 2/36 (5.6%) |
| GBrain `search`, balanced, keyless | 47/66 (71.2%) | 10/36 (27.8%) |
| JBrain automatic `/v1/query` | **66/66 (100%)** | **21/36 (58.3%)** |
| GBrain `query`, balanced, keyless | 47/66 (71.2%) | 10/36 (27.8%) |

The result is conditional: **GBrain is substantially stronger at raw question-to-page search; JBrain's EPL-aware question planner is stronger at top-five page selection on these fixtures.** JBrain's hard-set 58.3% is far from the user's >97% answer-quality target. In the hard set, both question-oriented paths missed all four implicit-entity questions; JBrain also missed all four multi-date and all four league-wide ranking questions at the page@5 level. JBrain's deterministic evidence tools can handle some of these when given the right explicit plan, but that is a different test.

The earlier conservative-mode GBrain run scored 38/66 and 9/36 for `search`, and 41/66 and 8/36 for `query`. Balanced mode is the primary comparison because it is the stronger keyless configuration in this run. It made zero provider calls too.

## Vector-backed GBrain

The same isolated GBrain brain then enabled `openai:text-embedding-3-large` at its existing 1,024-dimensional vector width, switched to `balanced` mode before the final full backfill, and embedded all **14,213 chunks across 13,989 pages** with title contextual wrapping. The backfill reported zero failures; `stats` showed 14,213 embedded chunks and `embed --stale --dry-run` found zero remaining. Balanced mode activated graph signals and relational retrieval. A private OpenAI key supplied query embeddings; GBrain's live responses recorded a vector stage and an embedding provider call for 119/120 `search` and 117/120 `query` requests. The remaining requests did not make a provider call. Query expansion stayed at GBrain's shipped default, off. GBrain reported the Voyage `rerank-3-lite` reranker **enabled but not ready** because no `VOYAGE_API_KEY` was present, so these results do **not** include cross-encoder reranking.

| Retrieval entry point | Familiar complete@5 | Hard complete@5 |
| --- | ---: | ---: |
| JBrain raw `/v1/search` | 14/66 (21.2%) | 2/36 (5.6%) |
| GBrain `search`, keyless | 47/66 (71.2%) | 10/36 (27.8%) |
| GBrain `search`, vectors + keyword + graph | **58/66 (87.9%)** | **25/36 (69.4%)** |
| JBrain automatic `/v1/query` | **66/66 (100%)** | 21/36 (58.3%) |
| GBrain `query`, vectors + keyword + graph | 58/66 (87.9%) | **23/36 (63.9%)** |

The vector-backed GBrain `search` gained 29 answerable cases and lost three versus its own keyless run; `query` gained 27 and lost three. Against JBrain's planner, GBrain `search` found four fewer complete page sets across all 102 answerable questions (83 versus 87), with nine paired GBrain-only successes and 13 JBrain-only successes. This small overall difference is not a general accuracy ranking, especially on development fixtures that informed JBrain's EPL adapter. The systems have complementary hard-set strengths: JBrain `/v1/query` found five of six multi-club composition cases versus zero for GBrain, while GBrain `search` found all four multi-date match cases and three of four league-comparison cases versus zero for JBrain. Both missed all four implicit-entity cases. Neither reaches the requested >97% target on this hard set.

GBrain's returned query-cost estimates sum to about **$0.00048** for the 236 embedding calls across both retrieval entry points. This is a rounded GBrain estimate, not a provider invoice. The 14,213-chunk backfill's exact token spend was not returned by GBrain; the completed run used a **$2 spending cap**. An earlier partial backfill was interrupted when the title-wrapping mode was corrected, so that exploratory spend is separate. OpenAI currently lists `text-embedding-3-large` at [$0.13 per million input tokens](https://developers.openai.com/api/docs/models/text-embedding-3-large). GBrain's vector-backed PGLite directory occupied about 1.1 GB after the run; storage grows with the 1,024-dimensional vectors and database internals.

An additional hard-set pilot forced GBrain's optional query expansion using `openai:gpt-4.1-nano`. Six questions completed, then the seventh exceeded the 90-second per-query timeout. The incomplete pilot is excluded from the table; its 36-question score cannot be inferred. The isolated brain's expansion-model override was removed afterward.

Do not infer final-answer accuracy from page counts. A selected page can contain the right fact outside the returned chunk; a model may still misread it, cite it incorrectly or fail a calculation. Our recent Luna smoke test answered one simple question and rejected one hard ranking answer for invalid citations. This is not a blind multi-domain holdout. A GBrain run with a Voyage key could move the ranking further in either direction on this corpus, and must be measured rather than assumed.

The trace includes observed timings. JBrain measurements are warm HTTP requests to a persistent daemon. GBrain measurements are separate CLI invocations with its own startup/connect costs, plus CLI-reported operation time. Those paths are not equivalent enough for a speed win claim. A fair latency comparison should keep both services resident, warm both, issue the same query mix and concurrency, and report p50/p95 and successful-evidence throughput. For retrieval *quality*, the next decisive test is an independently authored, source-checked holdout spanning EPL and another domain, with page@5, **actual excerpt/fact coverage**, abstention, citation support, final answer correctness and provider dollars measured separately.

Reproduce the keyless scoring with `python3 scripts/compare_gbrain.py --gbrain-mode balanced` after preparing an isolated GBrain home and importing the frozen Markdown with `--no-embed`. Extract links with `gbrain extract links --source db --source-id epl`; the keyless benchmark aborts if a GBrain retrieval reports a provider call. To reproduce the vector run, enable embeddings, switch to balanced mode, backfill the frozen source, and pass `--openai-key-file` plus `--reuse-jbrain .jbrain/gbrain-comparison/evidence-results-balanced.json` to the same script. Its result is `.jbrain/gbrain-comparison/evidence-results-embedded-balanced.json`. Earlier machine-readable artifacts are `evidence-results-balanced.json`, `evidence-results-conservative.json`, `import-summary.json` and `link-extraction.json` in that directory. The JBrain corpus digest and both fixture digests are embedded in each evidence result. GBrain's [retrieval architecture](https://github.com/garrytan/gbrain/blob/master/docs/architecture/RETRIEVAL.md) describes the vector and reranking stages.
