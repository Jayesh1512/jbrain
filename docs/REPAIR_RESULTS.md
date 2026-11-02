# Targeted accuracy repairs after the live baseline

The [first real-model baseline](LIVE_BASELINE_RESULTS.md) remains frozen at **11/16 correct answerable questions** and **18/24 overall** under a provisional implementation-author source review. The repairs below are separate development experiments. Their successful cases cannot be combined into a new 24-question accuracy score. A full, independently reviewed holdout remains necessary before claiming >97%.

The repair experiments below used release binary `dc5f2a0b1be31b3e809d9917ed23455354b77feced91a20d885fff82dfe9d2e0` and `gpt-6-astra` for both generation and separate verification. They are historical measurements; the EPL trial daemon was subsequently switched to low-cost `gpt-6-luna` for both roles. The 13,989-document corpus is unchanged (`ebbac42a3b13fcd4198636606de6a9e33de056a8cf487a9f4e2823ac86a4c602`). The original baseline binary and each intermediate binary are preserved under `.jbrain/` with their per-run manifests.

| Former baseline miss | Repair and isolated real-model observation | Status |
|---|---|---|
| Calendar-year away wins | Inclusive date bounds across complete 2019/20 and 2020/21 receipts; model answered **11** (5 + 6) with valid citations in `repair-v3`. | Demonstrated on one attempt |
| Wins from a level half-time score | Native match filter reads stored half-time facts; model answered **Arsenal 10** in `repair-v1`. | Demonstrated on one attempt |
| League-wide most away goals | Complete 380-match rank with all tied leaders; model answered **Arsenal and Brighton, 35 each** in `repair-v4`. | Demonstrated on one attempt |
| Most league draws, including ties | Same complete rank; model answered **Manchester United alone, 15** in `repair-v4`. | Demonstrated on one attempt |
| Missing player scorer/minute | A verified abstention now keeps its user-facing explanation; model correctly explained the missing goal-event data in `repair-v2`. | Demonstrated on one attempt |
| Brighton versus Villa home wins | Prompt now resolves a unique covered league and canonical club from the EPL catalog. Every attempted real-model retest ended with a provider I/O failure before a final answer. | **Unmeasured** |

The Newcastle away unbeaten and goal-difference canary remained correct in `repair-v1` (16 matches, +19). These known development questions were inspected during implementation; they are unsuitable as an independent holdout.

## Deterministic evidence checks

The final binary’s native evidence API recomputed all four repaired numeric reference results from the unchanged EPL corpus: Manchester United’s calendar-2020 away wins **5 + 6 = 11**; Arsenal’s level-at-half-time wins **10** from all 38 matches; away-goal leaders Arsenal and Brighton **35 each** from all 380 season matches; and Manchester United’s sole draw lead **15** from all 380 season matches. Both full league ranks took under 0.1 seconds in direct local calls before the compact model view was added. The native tool retains full revision-bound inputs for audit.

The first two accepted rank answers sent the full 380-match receipts into repeated model context and cost about **$1.39** and **$0.97** respectively at the [checked Astra rates](https://developers.openai.com/api/docs/models/gpt-6-astra). The current model workflow instead sends the ranking, input count and SHA-256 of the full input receipt, while retaining revisions internally for freshness checks. A simulated-provider integration test confirms the digest matches the complete native receipt and that raw match inputs are absent from the model payload. One live compact-rank call produced a **1.3 KB** model receipt versus **82 KB** from the full native tool; later provider I/O failure prevented an accepted answer and a full-workflow cost comparison. Its first four metered calls cost about **$0.07**, which must not be compared to a completed answer price.

## Provider reliability limit

During targeted runs the model sometimes returned `MODEL_PROVIDER` with a transport I/O failure. Increasing the per-request bound from 60 to 90 seconds and the workflow bound from 180 to 240 seconds did not eliminate these failures. Disabling idle connection reuse did not eliminate them either, so normal connection pooling was restored. A separate Python HTTPS client sent the same structured planner request three times: two completed in **20.35** and **20.88** seconds, while one timed out after **100.09** seconds. This is evidence of intermittent request latency or transport failure beyond one Rust connection-pool behavior, not a diagnosis of the provider’s internal cause. Failed calls without usage receipts may still be billed.

Because the latest three-case live run ended in provider I/O failures, it cannot measure the compact ranking answer quality or the home-comparison prompt. I did not fold those failures away or claim that the repaired binary passes the >97% gate. A complete stable-provider development rerun, a measured cheaper-model comparison, and an independent multi-domain holdout are still needed.

## Validation and artifacts

- All 84 relevant automated tests passed: 3 Rust unit tests, 17 simulated-provider/evidence tests, 20 native service tests, 10 query tests and 34 evaluation/import tests. They cover citation repair, truthful abstention, exact date/half-time subsets, complete tied rankings, and compact receipt hashing.
- The final binary and canonical corpus hashes were rechecked after the last daemon restart. The service health endpoint reports 13,989 concepts ready. Direct native evidence checks still return the four reference values above.
- `.jbrain/repair-v1/` through `.jbrain/repair-v5/` retain question-only inputs, separate attempted responses, binary hashes and transport failures. `.jbrain/repair-v4/rank-tool-check.json` contains the two full 380-match receipts; `.jbrain/repair-v5/python-planner-diagnostic.json` contains the external-client timing and usage metadata. No API key is stored in these artifacts.
