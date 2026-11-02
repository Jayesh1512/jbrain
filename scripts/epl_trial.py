#!/usr/bin/env python3
"""Prepare a sourced OKF football corpus and log evidence retrieval; Python stdlib only."""
import argparse
import collections
import concurrent.futures
import csv
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / ".jbrain/epl-trial"
VERSION = "epl-results-v1"
NAMES = {
    "Birmingham": "Birmingham City", "Blackburn": "Blackburn Rovers", "Bolton": "Bolton Wanderers",
    "Bradford": "Bradford City", "Brighton": "Brighton and Hove Albion", "Cardiff": "Cardiff City",
    "Charlton": "Charlton Athletic", "Coventry": "Coventry City", "Derby": "Derby County",
    "Huddersfield": "Huddersfield Town", "Hull": "Hull City", "Ipswich": "Ipswich Town",
    "Leeds": "Leeds United", "Leicester": "Leicester City", "Luton": "Luton Town",
    "Man City": "Manchester City", "Man United": "Manchester United", "Newcastle": "Newcastle United",
    "Norwich": "Norwich City", "Nott'm Forest": "Nottingham Forest", "Oldham": "Oldham Athletic",
    "QPR": "Queens Park Rangers", "Sheffield Weds": "Sheffield Wednesday", "Stoke": "Stoke City",
    "Swansea": "Swansea City", "Swindon": "Swindon Town", "Tottenham": "Tottenham Hotspur",
    "West Brom": "West Bromwich Albion", "West Ham": "West Ham United", "Wigan": "Wigan Athletic",
    "Wolves": "Wolverhampton Wanderers",
}
EXTRA_ALIASES = {"Manchester United": ["Man Utd", "Manchester Utd"],
                 "Manchester City": ["Man City"], "Nottingham Forest": ["Nottm Forest", "Nott Forest"],
                 "Brighton and Hove Albion": ["Brighton & Hove Albion", "Brighton"],
                 "Tottenham Hotspur": ["Spurs", "Tottenham"], "Bournemouth": ["AFC Bournemouth"]}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def season(year):
    return f"{year}/{str(year + 1)[-2:]}"


def season_id(year):
    return f"epl/seasons/{year}-{str(year + 1)[-2:]}"


def club_id(club):
    return "epl/clubs/" + slug(club)


def club_season_id(year, club):
    return f"epl/club-seasons/{year}-{str(year + 1)[-2:]}/{slug(club)}"


def pair_id(a, b):
    return "epl/head-to-head/" + "-vs-".join(sorted((slug(a), slug(b))))


def link(identity, label):
    return f"[{label}](/{identity}.md)"


def parse_csv(data, year):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    rows = csv.DictReader(io.StringIO(text))
    required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
    if not required.issubset(rows.fieldnames or []):
        raise ValueError(f"Missing required CSV fields in {season(year)}")
    matches, fixtures = [], set()
    for row_number, row in enumerate(rows, 2):
        if not any((row.get(key) or "").strip() for key in required):
            continue  # Some historical files contain trailing empty CSV rows.
        if any(not (row.get(key) or "").strip() for key in required):
            raise ValueError(f"Incomplete result at {season(year)} row {row_number}")
        if row.get("Div", "E0") != "E0":
            raise ValueError("Unexpected competition")
        date = None
        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
            try:
                date = datetime.datetime.strptime(row["Date"], fmt).date()
                break
            except ValueError:
                pass
        # The suspended 2019/20 competition finished after the usual June cutoff.
        last_date = datetime.date(year + 1, 8, 31) if year == 2019 else datetime.date(year + 1, 6, 30)
        if date is None or not datetime.date(year, 7, 1) <= date <= last_date:
            raise ValueError(f"Invalid season date at {season(year)} row {row_number}")
        home = NAMES.get(row["HomeTeam"], row["HomeTeam"])
        away = NAMES.get(row["AwayTeam"], row["AwayTeam"])
        hg, ag = int(row["FTHG"]), int(row["FTAG"])
        expected = "H" if hg > ag else "A" if hg < ag else "D"
        if hg < 0 or ag < 0 or row["FTR"] != expected or home == away:
            raise ValueError(f"Inconsistent match result at {season(year)} row {row_number}")
        if (home, away) in fixtures:
            raise ValueError("Duplicate home/away fixture")
        fixtures.add((home, away))
        identity = f"epl/matches/{year}-{str(year + 1)[-2:]}/{date.isoformat()}-{slug(home)}-vs-{slug(away)}"
        matches.append({"id": identity, "year": year, "date": date.isoformat(), "home": home, "away": away,
                        "hg": hg, "ag": ag, "result": expected, "row": row_number,
                        "raw": {k: v for k, v in row.items() if k and isinstance(v, str) and v.strip()}})
    teams = {m[key] for m in matches for key in ("home", "away")}
    expected_teams = 22 if year < 1995 else 20
    if len(teams) != expected_teams or len(matches) != expected_teams * (expected_teams - 1):
        raise ValueError(f"Incomplete season {season(year)}: {len(teams)} teams, {len(matches)} matches")
    if fixtures != {(a, b) for a in teams for b in teams if a != b}:
        raise ValueError("Season does not form a complete double round robin")
    return sorted(matches, key=lambda m: (m["date"], m["id"]))


def team_stats(matches, club):
    stats = {key: 0 for key in ("played", "won", "drawn", "lost", "goals_for", "goals_against", "points")}
    for m in matches:
        if club not in (m["home"], m["away"]):
            continue
        gf, ga = (m["hg"], m["ag"]) if club == m["home"] else (m["ag"], m["hg"])
        stats["played"] += 1
        stats["goals_for"] += gf
        stats["goals_against"] += ga
        stats["won" if gf > ga else "lost" if gf < ga else "drawn"] += 1
    stats["points"] = stats["won"] * 3 + stats["drawn"]
    stats["goal_difference"] = stats["goals_for"] - stats["goals_against"]
    return stats


def unbeaten_run(matches, club):
    current, best = [], []
    for m in sorted(matches, key=lambda m: (m["date"], m["id"])):
        if club not in (m["home"], m["away"]):
            continue
        gf, ga = (m["hg"], m["ag"]) if club == m["home"] else (m["ag"], m["hg"])
        if gf >= ga:
            current.append(m)
            if len(current) > len(best):
                best = current[:]
        else:
            current = []
    return best


def standings(matches):
    teams = {m[key] for m in matches for key in ("home", "away")}
    rows = [(club, team_stats(matches, club)) for club in teams]
    return sorted(rows, key=lambda item: (-item[1]["points"], -item[1]["goal_difference"], -item[1]["goals_for"], item[0]))


def stat_line(club, stats):
    return (f"{club} played {stats['played']} matches: won {stats['won']}, drew {stats['drawn']}, "
            f"lost {stats['lost']}. Goals scored: {stats['goals_for']}; goals conceded: {stats['goals_against']}; "
            f"goal difference: {stats['goal_difference']}. Points earned from match results: {stats['points']}.")


def document(identity, title, body, sources, aliases=(), extra=None):
    metadata = {"type": "Reference", "title": title, "description": title + ". Historical English Premier League results dataset.",
                "tags": ["football", "premier-league", "epl", "historical-results"], "aliases": list(aliases),
                "sources": [{"resource": source} for source in sorted(set(sources))], "dataset": VERSION, **(extra or {})}
    markdown = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items()) + "\n---\n" + body + "\n"
    return {"id": identity, "markdown": markdown, "revision": digest(markdown.encode())}


def build_documents(matches, manifest):
    by_year, by_club, by_pair = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
    urls = {item["start_year"]: item["url"] for item in manifest}
    for m in matches:
        by_year[m["year"]].append(m)
        for club in (m["home"], m["away"]):
            by_club[club].append(m)
        by_pair[tuple(sorted((m["home"], m["away"])))].append(m)
    documents = []
    minimum, maximum = min(by_year), max(by_year)
    scope = (f"Coverage is Premier League matches in seasons {season(minimum)} through {season(maximum)} only. "
             "The inaugural 1992/93 season, later seasons, cup matches, player statistics, managers, transfers and live information are not included.")
    table_note = ("Points are calculated as three per win and one per draw, BEFORE disciplinary deductions or other administrative adjustments. "
                  "Result tables are sorted by earned points, goal difference and goals scored; they are NOT official final league standings. "
                  "A match date is the recorded local date; timezone and kickoff times are not normalized.")
    overview = ["# English Premier League historical results", scope, "Source: Football-Data.co.uk historical English results CSV files.",
                f"This snapshot contains {len(matches):,} completed matches, {len(by_year)} seasons and {len(by_club)} clubs.",
                table_note, "Knowledge was generated deterministically from source rows without an ingestion model. "
                "Aliases expand common club names; cross-links use canonical document IDs. "
                "Derived summaries are computed from this snapshot, not independent source evidence.",
                "## Seasons", *[f"- {link(season_id(y), season(y))}" for y in sorted(by_year)],
                "## Clubs", *[f"- {link(club_id(c), c)}" for c in sorted(by_club)],
                "## Dataset records", link("epl/records", "Records within this dataset")]
    documents.append(document("epl/overview", "English Premier League historical results corpus", "\n\n".join(overview), urls.values(),
                              ["EPL", "Premier League", "English Premier League", "dataset coverage"], {"coverage_start": season(minimum), "coverage_end": season(maximum)}))
    for year, games in sorted(by_year.items()):
        table = standings(games)
        rows = ["| Club | P | W | D | L | GF | GA | GD | Earned points |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for club, stats in table:
            values = [stats[key] for key in ("played", "won", "drawn", "lost", "goals_for", "goals_against", "goal_difference", "points")]
            rows.append("| " + link(club_season_id(year, club), club) + " | " + " | ".join(map(str, values)) + " |")
        leader = table[0]
        body = (f"# Premier League {season(year)}\n\n{len(games)} matches; {len(table)} clubs; "
                f"{sum(m['hg'] + m['ag'] for m in games)} goals. Match dates: {min(m['date'] for m in games)} to {max(m['date'] for m in games)}.\n\n"
                f"Highest earned points: {leader[0]}, {leader[1]['points']}.\n\n{table_note}\n\n" + "\n".join(rows) +
                "\n\n" + link("epl/overview", "Dataset coverage and source limitations"))
        documents.append(document(season_id(year), f"Premier League {season(year)} season results table", body, [urls[year]],
                                  [f"EPL {season(year)}", f"{year}-{year + 1} Premier League", f"{year}-{str(year + 1)[-2:]} standings"], {"season": season(year)}))
        for club, stats in table:
            club_games = [m for m in games if club in (m["home"], m["away"])]
            body = [f"# {club} in Premier League {season(year)}", stat_line(club, stats), table_note,
                    f"{club} were unbeaten in this season's league matches." if stats["lost"] == 0 else "",
                    link(club_id(club), f"{club} club summary"), link(season_id(year), f"{season(year)} season table"), "## Fixtures and results"]
            body += [f"- {link(m['id'], m['date'] + ' ' + m['home'] + ' ' + str(m['hg']) + '-' + str(m['ag']) + ' ' + m['away'])}" for m in club_games]
            aliases = [f"{club} {season(year)}", f"{club} {year}-{year + 1}"]
            documents.append(document(club_season_id(year, club), f"{club} Premier League {season(year)} results", "\n\n".join(filter(None, body)), [urls[year]], aliases,
                                      {"season": season(year), "club": club, "computed_statistics": stats}))
    for club, games in sorted(by_club.items()):
        years = sorted({m["year"] for m in games})
        stats = team_stats(games, club)
        run = unbeaten_run(games, club)
        body = [f"# {club}: covered Premier League history", scope, stat_line(club, stats), table_note]
        if run:
            body += [f"Longest unbeaten Premier League run within the covered results: {len(run)} matches, "
                     f"from {run[0]['date']} to {run[-1]['date']}. This can be truncated by dataset boundaries.",
                     link(run[0]["id"], "First match of this unbeaten run"), link(run[-1]["id"], "Last match of this unbeaten run")]
        body += ["## Seasons", *[f"- {link(club_season_id(y, club), club + ' ' + season(y))}" for y in years],
                 "## Head-to-head summaries", *[f"- {link(pair_id(a, b), a + ' vs ' + b)}" for a, b in sorted(by_pair) if club in (a, b)],
                 link("epl/overview", "Dataset scope")]
        aliases = sorted({raw for raw, canonical in NAMES.items() if canonical == club} | set(EXTRA_ALIASES.get(club, [])))
        documents.append(document(club_id(club), club + " Premier League club results history", "\n\n".join(body), [urls[y] for y in years], aliases))
    for (a, b), games in sorted(by_pair.items()):
        a_stats = team_stats(games, a)
        body = [f"# {a} vs {b}: Premier League head-to-head", scope,
                f"{len(games)} meetings in this dataset. {a} wins: {a_stats['won']}; {b} wins: {a_stats['lost']}; draws: {a_stats['drawn']}. "
                f"Goals: {a} {a_stats['goals_for']}, {b} {a_stats['goals_against']}.",
                link(club_id(a), a), link(club_id(b), b), "## By season",
                "| Season | Meetings | " + a + " wins | Draws | " + b + " wins | " + a + " goals | " + b + " goals |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for y in sorted({m["year"] for m in games}):
            s = team_stats([m for m in games if m["year"] == y], a)
            body.append(f"| {link(season_id(y), season(y))} | {s['played']} | {s['won']} | {s['drawn']} | {s['lost']} | {s['goals_for']} | {s['goals_against']} |")
        documents.append(document(pair_id(a, b), f"{a} vs {b} Premier League head-to-head results", "\n\n".join(body), [urls[y] for y in sorted({m['year'] for m in games})],
                                  [f"{b} vs {a}", f"{a} versus {b}", f"{a} {b} head to head"]))
    for m in matches:
        raw = m["raw"]
        result = f"{m['home']} won." if m["result"] == "H" else f"{m['away']} won." if m["result"] == "A" else "The match was a draw."
        body = [f"# {m['home']} {m['hg']}-{m['ag']} {m['away']}",
                f"English Premier League, season {season(m['year'])}; match date {m['date']}. "
                f"Home team: {m['home']}. Away team: {m['away']}. Full-time score: {m['hg']}-{m['ag']}. {result}"]
        if raw.get("HTHG") and raw.get("HTAG"):
            body.append(f"Half-time score: {raw['HTHG']}-{raw['HTAG']}.")
        if raw.get("Referee"):
            body.append("Referee: " + raw["Referee"] + ".")
        for label, home_key, away_key in [("Shots", "HS", "AS"), ("Shots on target", "HST", "AST"),
                                         ("Corners", "HC", "AC"), ("Fouls committed", "HF", "AF"),
                                         ("Yellow cards", "HY", "AY"), ("Red cards", "HR", "AR")]:
            if raw.get(home_key) and raw.get(away_key):
                body.append(f"{label}: {m['home']} {raw[home_key]}, {m['away']} {raw[away_key]}.")
        body += [f"Source CSV record starts on line {m['row']} (header is line 1).",
                 link(season_id(m["year"]), season(m["year"]) + " season"), link(club_id(m["home"]), m["home"]), link(club_id(m["away"]), m["away"]),
                 link(club_season_id(m["year"], m["home"]), m["home"] + " season results"),
                 link(club_season_id(m["year"], m["away"]), m["away"] + " season results"),
                 link(pair_id(m["home"], m["away"]), "Head-to-head history")]
        documents.append(document(m["id"], f"{m['home']} vs {m['away']} {m['date']} Premier League {season(m['year'])}", "\n\n".join(body), [urls[m["year"]]],
                                  extra={"season": season(m["year"]), "match_date": m["date"], "source_row": m["row"], "home_team": m["home"], "away_team": m["away"]}))
    records = ["# Premier League records within this dataset", scope, table_note, "## Highest-scoring matches"]
    records += [f"- {link(m['id'], m['date'] + ' ' + m['home'] + ' ' + str(m['hg']) + '-' + str(m['ag']) + ' ' + m['away'])}" for m in sorted(matches, key=lambda m: (-(m['hg'] + m['ag']), m['date'], m['id']))[:20]]
    records += ["## Highest earned points in a club season"]
    seasons = [(year, club, s) for year, games in by_year.items() for club, s in standings(games)]
    records += [f"- {link(club_season_id(y, c), c + ' ' + season(y))}: {s['points']} points, {s['won']} wins, {s['drawn']} draws, {s['lost']} losses." for y, c, s in sorted(seasons, key=lambda x: (-x[2]['points'], x[0], x[1]))[:20]]
    records += ["## Unbeaten full seasons", *[link(club_season_id(y, c), c + ' ' + season(y)) for y, c, s in seasons if s["lost"] == 0], link("epl/overview", "Dataset scope")]
    documents.append(document("epl/records", "Premier League dataset records points goals unbeaten seasons", "\n\n".join(records), urls.values()))
    return documents


def prepare(source_dir, root):
    started = time.perf_counter()
    manifest = json.loads((source_dir / "manifest.json").read_text())
    if [item["start_year"] for item in manifest] != list(range(1993, 2025)):
        raise ValueError("This trial requires all 32 seasons, 1993/94 through 2024/25")
    matches = []
    target = root / "sources"
    target.mkdir(parents=True, exist_ok=True)
    for item in manifest:
        data = (source_dir / item["file"]).read_bytes()
        if digest(data) != item["sha256"]:
            raise ValueError("Source hash mismatch")
        matches.extend(parse_csv(data, item["start_year"]))
        shutil.copyfile(source_dir / item["file"], target / item["file"])
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    docs = build_documents(matches, manifest)
    identities = {d["id"] for d in docs}
    if len(identities) != len(docs):
        raise ValueError("Duplicate concept IDs")
    links = [(d["id"], target) for d in docs for target in set(re.findall(r"\]\(/([^()]+)\.md\)", d["markdown"]))]
    missing = sorted({t for _, t in links} - identities)
    if missing:
        raise ValueError(f"Broken generated links: {missing}")
    with (root / "documents.jsonl").open("w") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    report = {"dataset": VERSION, "created_at": now(), "seasons": len(manifest), "matches": len(matches),
              "clubs": len({m[k] for m in matches for k in ("home", "away")}), "documents": len(docs),
              "directed_links": len(links), "unresolved_generated_links": missing,
              "date_start": min(m["date"] for m in matches), "date_end": max(m["date"] for m in matches),
              "coverage": "Premier League 1993/94 through 2024/25; excludes inaugural 1992/93 and subsequent seasons",
              "source_bytes": sum(item["bytes"] for item in manifest), "markdown_bytes": sum(len(d["markdown"].encode()) for d in docs),
              "documents_sha256": digest((root / "documents.jsonl").read_bytes()),
              "generator_sha256": digest(Path(__file__).read_bytes()), "preparation_seconds": round(time.perf_counter() - started, 3),
              "separate_model_api_calls": 0, "model_usage_note": "Deterministic conversion only. Coding/research agent token usage is unmeasured and excluded."}
    (root / "corpus.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def request(url, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def sample(pid):
    raw = subprocess.check_output(["ps", "-o", "rss=,time=", "-p", str(pid)], text=True).split()
    parts = raw[1].split(":")
    return {"rss_mib": round(int(raw[0]) / 1024, 3), "cpu_seconds": sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))}


def session(root):
    return json.loads((root / "session.json").read_text())


def start(root, port, model="gpt-6-luna", key_file=None):
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "session.json"
    previous = json.loads(config_path.read_text()) if config_path.exists() else {}
    if previous:
        try:
            request(previous["url"], "/health")
        except (OSError, ValueError):
            pass
        else:
            raise ValueError("A service already answers at the recorded URL; reuse it or stop it explicitly")
    binary = ROOT / "target/release/jbrain"
    if not binary.is_file():
        raise ValueError("Build first: cargo build --release --locked")
    env = {**os.environ, "JBRAIN_DATA_DIR": str(root / "brain"), "JBRAIN_HOST": "127.0.0.1",
           "JBRAIN_PORT": str(port), "JBRAIN_TOKEN": "", "JBRAIN_HISTORY": "off",
           "JBRAIN_MAX_PENDING": "64", "JBRAIN_MAX_PENDING_BYTES": "8388608", "JBRAIN_TEST_PAUSE_PHASE": ""}
    # Pin both roles for this trial. An inherited shell setting must not silently
    # restore the expensive development model on restart.
    env["JBRAIN_MODEL"] = model
    env["JBRAIN_VERIFIER_MODEL"] = model
    if key_file is not None:
        env["JBRAIN_API_KEY_FILE"] = str(key_file)
        env["OPENAI_API_KEY"] = ""
    log_path = root / "service.log"
    offset = log_path.stat().st_size if log_path.exists() else 0
    begun = time.perf_counter()
    with log_path.open("ab") as log:
        proc = subprocess.Popen([str(binary), "serve"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
    ready = None
    for _ in range(300):
        text = log_path.read_bytes()[offset:].decode(errors="replace")
        if proc.poll() is not None:
            raise RuntimeError("Service startup failed: " + text)
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "ready":
                ready = event
                break
        if ready:
            break
        time.sleep(.1)
    if ready is None:
        proc.terminate()
        raise RuntimeError("Service did not become ready within 30 seconds")
    config = {**previous, "dataset": VERSION, "pid": proc.pid, "url": ready["url"], "started_at": now(),
              "data_dir": str(root / "brain"), "binary_sha256": digest(binary.read_bytes()),
              "startup_seconds": round(time.perf_counter() - begun, 3),
              "answer_model": model if env.get("JBRAIN_API_KEY_FILE") or env.get("OPENAI_API_KEY") else None,
              "verifier_model": model if env.get("JBRAIN_API_KEY_FILE") or env.get("OPENAI_API_KEY") else None,
              "provider": "OpenAI Responses" if env.get("JBRAIN_API_KEY_FILE") or env.get("OPENAI_API_KEY") else "disabled"}
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(json.dumps(config, indent=2))


def ingest(root):
    config = session(root)
    url, pid = config["url"], config["pid"]
    if request(url, "/health")["concepts"]:
        raise ValueError("Initial ingestion requires an empty dedicated brain; refusing to mix corpora")
    before, started = sample(pid), time.perf_counter()
    corpus = json.loads((root / "corpus.json").read_text())
    completed, sampled = 0, [before]
    receipts = root / "receipts.jsonl"

    def write(pair):
        number, d = pair
        receipt = request(url, "/v1/operations", {"requestId": VERSION + ":" + digest(d["id"].encode()), "actor": "epl-deterministic-importer/1", "action": "put",
                                                   "id": d["id"], "expectedRevision": None, "markdown": d["markdown"]})
        if receipt.get("revision") != d["revision"]:
            raise ValueError("Admitted revision does not match generated source")
        return receipt

    with (root / "documents.jsonl").open() as inputs, receipts.open("w") as output, concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        # Bound client-side queued work as well as the service's writer queue.
        while True:
            batch = [json.loads(line) for _, line in zip(range(100), inputs)]
            if not batch:
                break
            for receipt in pool.map(write, enumerate(batch, completed)):
                output.write(json.dumps(receipt) + "\n")
                completed += 1
            output.flush()
            sampled.append(sample(pid))
            if completed % 1000 == 0:
                print(f"Ingested {completed}/{corpus['documents']} concepts; {time.perf_counter() - started:.1f}s", flush=True)
    after = sample(pid)
    health = request(url, "/health")
    if health["concepts"] != corpus["documents"]:
        raise ValueError("Wrong stored document count")
    config["ingestion"] = {"documents": completed, "workers": 4, "wall_seconds": round(time.perf_counter() - started, 3),
                           "service_before": before, "service_after": after,
                           "max_sampled_rss_mib": max(s["rss_mib"] for s in sampled + [after]),
                           "service_cpu_seconds_delta": round(after["cpu_seconds"] - before["cpu_seconds"], 3),
                           "service_storage_bytes": sum(p.stat().st_size for p in (root / "brain").rglob("*") if p.is_file()),
                           "separate_model_api_calls": 0}
    (root / "session.json").write_text(json.dumps(config, indent=2) + "\n")
    print(json.dumps(config["ingestion"], indent=2))


def verify(root):
    config = session(root)
    url = config["url"]
    started = time.perf_counter()
    count, edges, revisions = 0, 0, {}
    with (root / "documents.jsonl").open() as f:
        documents = [json.loads(line) for line in f]

    def check(d):
        encoded = urllib.parse.quote(d["id"], safe="")
        actual = request(url, "/v1/concept?id=" + encoded)
        if actual["markdown"] != d["markdown"] or actual["revision"] != d["revision"]:
            raise ValueError("Source fidelity failure: " + d["id"])
        expected = sorted(set(re.findall(r"\]\(/([^()]+)\.md\)", d["markdown"])))
        if actual["links"] != expected:
            raise ValueError("Extracted links differ from generated links: " + d["id"])
        return d["id"], len(expected), actual["revision"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for identity, outgoing, revision in pool.map(check, documents):
            count += 1
            edges += outgoing
            revisions[identity] = revision
    corpus = json.loads((root / "corpus.json").read_text())
    # Every concept and extracted edge was checked above. Exercise resolution and backlinks on a deterministic sample.
    sample_docs = documents[::max(1, len(documents) // 80)]
    link_requests = 0
    for d in sample_docs:
        links = request(url, "/v1/links?" + urllib.parse.urlencode({"id": d["id"]}))
        link_requests += 1
        if any(not e["resolved"] or e["revision"] != revisions[e["id"]] for e in links["outgoing"]):
            raise ValueError("Unresolved or stale target")
        if links["outgoing"]:
            target = links["outgoing"][0]["id"]
            back = request(url, "/v1/links?" + urllib.parse.urlencode({"id": target}))
            link_requests += 1
            if d["id"] not in {e["id"] for e in back["incoming"]}:
                raise ValueError("Backlink missing")
    if edges != corpus["directed_links"] or count != corpus["documents"]:
        raise ValueError("Stored corpus totals mismatch")
    # Smoke tests check term retrieval, not accuracy against the user's future questions.
    smoke = {}
    for q, required in [("Arsenal 2003 04", "epl/club-seasons/2003-04/arsenal"),
                        ("Manchester City 2017 18", "epl/club-seasons/2017-18/manchester-city"),
                        ("dataset coverage", "epl/overview")]:
        results = request(url, "/v1/search?" + urllib.parse.urlencode({"q": q, "limit": 10}))["results"]
        ids = [e["id"] for e in results]
        smoke[q] = required in ids
    report = {"verified_at": now(), "documents_read_and_hash_verified": count, "extracted_edges_verified": edges,
              "resolution_sample_documents": len(sample_docs), "resolution_and_backlink_requests": link_requests,
              "smoke_queries": smoke, "wall_seconds": round(time.perf_counter() - started, 3),
              "service_after_verification": sample(config["pid"]),
              "note": "These are source/graph checks and smoke queries, not a scored user-question retrieval benchmark."}
    (root / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def ask(root, question, searches, read_ids, link_ids, limit, mode="query"):
    config = session(root)
    url = config["url"]
    before, begin = sample(config["pid"]), time.perf_counter()
    if mode == "query" and limit > 16:
        raise ValueError("Query mode limits results to 16; use --mode raw for the original search workflow")
    searches = ([question] if mode == "raw" else []) + searches
    searches = list(dict.fromkeys(searches))
    observations = []
    if mode == "query":
        t = time.perf_counter()
        result = request(url, "/v1/query", {"question": question, "limit": limit})
        observations.append({"kind": "query", "query": question, "latency_ms": round((time.perf_counter() - t) * 1000, 3), "response": result})
    for q in searches:
        t = time.perf_counter()
        result = request(url, "/v1/search?" + urllib.parse.urlencode({"q": q, "limit": limit}))
        observations.append({"kind": "search", "query": q, "latency_ms": round((time.perf_counter() - t) * 1000, 3), "response": result})
    for identity in read_ids:
        t = time.perf_counter()
        result = request(url, "/v1/concept?" + urllib.parse.urlencode({"id": identity}))
        observations.append({"kind": "read", "id": identity, "latency_ms": round((time.perf_counter() - t) * 1000, 3), "response": result})
    for identity in link_ids:
        t = time.perf_counter()
        result = request(url, "/v1/links?" + urllib.parse.urlencode({"id": identity}))
        observations.append({"kind": "links", "id": identity, "latency_ms": round((time.perf_counter() - t) * 1000, 3), "response": result})
    after = sample(config["pid"])
    row = {"at": now(), "question_id": digest(question.encode())[:16], "question": question, "observations": observations, "retrieval_http_requests": len(observations),
           "wall_ms": round((time.perf_counter() - begin) * 1000, 3), "service_before": before, "service_after": after,
           "service_cpu_seconds_delta": round(after["cpu_seconds"] - before["cpu_seconds"], 3),
           "response_utf8_bytes": sum(len(json.dumps(o["response"], ensure_ascii=False).encode()) for o in observations),
           "separate_model_api_calls": 0, "agent_model_tokens": None,
           "answer": None, "answer_correct": None, "evidence_complete": None,
           "retrieval_mode": mode,
           "usage_note": "Query mode uses deterministic Rust planning; raw mode preserves the original literal search. Additional keyword searches are agent-provided. Billed agent reasoning is unmeasured. No automated quality judgment."}
    path = root / "questions.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--source-dir", type=Path, required=True)
    p = commands.add_parser("start")
    p.add_argument("--port", type=int, default=7331)
    p.add_argument("--model", default="gpt-6-luna", help="Answer and verifier model (default: low-cost gpt-6-luna)")
    p.add_argument("--key-file", type=Path, help="Private API key file, outside this repository")
    commands.add_parser("ingest")
    commands.add_parser("verify")
    p = commands.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--mode", choices=["query", "raw"], default="query")
    p.add_argument("--search", action="append", default=[])
    p.add_argument("--read", action="append", default=[])
    p.add_argument("--links", action="append", default=[])
    p.add_argument("--limit", type=int, default=5, choices=range(1, 101))
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.source_dir, args.root)
    elif args.command == "start":
        start(args.root.resolve(), args.port, args.model, args.key_file)
    elif args.command == "ingest":
        ingest(args.root)
    elif args.command == "verify":
        verify(args.root)
    else:
        ask(args.root, args.question, args.search, args.read, args.links, args.limit, args.mode)


if __name__ == "__main__":
    main()
