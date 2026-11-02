# JBrain agent protocol

Use HTTP, direct Streamable HTTP MCP at `/mcp`, or the native `jbrain` CLI to connect to the shared service. Start the storage service once outside the agents. Direct HTTP/MCP shares one native service across all agents. Clients that require stdio can run `jbrain mcp` as a native proxy. No agent owns the storage.

## Read

Use `brain_ask` for the daemon's configured model workflow and independently checked answer, or `brain_evidence` for typed catalog/find/read/compute/link tools. Only an `/ask` result with `status: answered` and `complete: true` is accepted by that workflow; this still needs empirical accuracy validation. On `retryable`, restart the read workflow because evidence changed. On `429 ASK_BUSY`, back off. Reads/writes remain available while inference waits. See [configuration, limits and the release gate](ACCURACY_WORKFLOW.md).

`brain_query` remains a model-free evidence endpoint; `complete` is always false, and `plan_satisfied` only describes interpreted clauses. Use `brain_browse` for filtered metadata and `brain_neighbors` for bounded links. Inspect every clause and coverage: `candidate_only` is not support. Fact bases and source qualifications are part of the evidence. Supplying your own plan does not establish that the original question was understood. See [the query contract](QUERY_RETRIEVAL.md).

Browse `brain_list` or search `brain_search` (lexical BM25: use 2-5 distinctive keywords, one fact per search; common question words are ignored, and when fewer than three notes match every word, notes matching any word are appended after them, so weigh the score and snippet), read exact Markdown and its revision using `brain_read`, and follow `brain_links` as needed. Separate reads may observe different revisions if other agents commit between them. Preserve source metadata and unknown frontmatter fields when editing.

## Create

Prepare OKF Markdown with a `type` field. Choose a lowercase concept ID without `.md`. Persist a unique request ID with the payload before sending:

```json
{
  "requestId": "<unique-id-for-this-write>",
  "actor": "<agent-name/version>",
  "action": "put",
  "id": "project/concept",
  "expectedRevision": null,
  "markdown": "---\ntype: Reference\n---\nKnowledge.\n"
}
```

`null` means the concept must not exist. Simultaneous creation produces one winner; other agents receive a conflict.

## Update or delete

1. Read the current concept and revision.
2. Perform analysis, extraction and merging outside storage transactions.
3. Submit the complete Markdown with that `expectedRevision`, `action: "put"`, your actor and a new request ID.
4. For deletion, submit `action: "delete"` and the revision; omit `markdown`.
5. Wait for success before treating the write as committed. Another agent may commit an update immediately afterward.

On `409 REVISION_CONFLICT`, read the latest state, reconcile your intended change, then send a new write with a **new request ID** and latest revision. Do not blindly replace the revision and resend old content: this can erase another agent's contribution. A rejected request stays rejected under its original ID.

## Uncertain results

After a network error, timeout, `503` or disconnect, keep the original ID/payload. Query `brain_operation` or `GET /v1/operations/:requestId`:

- `applied`: use the stored receipt.
- `rejected`: inspect the stored conflict/error and revise the intended write if needed.
- `queued` or `prepared`: accepted work is pending processing or recovery.
- `404`: resend the original write with the same ID/payload.

Resending the exact request directly is also safe. Identical concurrent deliveries share one operation. Changing the actor, action, content or revision while keeping the ID returns `REQUEST_ID_REUSED`.

For `QUEUE_FULL`, back off and retry the same ID/payload. For `RECOVERY_REQUIRED`, storage needs operator recovery/restart; the accepted request is retained.

## Ownership

Never open the SQLite files, initialize PGLite, directly edit knowledge files, or run Git commands against the live bundle from an agent. Use the service for all changes. Remote agents connect to its URL with authentication; they do not mount the database directory.

Atomicity is per concept. Multi-concept changes are separate operations; plan for partial completion and retry by request ID.
