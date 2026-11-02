# Automatic evidence retrieval

The [difficult evaluation](HARD_RETRIEVAL_EVAL.md) found that qualifiers and clause bindings could be lost while the old `complete` flag remained true. The contract is now corrected: `complete` is always false here; `plan_satisfied` carries the old plan-matching signal. Use the new [accuracy workflow](ACCURACY_WORKFLOW.md) for a model-generated, separately checked answer.

JBrain now interprets common questions in Rust and returns bounded evidence through `POST /v1/query`, `jbrain query`, and MCP `brain_query`. No embedding model, model server, paid API, or model call is involved. Literal `search` and full `links` retain their previous behavior for compatibility and baseline comparisons.

## Usage

```sh
./target/release/jbrain query "Compare Liverpool goals scored in 2018/19 and 2019/20."
./target/release/jbrain browse --prefix epl/club-seasons/ --period 2019/20 --limit 10
./target/release/jbrain neighbors epl/seasons/2019-20 --kind ClubSeasonSummary --entity Liverpool --limit 4
```

```sh
curl http://127.0.0.1:7331/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"How many points did Liverpool earn in 2019/20?","limit":5,"max_chars":16000}'
```

Inspect `plan`, `coverage`, `warnings`, and source qualifications before composing an answer. Results carry the concept ID, revision, source metadata, requested fact values, metadata pointers or extracted quotes, and an excerpt. This is evidence retrieval; it does not generate a final answer.

## How it works

1. Resolve canonical entities and aliases from producer metadata. A bounded spelling correction searches alias prefixes/length ranges with Damerau distance; it does not embed or scan document bodies. Ambiguous matches are surfaced.
2. Normalize common measures, English football paraphrases, adjacent-year seasons, and ISO or day/month-name/year dates. Split repeated seasons and entity comparisons into separate clauses.
3. Search structured metadata for each clause independently. Entity, date, period, kind and fact-basis constraints are SQL parameters. Reserve a result for each clause before filling remaining slots. This prevents one season's many neighbors from displacing another season's evidence.
4. If structured facts are absent, retrieve scoped BM25 candidates with normalized terms. Expand at most two seeds per clause, four outgoing neighbors per seed, one hop. All clause scope filters apply before the SQL limit. Ordinary Markdown links still have no machine-declared relation type; labels and enclosing headings are retained for inspection through `neighbors`.
5. Distinguish declared fact support from plausible candidates. Conflicting fact values, truncated fact pools, draft/deprecated evidence, and expired RFC3339 `stale_after` values prevent automatic completion. Negation, relative freshness and unhandled before/after comparisons require verification or an explicit plan.

`status` is `partial`, `needs_verification`, or `unsupported`. A clause's support is `structured_fact`, `candidate_only`, or `missing`. **`plan_satisfied` means every interpreted clause has matching declared facts; `complete` and `answer_supported` remain false.** A wrong or incomplete interpretation can still miss the user's intent. Read the plan, especially for unfamiliar domains and qualifiers. Contradictions outside the bounded pool are not exhaustively audited. Historical reports describe the old response contract; this correction alone is not an accuracy improvement.

A generic prose document can be retrieved without structured facts, but it remains `candidate_only`; an agent must inspect it. Related candidates on an unsupported question do not imply an answer exists.

## Optional producer metadata

These are JBrain extensions inside permissive OKF frontmatter, not fields required by the OKF specification. Existing OKF bytes and unknown metadata are preserved.

```yaml
---
type: Reference
title: Atlas throughput measurement
entity_id: atlas
entity_name: Atlas
aliases: [AX]
document_kind: Measurement
period: 2026-Q3
facts:
  requests_per_second:
    value: 120
    basis: single_client
sources:
  - resource: https://example.com/measurement
---
A measurement with its methodology and qualifications.
```

`entity` or `name` can supply the entity name; `entities: [canonical-name, ...]` adds multiple entities. Keep IDs/names consistent between documents. `period`, `date`, `basis`, `document_kind`, `status`, and `stale_after` are optional. Generic fact values come only from declared `facts`/`computed_statistics`, never model-generated guesses. Fact objects may declare `value` and `basis`; scalar values inherit the document basis. A metadata-only fact has an empty quote and an explicit metadata pointer, rather than an invented prose quotation.

The legacy EPL importer supplies `club`, `season`, `computed_statistics`, `match_date`, `home_team`, and `away_team`. A derived adapter indexes those fields and the importer's exact match-statistic labels. It infers document kinds and preserves the distinction between earned points **before administrative deductions** and official final points. This adapter is deliberately domain-specific; it does not imply arbitrary prose can be converted reliably into facts.

Automatic English interpretation covers the implemented vocabulary. Non-season periods such as `2026-Q3`, unfamiliar measures, temporal ranges, arbitrary relations, and complex questions (including comparisons across multiple match dates) should use a caller-supplied plan or ordinary search/browse. No server model fallback is configured.

## Caller-supplied interpretation

An agent can use its own reasoning or model and supply a structured plan. JBrain validates and executes it deterministically. The agent is responsible for preserving every condition in the original question; the plan replaces automatic interpretation.

```json
{
  "question": "Find Atlas throughput for Q3 2026",
  "plan": {
    "clauses": [{
      "entities": ["Atlas"],
      "period": "2026-Q3",
      "kind": "Measurement",
      "metrics": ["requests_per_second"],
      "basis": "single_client"
    }]
  }
}
```

Use `jbrain query "..." --plan plan.json` where the file contains the `{"clauses":[...]}` plan object. Optional clause fields are `text`, `entities`, `period`, `date`, `metrics`, `kind`, `basis`, and `terms`; unknown fields are rejected. Filters are exact, producer-defined values. Dates must be valid `YYYY-MM-DD` strings. Quotes and SQL syntax in terms cannot inject SQL or FTS expressions.

## Bounds and concurrent access

Questions are limited to 4096 bytes/96 words, plans to four clauses, clause entities/measures to four each, and clause terms to 32. At most 20 candidates are retained per clause; `limit` is 1–16 and must reserve one slot per clause. Alias correction performs at most 32 bounded vocabulary lookups, at most 64 alias/entity rows each; very large or ambiguous alias vocabularies can reduce recall. Exact entity mentions are capped at eight. Query HTTP bodies are limited to 16 KiB.

`max_chars` (2048–64000) limits **combined excerpt characters**, with at most 4000 per document. It is not a token count or a limit on the whole JSON response: structured facts, citations, plan and metadata add bytes. Fact values are indexed up to 1024 serialized bytes; facts per producer object are capped at 64. Source entries are limited to eight per result. Use smaller limits and read a precise concept separately for full context.

`GET /v1/browse` takes `prefix`, `kind`, `period`, `limit` (1–100), and `offset`, returning metadata and `next_offset`. It synthesizes an index from current concepts; it does not rewrite OKF `index.md` files. `GET /v1/neighbors` takes `id`, `direction` (`outgoing`/`incoming`), `kind`, `period`, `entity`, and `limit` (1–64). It returns resolved links, labels, headings and `has_more`. Unresolved links remain visible through the original `/v1/links` API.

The derived metadata, facts, aliases and link context publish in the **same SQLite transaction** as each document's revision and FTS index. A query holds the existing shared index lock while assembling evidence, so its citations/facts come from one published index state. Writers still perform expensive file durability work outside that lock; multiple agents can request reads/writes concurrently, with short index access serialized. There are no model calls inside a database transaction. Multi-document writes remain separate operations, not one atomic batch.

Schema version 4 replaces only the derived index and rebuilds it from canonical files on startup. It preserves files, the operation journal, request receipts and revisions. Start one service per brain; agents connect to it instead of opening SQLite or PGLite themselves.
