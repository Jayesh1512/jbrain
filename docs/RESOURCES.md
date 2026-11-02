# Resource usage

JBrain runs as a native Rust binary with bundled SQLite. A single Tokio event-loop thread serves HTTP and stateless Streamable HTTP MCP, and a writer thread sleeps until work arrives. Agents connect to the existing process. The optional `jbrain mcp` stdio proxy is another native process; direct HTTP/MCP uses less total memory.

The [accuracy workflow measurements](ACCURACY_RESULTS.md) are the latest local samples: 6.909 MiB release executable, 8.92 MiB empty RSS and 11.70 MiB after a 100-document workload, with 0.00 seconds idle CPU delta at reporting precision. They exclude real model calls. Optional `/ask` inference uses a remote provider and at most two blocking workers; no local model is loaded. Contextual passages and metadata indexes increase disk use. See [limits and configuration](ACCURACY_WORKFLOW.md).

## What keeps it small

- No embedding computation, vector database, local model, JavaScript runtime, UI server or periodic polling.
- One owner and one final writer for the entire brain. Agents can read and submit writes concurrently; conflicting revisions are rejected instead of silently replacing an agent's changes.
- Concept file reads are bounded to 1 MB, including offline imports, so an oversized file cannot be loaded in full before validation.
- Parsed concepts, search data and link relationships stay in SQLite on disk. Reads deserialize stored snapshots instead of reparsing Markdown/YAML. Startup walks one file at a time and reparses changed files only.
- SQLite page-cache targets are 256 KiB per database, memory mapping is disabled, and temporary tables spill to disk. These settings do not cap the entire process's RAM.
- Journal payloads stay on disk. The worker queue carries request IDs, with at most 128 outstanding deliveries, including duplicates. Permits remain with the writer even when a client disconnects, so disconnected deliveries cannot grow an unbounded work queue.
- Pending logical writes default to 64 entries and 8 MiB of serialized input. Excess admission receives `503 QUEUE_FULL`; accepted writes remain durable. Tune `JBRAIN_MAX_PENDING` and `JBRAIN_MAX_PENDING_BYTES` for your workload.
- Search entries use stable integer row IDs for replacement and joins, avoiding a full search-index scan on every write.
- Git commits are opt-in (`JBRAIN_HISTORY=git`). The default uses the durable SQLite journal without Git subprocesses. Receipts have `gitCommit: null` in that mode.

Full SQLite synchronization and file/directory fsync remain enabled. Lower resource usage does not remove the journal, revision checks, idempotency receipts or startup recovery.

## Measurements

The following prototype measurements predate automatic query retrieval. For the new endpoint on 13,989 EPL documents, warm query blocks sampled up to **13.59 MiB RSS**, P50/P95 **7.28/17.12 ms** at one client, and **75.00/105.29 ms** at 16 clients. These retrieval workflows differ from the older full-document workloads. Full corpus readback subsequently sampled 15.625 MiB RSS. Derived metadata indexes add disk storage; no new runtime dependency or model process was added. See [the protocol and measurements](QUERY_RESULTS.md).

Measured on an Apple M2 Mac (macOS 26.3.1), release binary about **6.4 MiB**. The 100-document workload gave:

| Implementation | Idle RSS | RSS after workload | 100 writes | 200 reads |
| --- | ---: | ---: | ---: | ---: |
| Earlier Bun prototype, Git | 42.38 MiB | 70.69 MiB | 9.877 s | 0.147 s |
| Rust, Git | 8.75 MiB | 11.44 MiB | 6.415 s | 0.039 s |
| Rust, journal only (default) | 8.56 MiB | 11.31 MiB | 0.777 s | 0.062 s |

The default Rust service used about **84% less RSS after this workload** than the earlier Bun prototype. In a separate **1,000-document** run, RSS after the workload was **11.81 MiB**, writing took 8.271 s, and stored file lengths totaled 24.48 MiB. The 100-document default used 3.49 MiB of stored file lengths. Rust's five-second idle samples showed no CPU-time change at `ps` reporting precision.

See `benchmarks/` for the recorded JSON samples and environment. These are local samples, not capacity promises or minimum system requirements. RSS includes resident process memory; OS filesystem cache, agent/LLM processes and benchmark-client memory are outside this measurement. Active connections, large documents, YAML metadata, searches and request concurrency can raise memory above these samples. There is no hard process-memory cap.

Each sample writes roughly 4.3 KiB OKF documents using four concurrent clients, reads 200 concepts, then observes five seconds idle. The service's RSS and cumulative CPU time are read from `ps`. A reported zero idle CPU delta means no change at its 0.01-second reporting precision; it does not prove literally zero CPU use. CPU totals exclude Git subprocesses. Storage is the sum of file lengths while running, including journal, derived index and WAL files, rather than allocated disk blocks.

The earlier Bun prototype always recorded Git history. Compare Rust Git mode with that baseline to separate runtime/index improvements from turning Git off. The lean default saves additional write work by omitting Git. These measurements describe JBrain's implementations; they are not benchmarks of GBrain or Slashdocs.

## Repeat the native benchmark

```sh
cargo build --release --locked
python3 scripts/benchmark.py --label rust-lean --history off -- ./target/release/jbrain serve
python3 scripts/benchmark.py --label rust-git --history git -- ./target/release/jbrain serve
python3 scripts/benchmark.py --label rust-1000 --documents 1000 --history off -- ./target/release/jbrain serve
```

The benchmark uses Python's standard library, a temporary data directory and an ephemeral port. It cleans up its server and data. Run it without other builds or tests for more stable timing. Cold executable startup and filesystem conditions can change wall-clock results substantially.

## Storage and limits

The release binary is a fixed installation cost. Cargo's `target/` directory is build output, not a runtime dependency; distribute just the binary. The brain stores canonical Markdown, parsed snapshots and search indexes, plus the complete operation journal. Optional Git adds another history representation. Storage therefore grows with both current data and edit history. Deletion removes current retrieval content but does not erase historical journal payloads or old Git commits. Automatic history/receipt pruning is not implemented.

The default needs one local filesystem host and serializes final writes. Downloads, extraction, inference and merge reasoning run in agents before submitting knowledge. More agents share the service but create more active request work. If sustained write throughput outgrows one host, a server database or distributed design is a different resource tradeoff.
