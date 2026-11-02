# New difficult question challenge

The local service is ready with 13,989 unchanged documents. The new answer-model endpoint still returns `503 MODEL_NOT_CONFIGURED`. The prior 78 automated checks remain passing; this run changes evaluation code and documentation only, not the production implementation.

This challenge contains **24 new development questions**: 16 answerable, six unsupported by this corpus and two requiring clarification. Questions and CSV-derived gold were saved before retrieval. The questions were authored with implementation knowledge; this is not an independent holdout. Frozen CSV hashes were verified, and canonical document hashes remain unchanged.

## Observations

- Automatic `/v1/query` supplied the complete required evidence on **0/16 answerable questions** under this challenge's strict sufficient-input checks. Several retrieved partial facts or the right documents. It returned 16 of 20 club facts for the draw-ranking case, one of three requested match facts, and both Newcastle fixture counts while omitting league sizes. This is a retrieval result, not a real-model final-answer accuracy score.
- Caller-authored tool plans plus explicit Python computations recovered **16/16 source-backed answers**. This includes ranking/grouping, date filtering, comparisons and arithmetic in the client. It is not 16/16 autonomous answers from the deployed `/ask` workflow.
- Eleven answerable questions had `plan_satisfied: true` despite missing requested conditions. All `/query` results retained `complete: false`, as required by the corrected contract. The flag correction is not counted as an accuracy improvement.
- No provider model was called by the brain. `/ask` returned `503 MODEL_NOT_CONFIGURED`. Unsupported/ambiguous cases therefore receive no credit for correct model refusal or clarification merely because `/query` completion is false.

The automatic queries used generous bounds: 16 returned documents and 64,000 excerpt characters. One sequential pass measured P50 **19.0 ms** and P95 **145.0 ms**, including HTTP. Scripted evidence workflows had median **12.9 ms**, maximum **211.4 ms**. These are different workflows with different request counts; neither includes remote inference, and these samples are not throughput or latency guarantees.

## Source-backed answers from the new questions

- Most away league goals in 2022/23: **Arsenal and Brighton tied at 35** each.
- Manchester United's away league wins during calendar year 2020: **11**. This crosses two football seasons.
- Arsenal wins after a level half-time score in 2023/24: **10**.
- Newcastle away matches without defeat in 2022/23: **16**, with **+19** goal difference specifically in those matches. Automatic retrieval returned the whole-season goal difference of +35 and five losses instead.
- Liverpool goals conceded in away defeats across 2021/22 and 2022/23 combined: **24**.
- Brighton versus Aston Villa home wins in 2022/23: **10 versus 12**, Villa ahead by two.
- Tottenham–Manchester City, 5 February 2023: **1–0 at half time, 1–0 at full time; shots on target 3–5**. Automatic retrieval supplied only shots on target.
- Brentford's opponent on 13 August 2021: **Arsenal**, who scored **26 away league goals** that season.
- Nottingham Forest versus Everton away defeats in 2022/23: **13 versus eight**.
- Chelsea draws in 2023/24: **four at home, five away**.
- Burnley away non-wins in 2014/15: **16**.
- Southampton away wins in 2024/25: **one**. Automatic retrieval returned the two wins across all venues.
- Highest draw total in 2016/17: **Manchester United, 15**, without a tie.
- Newcastle fixtures and league sizes: **42 fixtures / 22 clubs in 1993/94**, versus **38 / 20 in 2024/25**.
- Leicester away draws in 2015/16 versus Chelsea in 2014/15: **six versus five**.
- Manchester City points in 2020/21: **41 home, 45 away**. The question's suggestion of fewer away points is false: they earned four more away. These are points derived from wins and draws.

## Newly demonstrated gaps

1. **Grouped aggregate tool:** the straightforward correct plan for the away-goals leaderboard used 21 tool calls (discover clubs, then compute each club). It exceeds the deployed model workflow's 16-provider-call budget once planning/finalization/verification are included. This does not prove every possible strategy fails, but the obvious reliable strategy does not fit. One grouped computation would avoid the loop.
2. **Date ranges:** adding `date_start` to the native scope returns HTTP 400. The calendar-year answer was recovered by fetching receipts for two seasons and filtering dates in Python. That calculation is outside the native computation API.
3. **Half-time predicates:** adding `half_time_result` returns HTTP 400. The demonstrated plan requires one win computation plus 28 match reads, then a Python count: 29 tool calls, again over the sequential model budget. The native match receipt omits half-time results.
4. **Composed computations:** differences, totals across seasons and correction of a false numerical premise currently require client arithmetic. The separate model verifier has not been tested live on these tasks.
5. **Automatic scope preservation:** the old parser still drops away/home, exclusion, several metrics, the comparison universe and competition. One unsupported FA Cup question selected Arsenal's 26 Premier League wins with `plan_satisfied: true`; the evidence was explicitly not accepted as an answer.

The six unsupported questions concern official Everton points after appeals/deductions, an individual Liverpool scorer/minute, Brighton xG, Arsenal FA Cup wins, a Newcastle European-match attendance, and Chelsea's 1992/93 results before this snapshot. The two ambiguous questions use unspecified “United,” “City” and “that season.” None is answered from related team totals.

## Reproduce and inspect

```sh
python3 scripts/challenge_eval.py --output /tmp/jbrain-challenge-fresh-run
```

The output directory must not already contain a frozen fixture. Runtime requests and responses, gold values, scoring checks and call counts are saved in `.jbrain/challenge-v3/results.json`; the fixture is `.jbrain/challenge-v3/frozen-cases.json`. Gold fixture SHA-256: `043ba5d2b5a734764e27786d8d877d666872a7f369279c8ae721b67ca24e3102`. Running release SHA-256: `f7404c24899dac57934a9ae2f51ddd228cf0fe7a1b6c751d537d42868b34e758`.

The 97% release gate remains **unproven**. The next engineering work should add grouped calculations, date/half-time predicates and composed arithmetic before a real-model development comparison and the independent holdout. Increasing model-call limits alone would raise cost while leaving these structural gaps.
