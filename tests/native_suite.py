#!/usr/bin/env python3
"""Native integration tests: Python stdlib is the only test runner dependency."""
import concurrent.futures as futures
import contextlib
import hashlib
import json
import os
import pathlib
import selectors
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
BINARY = os.environ.get("JBRAIN_TEST_BINARY", str(ROOT / "target/debug/jbrain"))


def md(body="knowledge", extra=""):
    return f"---\ntype: Note\n{extra}---\n{body}\n"


def write_input(concept, content=None, **changes):
    return {"requestId": str(uuid.uuid4()), "actor": "test/1", "action": "put", "id": concept,
            "expectedRevision": None, "markdown": md(concept) if content is None else content, **changes}


def http(url, path, payload=None, headers=None, raw=None):
    data = raw if raw is not None else None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        return response.status, json.loads(body) if body else None


class Service:
    def __init__(self, directory=None, **variables):
        self.temporary = tempfile.TemporaryDirectory(prefix="jbrain-native-") if directory is None else None
        self.directory = pathlib.Path(directory or self.temporary.name)
        env = {**os.environ, "JBRAIN_DATA_DIR": str(self.directory), "JBRAIN_PORT": "0", "JBRAIN_HOST": "127.0.0.1",
               "JBRAIN_TOKEN": "", "JBRAIN_HISTORY": "off", **variables}
        self.proc = subprocess.Popen([BINARY, "serve"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready = self.event("ready")
        self.url = ready["url"]

    def event(self, name):
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            if not selector.select(30):
                raise AssertionError(f"Timed out waiting for {name}")
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(self.proc.stderr.read())
            event = json.loads(line)
            assert event["event"] == name, event
            return event

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.kill()
            self.proc.communicate()
        if self.temporary:
            self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def put(self, concept, content=None, **changes):
        status, value = http(self.url, "/v1/operations", write_input(concept, content, **changes))
        assert status == 200, (status, value)
        return value

    def get(self, concept):
        return http(self.url, "/v1/concept?" + urllib.parse.urlencode({"id": concept}))


def probe_agent(url, agent):
    for index in range(4):
        concept = f"agents/{agent}/note-{index}"
        status, receipt = http(url, "/v1/operations", write_input(concept, md("sharedword"), actor=f"{agent}/1"))
        assert status == 200, (status, receipt)
        status, read = http(url, "/v1/concept?" + urllib.parse.urlencode({"id": concept}))
        assert status == 200 and read["revision"] == receipt["revision"]
        assert http(url, "/v1/search?q=sharedword")[0] == 200


class NativeTests(unittest.TestCase):
    def test_eight_agent_processes(self):
        with Service() as service:
            agents = [subprocess.Popen([sys.executable, __file__, "--agent", service.url, f"agent-{i}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(8)]
            try:
                for agent in agents:
                    output, error = agent.communicate(timeout=30)
                    self.assertEqual(agent.returncode, 0, error.decode())
                self.assertEqual(http(service.url, "/health")[1]["concepts"], 32)
                self.assertEqual(len(http(service.url, "/v1/search?q=sharedword&limit=100")[1]["results"]), 32)
            finally:
                for agent in agents:
                    if agent.poll() is None:
                        agent.kill()
                    agent.wait()

    def test_same_revision_has_one_winner(self):
        with Service() as service:
            seed = service.put("shared")
            with futures.ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(lambda i: http(service.url, "/v1/operations", write_input("shared", md(f"winner-{i}"), expectedRevision=seed["revision"])), range(16)))
            self.assertEqual(sum(status == 200 for status, _ in results), 1)
            self.assertEqual(sum(status == 409 for status, _ in results), 15)
            for status, body in results:
                if status == 409:
                    self.assertEqual(body["error"]["code"], "REVISION_CONFLICT")

    def test_merge_retry_has_no_lost_updates(self):
        with Service() as service:
            service.put("counter", md("0"))
            def agent(_):
                for _ in range(3):
                    for attempt in range(100):
                        current = service.get("counter")[1]
                        status, result = http(service.url, "/v1/operations", write_input("counter", md(str(int(current["body"].strip()) + 1)), expectedRevision=current["revision"]))
                        if status == 200:
                            break
                        self.assertEqual(status, 409, result)
                    else:
                        self.fail("Agent could not commit")
            with futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(agent, range(8)))
            self.assertEqual(service.get("counter")[1]["body"].strip(), "24")

    def test_duplicate_delivery_and_request_id_reuse(self):
        with Service() as service:
            payload = write_input("once")
            with futures.ThreadPoolExecutor(max_workers=20) as pool:
                results = list(pool.map(lambda _: http(service.url, "/v1/operations", payload), range(20)))
            for result in results:
                self.assertEqual(result, results[0])
            status, error = http(service.url, "/v1/operations", {**payload, "markdown": md("different")})
            self.assertEqual((status, error["error"]["code"]), (409, "REQUEST_ID_REUSED"))

    def test_create_delete_preconditions(self):
        with Service() as service:
            with futures.ThreadPoolExecutor(max_workers=8) as pool:
                creates = list(pool.map(lambda _: http(service.url, "/v1/operations", write_input("new")), range(8)))
            self.assertEqual(sum(status == 200 for status, _ in creates), 1)
            revision = service.get("new")[1]["revision"]
            with futures.ThreadPoolExecutor(max_workers=8) as pool:
                deletes = list(pool.map(lambda _: http(service.url, "/v1/operations", {"requestId": str(uuid.uuid4()), "actor": "test/1", "action": "delete", "id": "new", "expectedRevision": revision}), range(8)))
            self.assertEqual(sum(status == 200 for status, _ in deletes), 1)
            self.assertEqual(service.get("new")[0], 404)

    def test_readers_during_write_and_backpressure(self):
        with Service(JBRAIN_TEST_PAUSE_PHASE="file", JBRAIN_MAX_PENDING="1") as service:
            seed = service.put("shared", md("oldword"))
            payload = write_input("shared", md("newword"), requestId="crash-request", expectedRevision=seed["revision"])
            with futures.ThreadPoolExecutor(max_workers=16) as pool:
                pending = pool.submit(http, service.url, "/v1/operations", payload)
                try:
                    service.event("checkpoint")
                    reads = list(pool.map(lambda _: service.get("shared"), range(12)))
                    for status, value in reads:
                        self.assertEqual(status, 200)
                        self.assertEqual(value["revision"], seed["revision"])
                    self.assertEqual(http(service.url, "/v1/search?q=newword")[1]["results"], [])
                    self.assertEqual(http(service.url, "/v1/operations", write_input("overflow"))[0], 503)
                finally:
                    (service.directory / ".resume").touch()
                self.assertEqual(pending.result()[0], 200)
            self.assertIn("newword", service.get("shared")[1]["body"])

    def test_disconnected_client_keeps_accepted_write(self):
        with Service(JBRAIN_TEST_PAUSE_PHASE="prepared") as service:
            payload = write_input("disconnected", md("survivedword"), requestId="crash-request")
            address = urllib.parse.urlparse(service.url)
            connection = socket.create_connection((address.hostname, address.port))
            body = json.dumps(payload).encode()
            connection.sendall(f"POST /v1/operations HTTP/1.1\r\nHost: {address.netloc}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            service.event("checkpoint")
            connection.close()
            (service.directory / ".resume").touch()
            status, receipt = http(service.url, "/v1/operations", payload)
            self.assertEqual(status, 200, receipt)
            self.assertIn("survivedword", service.get("disconnected")[1]["body"])
            self.assertEqual(http(service.url, "/v1/operations", payload)[1], receipt)

    def test_pending_byte_budget_and_duplicate_retry(self):
        with Service(JBRAIN_TEST_PAUSE_PHASE="accepted", JBRAIN_MAX_PENDING_BYTES="600") as service:
            payload = write_input("budget", md("界" * 60), requestId="crash-request")
            with futures.ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(http, service.url, "/v1/operations", payload)
                try:
                    service.event("checkpoint")
                    status, error = http(service.url, "/v1/operations", write_input("overflow", md("界" * 60)))
                    self.assertEqual((status, error["error"]["code"]), (503, "QUEUE_FULL"))
                finally:
                    (service.directory / ".resume").touch()
                receipt = pending.result()
            self.assertEqual(http(service.url, "/v1/operations", payload), receipt)
            self.assertFalse((service.directory / "knowledge/.git").exists())

    def test_oversized_offline_import_fails_before_reading_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "knowledge").mkdir()
            with (root / "knowledge/oversized.md").open("wb") as file:
                file.write(md().encode())
                file.truncate(20 * 1024 * 1024)
            proc = subprocess.run([BINARY, "serve"], env={**os.environ, "JBRAIN_DATA_DIR": directory, "JBRAIN_PORT": "0"}, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stderr)["error"]["code"], "INVALID_OKF")

    def test_owner_guard_and_directory_alias(self):
        with Service() as service, tempfile.TemporaryDirectory() as aliases:
            alias = pathlib.Path(aliases) / "same-brain"
            alias.symlink_to(service.directory)
            for path in [service.directory, alias]:
                proc = subprocess.run([BINARY, "serve"], env={**os.environ, "JBRAIN_DATA_DIR": str(path), "JBRAIN_PORT": "0"}, capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("owning service", proc.stderr)
            service.put("still-working")

    def test_okf_aliases_links_and_unknown_fields(self):
        with Service() as service:
            content = md("[target](./target.md#x) and [ref][r]\n\n[r]: /other/linked.md", "title: Knowledge\naliases: [collectivebrain]\ncustom: { retained: true }\nsources: [{ resource: https://example.com/spec }]\n")
            service.put("notes/source", content)
            service.put("notes/target")
            concept = service.get("notes/source")[1]
            self.assertEqual(concept["markdown"], content)
            self.assertEqual(concept["metadata"]["custom"], {"retained": True})
            self.assertEqual(len(http(service.url, "/v1/search?q=collectivebrain")[1]["results"]), 1)
            links = http(service.url, "/v1/links?id=notes/source")[1]
            self.assertEqual([(item["id"], item["resolved"]) for item in links["outgoing"]], [("notes/target", 1), ("other/linked", 0)])
            self.assertEqual(http(service.url, "/v1/links?id=notes/target")[1]["incoming"][0]["id"], "notes/source")

    def test_validation_and_authentication(self):
        with Service() as service:
            for concept in ["../escape", "/absolute", "nested/../../escape", ".git/config", "index", "nested/log", "Uppercase", "name.md"]:
                self.assertEqual(http(service.url, "/v1/operations", write_input(concept))[0], 400)
            for content in ["missing header", "---\ntitle: Missing\n---\n", "---\ntype: Note\ntype: Other\n---\n", "---\ntype: Note\nsources: [{}]\n---\n"]:
                self.assertEqual(http(service.url, "/v1/operations", write_input("invalid", content))[0], 422)
            blind = write_input("blind")
            del blind["expectedRevision"]
            self.assertEqual(http(service.url, "/v1/operations", blind)[0], 400)
            self.assertEqual(http(service.url, "/v1/operations", raw=b"{")[0], 400)
            self.assertEqual(http(service.url, "/health", headers={"Origin": "https://other.example"})[0], 403)
            service.put("valid")
        with Service(JBRAIN_TOKEN="test-only-token") as service:
            self.assertEqual(http(service.url, "/health")[0], 401)
            self.assertEqual(http(service.url, "/health", headers={"Authorization": "Bearer test-only-token"})[0], 200)

    def test_native_cli(self):
        with Service() as service:
            file = service.directory / "input.md"
            file.write_text(md("cliword"))
            env = {**os.environ, "JBRAIN_URL": service.url, "JBRAIN_TOKEN": ""}
            args = [BINARY, "put", "cli-note", "--file", str(file), "--actor", "cli/1", "--request-id", "cli-create", "--create"]
            write = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(write.returncode, 0, write.stderr)
            read = subprocess.run([BINARY, "get", "cli-note"], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(write.stdout)["revision"], json.loads(read.stdout)["revision"])
            args[args.index("cli-create")] = "cli-conflict"
            conflict = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(conflict.returncode, 1)
            self.assertEqual(json.loads(conflict.stderr)["error"]["code"], "REVISION_CONFLICT")

    def test_direct_http_mcp_multiple_clients(self):
        with Service() as service:
            headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
            def rpc(method, params, identity=1):
                return http(service.url, "/mcp", {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}, headers)
            status, initialized = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
            self.assertEqual(status, 200, initialized)
            self.assertIn("tools", initialized["result"]["capabilities"])
            status, tools = rpc("tools/list", {})
            self.assertIn("brain_write", [tool["name"] for tool in tools["result"]["tools"]])
            def agent(i):
                status, result = rpc("tools/call", {"name": "brain_write", "arguments": write_input(f"mcp/note-{i}", md("mcpword"))}, i)
                self.assertEqual(status, 200, result)
                self.assertFalse(result["result"].get("isError", False), result)
            with futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(agent, range(8)))
            status, conflict = rpc("tools/call", {"name": "brain_write", "arguments": write_input("mcp/note-0")})
            self.assertTrue(conflict["result"]["isError"])
            self.assertEqual(json.loads(conflict["result"]["content"][0]["text"])["error"]["code"], "REVISION_CONFLICT")
            self.assertEqual(http(service.url, "/health")[1]["concepts"], 8)

    def test_native_stdio_mcp_proxy(self):
        with Service() as service:
            proc = subprocess.Popen([BINARY, "mcp"], env={**os.environ, "JBRAIN_URL": service.url, "JBRAIN_TOKEN": ""}, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def send(value):
                proc.stdin.write(json.dumps(value) + "\n")
                proc.stdin.flush()
            def receive():
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(10), "No MCP stdio response")
                    return json.loads(proc.stdout.readline())
            try:
                send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
                self.assertIn("result", receive())
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "brain_write", "arguments": write_input("stdio", md("nativeword"))}})
                self.assertFalse(receive()["result"].get("isError", False))
                self.assertIn("nativeword", service.get("stdio")[1]["body"])
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)
                proc.stdout.close()
                proc.stderr.close()

    def test_incremental_restart_offline_changes_and_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = write_input("persistent")
            with Service(directory) as service:
                receipt = http(service.url, "/v1/operations", payload)
                conflict = write_input("persistent")
                self.assertEqual(http(service.url, "/v1/operations", conflict)[0], 409)
            index = pathlib.Path(directory) / "index.sqlite"
            with contextlib.closing(sqlite3.connect(index)) as conn:
                data_version = conn.execute("SELECT document FROM concepts WHERE id='persistent'").fetchone()[0]
            with Service(directory) as service:
                self.assertEqual(http(service.url, "/v1/operations", payload), receipt)
                self.assertEqual(http(service.url, "/v1/operations", conflict)[0], 409)
                with contextlib.closing(sqlite3.connect(index)) as conn:
                    self.assertEqual(conn.execute("SELECT document FROM concepts WHERE id='persistent'").fetchone()[0], data_version)
            (pathlib.Path(directory) / "knowledge/persistent.md").write_text(md("offlineword"))
            with Service(directory) as service:
                self.assertIn("offlineword", service.get("persistent")[1]["body"])

    def test_legacy_journal_index_migration_and_owner_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "knowledge").mkdir()
            applied = write_input("legacy", md("legacyword"), actor="researcher/旧")
            queued = write_input("legacy-pending", md("pendingword"))
            receipt = {"requestId": applied["requestId"], "id": "legacy", "revision": hashlib.sha256(applied["markdown"].encode()).hexdigest(), "gitCommit": "preserved-old-receipt"}
            (root / "knowledge/legacy.md").write_text(applied["markdown"])
            with contextlib.closing(sqlite3.connect(root / "operations.sqlite")) as conn:
                conn.execute("CREATE TABLE operations(sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, input TEXT NOT NULL, state TEXT NOT NULL, status INTEGER, result TEXT, accepted_at TEXT NOT NULL, completed_at TEXT)")
                for payload, state, result in [(applied, "applied", receipt), (queued, "queued", None)]:
                    fingerprint = hashlib.sha256(json.dumps([payload.get(k) for k in ["requestId", "actor", "action", "id", "expectedRevision", "markdown"]], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                    conn.execute("INSERT INTO operations(request_id,fingerprint,input,state,status,result,accepted_at) VALUES(?,?,?,?,?,?,?)", (payload["requestId"], fingerprint, json.dumps(payload), state, 200 if result else None, json.dumps(result) if result else None, "2026-10-24T00:00:00.000Z"))
                conn.commit()
            with contextlib.closing(sqlite3.connect(root / "index.sqlite")) as conn:
                conn.execute("CREATE TABLE concepts(id TEXT PRIMARY KEY, revision TEXT NOT NULL, markdown TEXT NOT NULL, title TEXT NOT NULL, type TEXT NOT NULL, description TEXT NOT NULL, tags TEXT NOT NULL)")
                conn.commit()
            with contextlib.closing(sqlite3.connect(root / "owner.sqlite")) as legacy_owner:
                legacy_owner.execute("CREATE TABLE owner(singleton INTEGER PRIMARY KEY)")
                legacy_owner.commit()
                legacy_owner.execute("BEGIN EXCLUSIVE")
                proc = subprocess.run([BINARY, "serve"], env={**os.environ, "JBRAIN_DATA_DIR": directory, "JBRAIN_PORT": "0"}, capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("previous Bun service", proc.stderr)
            with Service(directory) as service:
                self.assertEqual(http(service.url, "/v1/operations", applied), (200, receipt))
                self.assertEqual(service.get("legacy")[1]["markdown"], applied["markdown"])
                self.assertIn("pendingword", service.get("legacy-pending")[1]["body"])
                self.assertEqual(http(service.url, "/v1/operations", queued)[0], 200)
                with contextlib.closing(sqlite3.connect(root / "owner.sqlite", timeout=0)) as contender:
                    with self.assertRaises(sqlite3.OperationalError):
                        contender.execute("BEGIN EXCLUSIVE")

    def test_optional_git_history(self):
        with Service(JBRAIN_HISTORY="git") as service:
            payload = write_input("tracked")
            first = http(service.url, "/v1/operations", payload)
            self.assertEqual(first[0], 200)
            self.assertIsNotNone(first[1]["gitCommit"])
            self.assertEqual(http(service.url, "/v1/operations", payload), first)
            count = subprocess.check_output(["git", "-C", str(service.directory / "knowledge"), "rev-list", "--count", "HEAD"], text=True).strip()
            self.assertEqual(count, "1")

    @staticmethod
    def wait_for(check, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.2)
        raise AssertionError("Timed out")

    def test_git_remote_push(self):
        with tempfile.TemporaryDirectory() as remote:
            subprocess.check_call(["git", "init", "--bare", "--quiet", remote])
            head = lambda: subprocess.run(["git", "--git-dir", remote, "rev-parse", "--verify", "refs/heads/main"], capture_output=True, text=True).stdout.strip()
            with Service(JBRAIN_HISTORY="git", JBRAIN_GIT_REMOTE=remote) as service:
                for name in ["one", "two"]:
                    commit = service.put(name)["gitCommit"]
                    self.wait_for(lambda: head() == commit)
                sync = self.wait_for(lambda: (h := http(service.url, "/health")[1])["sync"]["pending"] is False and h["sync"])
                self.assertEqual((sync["pushedCommit"], sync["lastError"], sync["remote"]), (commit, None, remote))

    def test_unreachable_git_remote_never_blocks_writes_and_catches_up(self):
        with tempfile.TemporaryDirectory() as parent:
            remote = pathlib.Path(parent) / "late.git"
            with Service(JBRAIN_HISTORY="git", JBRAIN_GIT_REMOTE=str(remote)) as service:
                commit = service.put("offline")["gitCommit"]
                error = self.wait_for(lambda: http(service.url, "/health")[1]["sync"]["lastError"])
                self.assertTrue(error)
                self.assertEqual(http(service.url, "/health")[1]["status"], "ready")
                subprocess.check_call(["git", "init", "--bare", "--quiet", str(remote)])
                self.wait_for(lambda: subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"], capture_output=True, text=True).stdout.strip() == commit)

    def test_git_remote_requires_git_history(self):
        with self.assertRaises(AssertionError):
            Service(JBRAIN_HISTORY="off", JBRAIN_GIT_REMOTE="/tmp/nowhere.git")

    def test_unexpected_edits_and_symlinks_are_not_overwritten(self):
        with Service() as service:
            seed = service.put("external")
            path = service.directory / "knowledge/external.md"
            path.write_text(md("outside"))
            self.assertEqual(http(service.url, "/v1/operations", write_input("external", md("overwrite"), expectedRevision=seed["revision"]))[0], 503)
            self.assertIn("outside", path.read_text())
        with Service() as service:
            outside = service.directory / "outside.md"
            outside.write_text(md("outside"))
            (service.directory / "knowledge/linked.md").symlink_to(outside)
            self.assertEqual(http(service.url, "/v1/operations", write_input("linked"))[0], 503)
            self.assertIn("outside", outside.read_text())

    def test_crash_recovery_all_boundaries(self):
        for history in ["off", "git"]:
            for action in ["put", "delete"]:
                for phase in ["accepted", "prepared", "file", "git", "index"]:
                    with self.subTest(history=history, action=action, phase=phase), tempfile.TemporaryDirectory() as directory:
                        first = Service(directory, JBRAIN_HISTORY=history, JBRAIN_TEST_PAUSE_PHASE=phase)
                        try:
                            seed = first.put("target", md("original"))
                            payload = {"requestId": "crash-request", "actor": "crash/1", "action": action, "id": "target", "expectedRevision": seed["revision"]}
                            if action == "put":
                                payload["markdown"] = md("recoveredword")
                            queued = write_input("queued", md("queuedword"))
                            with futures.ThreadPoolExecutor(max_workers=2) as pool:
                                interrupted = pool.submit(http, first.url, "/v1/operations", payload)
                                first.event("checkpoint")
                                other = pool.submit(http, first.url, "/v1/operations", queued)
                                for _ in range(100):
                                    status, op = http(first.url, "/v1/operations/" + queued["requestId"])
                                    if status == 200 and op["state"] == "queued":
                                        break
                                    time.sleep(.005)
                                else:
                                    self.fail("Second agent was not durably queued")
                                first.kill()
                                for request in [interrupted, other]:
                                    with contextlib.suppress(Exception):
                                        request.result()
                            for suffix in ["", "-wal", "-shm"]:
                                (pathlib.Path(directory) / ("index.sqlite" + suffix)).unlink(missing_ok=True)
                            with Service(directory, JBRAIN_HISTORY=history) as recovered:
                                status, receipt = http(recovered.url, "/v1/operations", payload)
                                self.assertEqual(status, 200, receipt)
                                self.assertEqual(http(recovered.url, "/v1/operations/" + payload["requestId"])[1]["state"], "applied")
                                self.assertIn("queuedword", recovered.get("queued")[1]["body"])
                                self.assertEqual(recovered.get("target")[0], 200 if action == "put" else 404)
                                self.assertEqual(http(recovered.url, "/health")[1]["pendingWrites"], 0)
                        finally:
                            first.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--agent":
        probe_agent(sys.argv[2], sys.argv[3])
    else:
        unittest.main(verbosity=2)
