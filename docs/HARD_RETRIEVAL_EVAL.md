# Difficult retrieval evaluation — 2026-10-26

**The earlier 100% diagnostic result does not generalize to these harder questions.** The unchanged service often finds related documents but drops part of the requested meaning. Its `complete` field cannot currently be treated as assurance that the original question has been supported.

This run froze 64 new questions and source-derived gold before sending requests: 48 EPL cases against the existing 13,989-document corpus, plus 16 questions against a separate 11-document corpus converted from the [NASA metric planetary table](https://nssdc.gsfc.nasa.gov/planetary/factsheet/) and its [definitions](https://nssdc.gsfc.nasa.gov/planetary/factsheet/planetfact_notes.html). The NASA table is dated 18 March 2025; the corpus preserves that snapshot, units, negative rotation values, unknowns, and Moon-specific reference frames. It is not a live 2026 planetary database.

The Rust binary and EPL corpus were unchanged. No server model, embeddings or separate model API calls were used. The conversational agent that authored the evaluation has unmeasured model usage; zero refers to retrieval calls. The temporary planetary brain was removed after the test. The real user-question ledger was not modified.

## Results

| Measure | EPL | Planetary data |
| --- | ---: | ---: |
| Total cases | 48 | 16 |
| Answerable from stored data | 36 | 12 |
| Complete relevant documents in first 5 | 21/36 (58.3%) | 7/12 (58.3%) |
| All requested fact values or computation inputs returned | **9/36 (25.0%)** | **5/12 (41.7%)** |
| Same measure, excluding four simple controls per domain | **5/32 (15.6%)** | **1/8 (12.5%)** |
| Unsupported questions safely kept below `complete` | 5/12 | 4/4 |
| Unsupported `complete` claims under this evidence check | **17** | **0** |

The planetary path marked **none** of its automatic responses complete, including five with the correct requested fact values. This is conservative abstention, not good general-purpose question answering.

On EPL, 26 responses claimed completion; only 9 contained all requested structured facts or required calculation inputs: **34.6% precision among completion claims**. The 17 unsupported completion claims comprise four mixed-clause mistakes, four home/away mistakes, two league-ranking mistakes, and seven questions requesting absent competitions or finer-grained data. These are evidence/interpretation failures; no answer-generation model was evaluated, so they are not 17 measured wrong generated answers.

Some responses contain a useful prose line even when the selected structured metric is wrong. We report document retrieval, visible excerpts, and requested structured facts separately in the raw traces. A downstream agent that reinterprets the question or reads complete documents might recover more answers. This evaluation measures the automatic endpoint as delivered, without those extra steps.

## Where it fails

| Category | Answerable cases | Complete requested facts/inputs | Unsupported completion claims |
| --- | ---: | ---: | ---: |
| EPL simple controls | 4 | 4 | 0 |
| EPL unfamiliar wording/nickname/typo | 6 | 3 | 0 |
| EPL mixed clauses and measures | 6 | 2 | 4 |
| EPL multiple match dates | 4 | 0 | 0 |
| EPL implicit opponent followed by another fact | 4 | 0 | 0 |
| EPL home/away statistics | 4 | 0 | 4 |
| EPL comparisons against all clubs | 4 | 0 | 2 |
| EPL “did not lose” | 4 | 0 | 0 |
| Planetary simple controls | 4 | 4 | 0 |
| Planetary comparisons | 4 | 0 | 0 |
| Planetary unfamiliar wording/typo | 3 | 1 | 0 |
| Planetary relation/reference frame | 1 | 0 | 0 |

Concrete examples from saved responses:

| Question | Required evidence | Actual behavior |
| --- | --- | --- |
| Bournemouth home wins, 2016/17 | 9 home wins, derived from home fixtures | Returns 12 total wins and `complete: true`. |
| Watford away goals, 2017/18 | 17 away goals | Returns 44 total goals and `complete: true`. |
| Fulham wins in 2020/21; Huddersfield goals in 2018/19 | Fulham 5 wins; Huddersfield 22 goals | Returns Fulham 27 goals in 2020/21 and Fulham 7 wins in 2018/19, marked complete. |
| Chelsea FA Cup goals, 2020/21 | Not present in this EPL-only corpus | Returns Chelsea's 58 league goals and `complete: true`. |
| Venus rotation period **and** length of day | Two distinct measures with different definitions | Returns rotation period only; safely remains incomplete. |
| Moon orbital period around Earth | Moon's sidereal orbital period, not a conjunction of two subjects | Treats Moon and Earth as entities that must both occur in one concept, returning no evidence. |

The planetary distinctions follow the NASA source definitions: solar day and sidereal rotation differ, negative rotation denotes retrograde motion, and the Moon's tabulated orbital period refers to Earth. These definitions were retained when converting the source; the misses occur during retrieval interpretation.

## Explicit interpretation control

For eight planetary questions with explicit named subjects and measures, we also supplied manually interpreted plans containing entity names, metrics and `kind: PlanetFact`. Plans contained no target document IDs or expected values. **All 8/8 returned the correct requested facts and bases**, compared with 5/8 for the same questions automatically.

This supports a narrower conclusion: typed metadata lookup works on this small corpus when interpretation is supplied. It does not prove a particular LLM would construct correct plans, or that inference quality would be 100%. The plans were authored from the evaluation needs and are an assisted diagnostic. They are excluded from automatic scores. No model cost or accuracy is inferred from them.

Metadata fact values and their source pointers can be sufficient evidence without a prose quote. The current generic excerpt selector is weak: these explicit-plan controls returned correct structured values but none included every expected source-derived prose line. That is separately recorded, rather than counted as a wrong metadata value.

## Timing and resource observations

The 44 non-control EPL questions were warmed, then repeated three times at each concurrency: **132 requests per block, 396 measured requests total**, all returning HTTP 200. These timing measurements include failures and abstentions, so speed is not a measure of successful answering.

| Concurrent clients | P50 | P95 | Queries/s |
| ---: | ---: | ---: | ---: |
| 1 | 6.18 ms | 18.07 ms | 135 |
| 4 | 32.49 ms | 58.96 ms | 116 |
| 16 | 146.18 ms | 963.44 ms | 50 |

These are short loopback bursts using new Python urllib connections. They include service execution, queueing and transport; the long 16-client tail has not been isolated by profiling. The initial quality pass also observed an **849 ms** request for “Count Blackburn Rovers losses in 2011/12,” including **848 ms reported inside the query engine**, before the timed warm blocks. Warm averages conceal such first-use costs.

Service CPU time across timed blocks was **3.04 seconds**. RSS endpoint samples ranged from **13.203 to 13.328 MiB**; these before/after samples are not peak-memory measurements. The temporary planetary service used **11.406 MiB RSS** after quality checks. OS cache, clients and agents are excluded. Candidate-result caps do not establish a bound on SQL scan work or total latency.

## What to improve next

1. **Preserve the entire question scope.** Competition, home/away, player versus team, expected versus actual goals, and time slices need explicit fields. An unsupported condition must produce an unresolved constraint rather than disappear.
2. **Verify interpretation before claiming completion.** Every requested subject/period/measure must remain bound to its own clause. Completion must cover all original conditions, not merely whatever the planner retained.
3. **Generalize decomposition beyond season patterns.** Multiple dates, implicit opponents, relations such as “orbits Earth,” and two-entity comparisons require separate, correctly scoped retrieval steps.
4. **Compute derived statistics from sufficient inputs.** Home/away splits, rankings and negation should use exact match/season data and explicit operations. A single club total cannot support a league-wide comparison.
5. **Evaluate a caller-agent or small-model interpreter separately.** The explicit-plan control justifies testing interpretation assistance while keeping storage in Rust/SQLite. Measure its accuracy and usage on a fresh frozen set before making model or cost claims.
6. **Improve generic excerpts and profile slow fallbacks.** Return the section corresponding to each requested fact, and distinguish planning, SQL and queueing time. More football synonyms alone will not resolve these failures.

Until those checks are implemented, inspect `plan`, source scope, selected metrics and qualifiers even when `complete` is true.

## Reproduction and limits

```sh
# Fetch the primary HTML and notes into /tmp/jbrain-hard-eval/ first, or pass paths:
python3 scripts/hard_eval.py --prepare-only \
  --nasa-html /tmp/jbrain-hard-eval/planet-facts.html \
  --nasa-notes /tmp/jbrain-hard-eval/planet-notes.txt
python3 tests/hard_eval_test.py
python3 scripts/hard_eval.py
```

The EPL service recorded in `.jbrain/epl-trial/session.json` must be running the frozen binary. The script validates the fixture, source and binary hashes before reruns. It creates a separate temporary planetary service and removes it afterward. It never supplies plans to the automatic path or modifies the EPL documents.

Questions: [hard-retrieval-v1.json](../tests/fixtures/hard-retrieval-v1.json). Portable results: [hard-retrieval-v1-summary.json](evals/hard-retrieval-v1-summary.json). Full responses, source snapshots, gold inputs and frozen manifest are retained under `.jbrain/hard-eval-v1/`. Binary SHA-256: `69f760177fd7e80bd8d469cf8a3df38e2a61db19b66389fe4ddf937cdc953ebd`.

These questions were deliberately authored with knowledge of the implementation to probe its limits. This is an adversarial diagnostic, not a blind independent sample or an estimate of production accuracy. The new domain is only 11 documents; it checks transfer of interpretation, not large-corpus scale. Home/away and ranking questions are answerable from stored data but require calculation; the evaluation accepts complete calculation inputs, without requiring a generated final answer. Some failures need an additional read or a larger comparator set than fits the five-result response.
