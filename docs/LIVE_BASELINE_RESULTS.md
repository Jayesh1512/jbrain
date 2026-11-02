# Real-model EPL development baseline

Run completed 2026-10-26 with `gpt-6-astra` for both answer generation and separate verification. This is a historical development baseline; the EPL trial daemon was subsequently switched to low-cost `gpt-6-luna` for both roles. The first connection failure remains in the 24-question result. The old key file was not touched.

**Result: 11/16 answerable questions correct (68.75%).** The implementation-author source review also found 5/6 appropriate responses to unsupported questions and 2/2 appropriate clarifications, giving 18/24 overall (75%). These are provisional development labels, not independent human judgments or a held-out evaluation. The >97% release goal is not met.

| Measure | Observed |
|---|---:|
| Answerable correctness | 11/16 = 68.75% |
| Overall correctness | 18/24 = 75% |
| Unsupported handling | 5/6 = 83.33% |
| Ambiguous handling | 2/2 = 100% |
| Accepted factual answers with source-supported claims | 11/11 in this provisional review |
| Provider calls / local evidence tools | 192 / 139 |
| Provider calls with usage receipts | 191/192 |
| Wall latency, p50 / p95 / max | 41.1 / 59.7 / 92.8 s |
| Known priced API subtotal | $6.6677 for the 24 requests |
| Exact total cost | Unknown: first failed call has no usage receipt |
| Sampled daemon RSS, median / peak | 13.45 / 18.52 MiB |

The 16 answerable reference values were recomputed from hash-verified source CSVs after the run, with zero model or retrieval calls. The model received only the frozen question text, without expected answers, typed plans, source requirements or source calculations. A source review bound each judgment to the exact response hash. It was performed by the implementation author and is therefore provisional. The 24 familiar development questions cannot establish a 97% population accuracy claim; a fresh independent holdout is still required.

## Case results

An answerable abstention or provider failure is a miss. For unsupported cases, a specific and truthful explanation of the corpus limit can be correct even when the service returns `needs_verification`; a blank answer is a miss. "Cost" is known usage only and excludes the first failed call.

| Case | Source-backed expectation | Returned status | Review | Seconds | Calls | Cost |
|---|---|---|---|---:|---:|---:|
| `away_rank` | Arsenal & Brighton tied, 35 | `needs_verification` | **miss** | 25.9 | 1 | unknown |
| `calendar` | Man United 11 away wins | `needs_verification` | **miss** | 57.8 | 12 | $0.279 |
| `halftime` | Arsenal 10 wins from level HT | `needs_verification` | **miss** | 56.1 | 10 | $0.498 |
| `unbeaten` | Newcastle 16; GD +19 | `answered` | correct | 49.9 | 10 | $0.309 |
| `two_seasons` | Liverpool 24 goals conceded | `answered` | correct | 46.1 | 9 | $0.310 |
| `home_compare` | Villa 12, Brighton 10; Villa +2 | `needs_clarification` | **miss** | 34.3 | 4 | $0.097 |
| `three_facts` | Spurs–City HT 1–0, FT 1–0, SOT 3–5 | `answered` | correct | 40.7 | 6 | $0.148 |
| `opponent` | Arsenal; 26 away goals | `answered` | correct | 43.7 | 8 | $0.251 |
| `away_losses` | Forest 13, Everton 8 | `answered` | correct | 41.1 | 8 | $0.309 |
| `split_draws` | Chelsea home 4, away 5 | `answered` | correct | 33.6 | 9 | $0.290 |
| `negation` | Burnley 16 away non-wins | `answered` | correct | 38.3 | 8 | $0.230 |
| `zero` | Southampton 1 away win | `answered` | correct | 29.9 | 7 | $0.201 |
| `ties` | Man United alone, 15 draws | `needs_verification` | **miss** | 40.3 | 7 | $0.153 |
| `sizes` | 42/22 clubs vs 38/20 clubs | `answered` | correct | 59.7 | 12 | $0.553 |
| `cross_compare` | Leicester 6, Chelsea 5; +1 | `answered` | correct | 50.2 | 9 | $0.343 |
| `premise` | City home 41, away 45; away +4 | `answered` | correct | 92.8 | 16 | $1.053 |
| `official` | Explain official adjusted total absent | `unsupported` | correct | 43.3 | 10 | $0.276 |
| `player` | Explain scorer/minute absent | `needs_verification` | **miss** | 54.6 | 10 | $0.337 |
| `xg` | Explain xG absent | `needs_verification` | correct | 55.6 | 9 | $0.396 |
| `cup` | Explain FA Cup absent | `unsupported` | correct | 32.0 | 6 | $0.164 |
| `europe` | Explain Europe/attendance absent | `unsupported` | correct | 32.5 | 7 | $0.166 |
| `outside` | Explain 1992/93 absent | `unsupported` | correct | 25.4 | 6 | $0.152 |
| `ambiguous_season` | Ask club and season | `needs_clarification` | correct | 23.0 | 4 | $0.068 |
| `ambiguous_clubs` | Ask canonical clubs/metrics | `needs_clarification` | correct | 28.0 | 4 | $0.085 |

## What failed

- `away_rank`: a provider connection failure on the first question. The request may still have incurred a charge; no usage receipt is available.
- `calendar` and `ties`: the model reached relevant evidence but generated citations the deterministic checker rejected. No draft answer was accepted, even though the source gold is available. Citation formatting is an end-to-end failure mode.
- `halftime`: the verifier correctly identified that an all-wins compute receipt lacked half-time scores. The needed scores are stored in individual match documents, but the workflow did not retrieve and aggregate them.
- `home_compare`: the workflow asked whether the question referred to the men’s Premier League or women’s Super League. This was an unnecessary clarification under this brain’s EPL-only corpus and the frozen task scope.
- `player`: the verifier recognized that goal scorers and goal-event minutes are absent, but the service returned a null answer after repair. The user received no useful explanation.

The other 11 answerable responses match the frozen source values and their accepted citation receipts. The `xg` response is counted correct because it explicitly says that xG values and an identified xG source are missing; it does not substitute actual goals. The two ambiguous questions correctly ask for the missing team or season.

## Model usage and resource cost

All 191 metered calls resolved to the `gpt-6-astra` alias. Reported usage totals were 918,236 input tokens, of which 534,145 were cached reads and 376,741 were cache writes, plus 27,015 output tokens. That leaves 7,350 ordinary uncached input tokens. At [OpenAI’s model rates checked on 2026-10-26](https://developers.openai.com/api/docs/models/gpt-6-astra) for standard short-context use ($10/M ordinary input, $1/M cached read, $12.50/M cache write, $50/M output), the components are about $0.07, $0.53, $4.71 and $1.35 respectively. Cache writes dominate the bill. This is an estimate from API usage, not a billing export; service-tier, discounts and any charge for the failed request remain unknown.

The known subtotal averages $0.278 per question in this small case mix. Scaling that observed subtotal mechanically to 1,000 questions/day gives **at least about $278/day or $8,335 per 30 days**, plus any charge from calls without usage. It is a planning illustration, not a production price prediction. It excludes ingestion, storage and hosting. The evaluator deliberately leaves its exact cost projection null while one call is unmetered.

The daemon’s RSS was sampled every two seconds during the active run (533 samples over 1,072 seconds, beginning after the first question). Median was 13.45 MiB; observed maximum was 18.52 MiB. Its recorded CPU time increased by 7.97 seconds during those samples, roughly 0.7% of one core over the interval. The sampler does not measure OpenAI’s remote compute or client-process resource use. The 6.91 MiB release binary and one-daemon SQLite architecture remain small locally, but API use and latency are currently high.

## Next changes to evaluate

1. Add native, bounded date-range and half-time predicates to the EPL match compute tool, with completeness receipts. This addresses `calendar` and `halftime` without asking the model to read every match individually.
2. Make tie-aware rank results and citation templates easier to use; return actionable draft-validation diagnostics internally so one repair can fix malformed pointers and quotes before rejection.
3. Ground team and competition resolution in the explicit corpus catalog before asking clarification. Preserve clarification only when the stored scope cannot resolve the reference.
4. Preserve a verified abstention explanation through repair and status handling. The `player` question should return a clear unsupported response.
5. Reduce repeated evidence/context and provider calls after accuracy repairs. A cheaper generator or verifier should be tested on these development cases, then chosen using accuracy and complete workflow cost; there is no measured basis yet to claim a cheaper model wins.
6. Freeze the improved code, model and corpus before an independently authored, source-reviewed holdout across multiple domains. The release gate in [the accuracy workflow](ACCURACY_WORKFLOW.md) remains unchanged.

## Reproducibility

- Frozen binary SHA-256: `f7404c24899dac57934a9ae2f51ddd228cf0fe7a1b6c751d537d42868b34e758`.
- Canonical corpus SHA-256: `ebbac42a3b13fcd4198636606de6a9e33de056a8cf487a9f4e2823ac86a4c602` (13,989 documents, including 12,324 EPL matches).
- Frozen question SHA-256: `f67ac776a2b4ab06179cad980be2a2c1654d8215c8502d286bee282335f9b97d`.
- Frozen gold SHA-256: `8b39d86743749450dac3075bc9f1b6e6b8f4a6f4dbfe26cd9394ac9120ebd821`.
- Run artifacts (ignored from Git): `.jbrain/live-baseline-v1/` contains questions, separate gold, source recheck, all hashed responses, provisional judgments, prices, score, and resource samples. The source fixture and full CSV files are preserved under `.jbrain/challenge-v3/` and `.jbrain/epl-trial/sources/`.
- Storage concurrency was tested separately with 1, 4 and 16 concurrent HTTP clients: all 210 writes committed after safe compare-and-swap retries; 84 duplicate submissions returned matching receipts. This is a storage test, not a simultaneous model-answer accuracy test.
