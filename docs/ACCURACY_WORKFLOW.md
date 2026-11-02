# Accuracy workflow and the 97% release gate

The accuracy-first workflow is implemented. A 24-question real-model **development baseline** has now been measured with `gpt-6-astra` as generator and verifier; see [the live baseline results](LIVE_BASELINE_RESULTS.md) and [targeted repair experiments](REPAIR_RESULTS.md). The frozen first pass achieved 11/16 correct answerable questions under a provisional implementation-author source review. The separate repairs do not constitute a new full-run score. This remains below the >97% target and is not an independent holdout or release result. Simulated-provider tests establish protocol/control behavior, not language understanding or factual accuracy.

Use `POST /v1/ask`, `jbrain ask`, or MCP `brain_ask` for the same daemon-owned model workflow. All agents share the configured model and verifier. Native evidence tools also work without a model through `POST /v1/evidence`, `jbrain evidence`, and `brain_evidence`.

## What changed

1. A remote model decomposes the original question into requirements with literal question quotes, subjects, dates/periods, competition, grain, operation and qualifiers. The plan is provisional; structured output does not establish semantic correctness.
2. A local catalog exposes stored document kinds, date/period ranges, scalar metadata fields/values and metrics with their declared bases/units. Catalog enumeration is bounded and reports its limits.
3. The model can find contextual passages, read revision-cited sources, follow links, and refine the search. It can resolve an opponent from one match before looking up that opponent's season. Tools accept typed, parameterized filters, never SQL or shell commands. Source content is explicitly untrusted data.
4. Rust computes sums, minima, maxima, descending ranks, EPL venue/outcome subsets, and league-wide match ranks over **all documents in the requested scope**, with a 1,000-document limit. A match rank enumerates the entire season, returns every tied leader, and rejects a club or date filter that would silently narrow the league. Missing metrics, different bases, stale/draft/deprecated inputs, absent data and overlarge scopes are errors. No top-k aggregate is accepted. Decimal sums and ordering use checked `i128` decimal arithmetic over stored JSON values; excessive precision/overflow is rejected. An outcome's zero count is accepted only when actual club/venue input matches exist.
5. Each final factual claim cites an evidence ID, JSON pointer and exact quote. Rust checks pointer existence, quote/value agreement, source revisions, and explicit stale/draft/deprecated status. A separate model invocation sees the **original question**, candidate answer and evidence, without the planner or generator history. It checks missing conditions and unsupported claims. One repair and recheck are allowed within the budget.
6. A revision conflict returns `status: retryable`, `complete: false`. Calculation receipts additionally detect any intervening publication, including added/deleted inputs, using the in-process index generation. This is conservative: even unrelated writes can invalidate a calculation. The caller can retry. It is optimistic validation, not a transaction held open during inference.

The verifier is a separate invocation, using the same configured model by default or `JBRAIN_VERIFIER_MODEL` when set. Same-model errors can correlate. Verification is an additional check, not a proof or an accuracy guarantee. Independent held-out labels remain necessary.

`/v1/query` is retained for inexpensive retrieval. Its former `complete` flag incorrectly described matching its own interpreted plan. **Breaking response change:** `complete` and `answer_supported` are now always false there; `plan_satisfied` carries the former plan-matching result. A satisfied plan has `status: needs_verification`. Historical evaluation reports describe their frozen old binaries and remain unchanged. This contract correction alone does not improve answer accuracy and must not be scored as such.

## Configuration

Configure the **daemon**, then restart it. Credentials are read once, never returned in API results or written into the knowledge corpus. Use an existing private API key file outside this repository, or supply `OPENAI_API_KEY` in the daemon environment. Do not send a key in a chat message or pass it as a CLI argument.

```sh
export JBRAIN_MODEL=gpt-6-luna
export JBRAIN_API_KEY_FILE=/absolute/path/to/private/provider-key
# Optional: a separately evaluated verifier model.
# export JBRAIN_VERIFIER_MODEL=<model-id>
./target/release/jbrain serve
```

The model name is explicit; there is no silent expensive-model fallback. The EPL trial launcher pins both generator and verifier to `gpt-6-luna` by default, even if a shell still exports an old model name. `gpt-6-astra` was used for the historical development baseline only. [OpenAI lists Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) at $0.10/M input, $0.01/M cached input, $0.125/M cache writes and $0.50/M output for standard short-context text requests; these rates can change. [GPT-4.1 nano was shut down on October 23, 2026](https://developers.openai.com/api/docs/deprecations). Measure Luna's actual accuracy and whole-workflow cost before pinning a release model. Capture resolved model IDs reported by the provider; aliases can move. This adapter uses the OpenAI Responses API, strict function tools and strict JSON outputs. A different endpoint must support those exact semantics; generic chat-completions compatibility is insufficient.

For the prepared EPL trial, start the daemon with `python3 scripts/epl_trial.py start --key-file /absolute/path/to/private/provider-key`. The default model is `gpt-6-luna`; `--model` changes both roles only when explicitly supplied. The key stays outside the repository and is never printed by the launcher.

`OPENAI_BASE_URL` defaults to `https://api.openai.com/v1`. It is administrator-only configuration. HTTPS is required except explicit loopback HTTP for tests. Redirects are disabled. Without both a model and key, `/ask` returns `503 MODEL_NOT_CONFIGURED` and makes no model call; the storage and evidence APIs remain available.

The workflow admits two active questions; excess receives `429 ASK_BUSY`. Each question is bounded to 16 provider calls, 24 local tool calls, 240 seconds total, 90 seconds per provider request, 5,000 output tokens per call, 384,000 context bytes per call, and 192,000 retained evidence bytes. Budget exhaustion yields an unaccepted result. The longer deadline is a development response to observed provider I/O timeouts; it raises worst-case latency and does not guarantee completion. A failed/abandoned provider request may still be billed; missing usage is reported as unknown rather than zero.

No local inference engine, embedding model or vector database is added. Network waits run on at most two blocking workers; the Tokio loop and durable writer remain available. SQLite locks are acquired only while executing each local tool. Storage remains one owning daemon with many concurrent agent clients, CAS revisions, request-ID deduplication and bounded writer admission.

## Evidence tool arguments

All endpoints use `{ "tool": "<name>", "arguments": {...} }`. CLI example: `jbrain evidence read --file arguments.json`. MCP uses `brain_evidence` with the same wrapper. The model's exact function schemas are in `src/evidence.rs::definitions`.

- `catalog`: `{ "field": null, "prefix": null }`. Supply a scalar metadata field and optional literal value prefix to enumerate up to 100 exact values.
- `find`: `{ "text": "rotation", "scope": {"kind":"PlanetFact","period":null,"date":null,"entities":["Venus"],"filters":[]}, "limit": 10 }`. All scope conditions are ANDed. Metadata filters are `{"field":"home_team","value":"Arsenal"}`. Empty text browses deterministically. Nonempty text uses ORed lexical terms over sections with title, aliases and selected metadata as context. Results are candidates, not answer support. `has_more` indicates truncation; multiple passages can refer to one document.
- `read`: `{ "id": "epl/club-seasons/2016-17/bournemouth", "offset":0, "length":16000 }`. Offsets/lengths are UTF-8 bytes. Follow `next_offset` to read the rest. Returns source metadata, typed facts with bases and pointers, body excerpt, revision and body length. It does not assume a retrieved sentence establishes the whole answer.
- `links`: `{ "id": "epl/seasons/2016-17" }`. Read relevant resolved neighbors; link existence alone does not establish facts.
- `neighbors`: `{ "id":"epl/seasons/2016-17", "direction":"outgoing", "kind":"ClubSeasonSummary", "period":"2016/17", "entity":"", "limit":20 }`. Filters apply before the limit; empty strings mean no filter. Use this for large hubs whose full `links` response would exceed the model tool-output budget.
- `compute`: see examples below. Computations return full input receipts with revisions and a result. They establish a calculation over stored data, not real-world source completeness. Outcome receipts mark included/excluded matches. Ties in a descending rank retain equal values, with IDs as the stable secondary order.

```json
{
  "tool": "compute",
  "arguments": {
    "operation": "matches",
    "scope": {
      "kind": "Match", "period": "2016/17", "date": null,
      "entities": ["Bournemouth"],
      "filters": [{"field":"dataset","value":"epl-results-v1"}]
    },
    "metric": null, "basis": null,
    "club": "Bournemouth", "venue": "home", "outcome": "win"
  }
}
```

This computes nine home wins from the stored fixtures. It does not read the club's twelve total wins as the home answer. The match adapter is explicitly for the EPL importer; a new domain's structured events need a declared adapter, not football assumptions hidden in generic prose retrieval.

For a calendar-year EPL subset, run `matches` separately for each overlapping season with `date_from` and `date_to` set to the requested inclusive YYYY-MM-DD bounds, then combine the complete receipts. Set `half_time: "level"` together with `outcome: "win"` to count wins from level half-time scores. These three new fields are null when unused. The receipt includes each club-and-venue match examined with its date, score, optional half-time score and `included` flag; missing half-time data is an error. For generic numeric operations, use `operation: sum|min|max|rank`, the exact metric and basis, and null club/venue/outcome/date_from/date_to/half_time. Example scope `kind: PlanetFact`, metric `rotation_period`, basis `hours_sidereal` preserves signed values and rejects mixed units. A null/unknown source value is not zero. EPL points are earned from match results **before administrative deductions**, not necessarily official standings. Player scorers, managers, xG and cup results are absent from the EPL corpus.

For a league-wide EPL match statistic such as most away goals, use `operation: "match_rank"`, `scope.kind: "Match"`, one season period and only the `dataset=epl-results-v1` filter, `metric: "goals_for"`, `venue: "away"`, and null club/outcome/basis/date_from/date_to/half_time. The native result includes `maximum`, every tied `leader`, the complete ranked club set and a revision receipt for every stored match in that season. It rejects a club-filtered scope; the result proves a ranking over the stored season, subject to source coverage. `/v1/ask` retains every revision internally but sends the model the ranked result, input count and SHA-256 of the full input receipt. This keeps the 380-match source list out of repeated model context while retaining a native audit path and write-time freshness checks.

## Evaluation and model selection

Keep three distinct layers: backend capability with oracle interpretations, simulated-provider protocol tests, and actual-model final-answer accuracy. Only the third can substantiate the user's target. Existing hard EPL/NASA questions have been inspected and are development regressions, never a blind holdout. Do not create hundreds of near-duplicate templates and count them as independent questions.

1. Freeze source snapshots. Independently author diverse development questions with source-backed answers, including paraphrases, implicit references, several dates, negation, home/away, units, missing values, rankings, contradictions and unsupported grain/competition.
2. Run real model configurations against those development cases with the same corpus and workflow. Independently judge final answers, all conditions, citation support, abstention, latency and actual token usage. Select the best accuracy/cost configuration from measured results; do not assume nano or a larger model is the winner.
3. Freeze code/binary, prompts, model configuration and corpus. Obtain a fresh independently authored source/domain-separated holdout: at least 1,000 answerable, 300 unsupported and 200 ambiguous cases across at least two domains. Preserve author/reviewer provenance and source answers. Avoid model-generated labels approved only by the same model.
4. Gate on answerable accuracy at least 98.5%, its 95% Wilson lower bound **above 97%**, and overall correctness at least 98.5%. Require at least 99% citation support, answers without unsupported claims, correct unsupported handling and correct ambiguity handling. An abstention on an answerable question, a provider failure or a truncated workflow counts as a miss. Review per-category and per-domain results as well; an overall average must not conceal a weak important category.

The runner sends only each question to the service. Cases require `id`, `question`, `domain`, `class` (`answerable`, `unsupported`, `ambiguous`) and may include private source gold. Preserve a separate manifest containing the corpus digest, binary digest, model configuration, holdout author, review protocol and independent-holdout attestation. Scoring requires one source-reviewed human judgment per response, tied to its response hash:

```json
{"id":"case-001","response_sha256":"<from result>","reviewer":"<independent reviewer>","correct":true,"citations_correct":true,"unsupported_claims":false,"notes":"<source and scope checks>"}
```

```sh
python3 scripts/evaluate_ask.py run --cases development.jsonl --output /tmp/model-a.jsonl
python3 scripts/evaluate_ask.py score --results /tmp/model-a.jsonl --judgments /tmp/reviews.jsonl --prices /tmp/dated-prices.json
# After development is frozen and the independent cases exist:
python3 scripts/evaluate_ask.py run --partition holdout --provenance holdout-manifest.json --cases holdout.jsonl --output /tmp/holdout-run.jsonl
```

Provenance field names are `binary_sha256`, `corpus_manifest_sha256`, `model_configuration`, `holdout_author`, `review_protocol`, and `independent_holdout_attestation: true`. Attestation is a human process requirement; setting a JSON boolean cannot establish independence. The tool rejects small/development sets as a release gate and binds labels to exact responses. It never labels its own answer correct.

Each `/ask` result reports actual per-call usage and resolved model IDs when provided. Supply dated prices keyed by those IDs, with `input_per_million`, `output_per_million`, optionally `cached_input_per_million` and `cache_write_per_million`. Scoring projects measured whole-workflow API cost to **1,000 questions/day and 30,000/month**, including planner, generator, repair and verifier calls. If a provider call has no usage receipt, the exact total and projections are null while the known priced subtotal remains visible. Ingestion, storage and hosting are separate. The measured development subtotal and its limitations are in [the live baseline results](LIVE_BASELINE_RESULTS.md).

Official API references checked during implementation: [function calling](https://developers.openai.com/api/docs/guides/function-calling), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [evaluation practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices), [current low-cost model](https://developers.openai.com/api/docs/models/gpt-6-luna). Structured outputs enforce shape, not factual correctness. Recheck supported model versions, pricing and account access before the real comparison.
