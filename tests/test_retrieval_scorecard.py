from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from dsc_mcp import catalog
from eval import retrieval_scorecard as scorecard


ROOT = Path(__file__).resolve().parents[1]
PAGE = "https://numpy.org/doc/stable/reference/generated/numpy.orbit.html"
URL = PAGE + "#numpy.orbit"


@pytest.fixture
def snapshot(tmp_path):
    db = tmp_path / "docs.sqlite3"
    texts = ["First Evidence has Café shapes.", "Second supporting passage.",
             "Third supporting passage.", "Fourth supporting passage.", "Final distant evidence."]
    with sqlite3.connect(db) as connection:
        connection.executescript(catalog.SCHEMA)
        connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        for index, text in enumerate(texts, 1):
            doc_id = f"{index:024x}"
            heading = f"Orbit {index}"
            connection.execute("INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (doc_id, "numpy", "Orbit", heading, text, URL, "2.0", "2026-10-18T00:00:00Z", sha256(text.encode()).hexdigest()))
            connection.execute("INSERT INTO sections_fts VALUES (?, ?, ?, ?, ?)",
                               ("numpy", "Orbit", heading, text, doc_id))
        text = "Combine tables with an outer join."
        connection.execute("INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (f"{6:024x}", "pandas", "Joins", "Merge", text,
                            "https://pandas.pydata.org/docs/user_guide/merging.html#outer", "3.0", "2026-10-18T00:00:00Z", sha256(text.encode()).hexdigest()))
        connection.execute("INSERT INTO sections_fts VALUES (?, ?, ?, ?, ?)",
                           ("pandas", "Joins", "Merge", text, f"{6:024x}"))
    return db


def requirement(name="first", quote="first evidence has café shapes", url=URL):
    return {"name": name, "alternatives": [{"url": url, "quote": quote}]}


def evidence(**changes):
    return {"id": "evidence", "kind": "evidence", "category": "paraphrase", "library": "numpy",
            "query": "numpy.orbit", "requirements": [requirement()]} | changes


def diagnostic(kind, **changes):
    return {"id": kind, "kind": "diagnostic", "diagnostic": kind, "library": "numpy",
            "query": "quantum tunneling", "rationale": "An intentionally unsupported request."} | changes


def suite_file(tmp_path, db, cases):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({"schema_version": 1, "suite_id": "seeded-v1",
                                "database_sha256": sha256(db.read_bytes()).hexdigest(), "cases": cases}))
    return path


def cli(db, suite, output, *, injection=None):
    environment = os.environ | {"PYTHONPATH": str(ROOT)}
    if injection is None:
        args = [sys.executable, str(ROOT / "eval/retrieval_scorecard.py")]
    else:
        args = [sys.executable, "-c", injection]
    return subprocess.run(args + ["--db", str(db), "--suite", str(suite), "--output", str(output)],
                          cwd=ROOT, env=environment, capture_output=True, text=True)


def test_complete_evidence_cutoffs_and_unicode_normalization(snapshot, tmp_path):
    case = evidence(requirements=[requirement(quote=" FIRST\nEvidence\tHAS CAFÉ   SHAPES "),
                                  requirement("last", "Final distant evidence")])
    before = snapshot.read_bytes()
    report = scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [case]))
    result = report["cases"][0]
    assert result["direct"]["complete_at"] == {"1": False, "3": False, "5": True}
    assert [group["matches"][0]["rank"] for group in result["direct"]["requirements"]] == [1, 5]
    assert report["summary"]["direct_complete_at"]["5"] == {"numerator": 1, "denominator": 1}
    assert snapshot.read_bytes() == before
    assert report["verification"] == {"database_unchanged": True, "network_blocking_enforced": False}
    assert set(report["fingerprints"]) == {"suite_sha256", "database_sha256", "evaluator_sha256"}


@pytest.mark.parametrize("url,valid", [(PAGE, True), (URL, True), (PAGE + "#other", False),
                                       (PAGE + "-suffix", False), (PAGE.replace("orbit", "orb"), False)])
def test_urls_match_exact_pages_or_fragments(snapshot, tmp_path, url, valid):
    suite = suite_file(tmp_path, snapshot, [evidence(requirements=[requirement(url=url)])])
    if valid:
        assert scorecard.evaluate_suite(snapshot, suite)["cases"][0]["direct"]["complete_at"]["1"]
    else:
        with pytest.raises(scorecard.InvalidRun, match="Missing gold"):
            scorecard.evaluate_suite(snapshot, suite)


def test_alternatives_are_or_but_requirements_are_and(snapshot, tmp_path):
    group = requirement("choice")
    group["alternatives"].append({"url": URL, "quote": "Final distant evidence"})
    case = evidence(requirements=[group, requirement("second", "Second supporting passage")])
    report = scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [case]))
    assert report["cases"][0]["direct"]["complete_at"] == {"1": False, "3": True, "5": True}


def test_every_alternative_is_validated_and_quotes_cannot_cross_sections(snapshot, tmp_path):
    group = requirement()
    group["alternatives"].append({"url": URL, "quote": "Never present anywhere"})
    for requirements in ([group], [requirement(quote="shapes. Second supporting")]):
        with pytest.raises(scorecard.InvalidRun, match="Missing gold"):
            scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [evidence(requirements=requirements)]))


def test_multi_passage_requires_distinct_sections_and_can_be_incomplete(snapshot, tmp_path):
    cases = [evidence(id="cross-library", category="multi_passage", library=None,
                      requirements=[requirement(), requirement("join", "outer join", "https://pandas.pydata.org/docs/user_guide/merging.html#outer")])]
    result = scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, cases))["cases"][0]
    assert not result["direct"]["complete_at"]["5"]
    assert len(result["direct"]["requirements"][0]["matches"]) == 1
    assert result["direct"]["requirements"][1]["matches"] == []
    invalid = evidence(category="multi_passage", requirements=[requirement(), requirement("also first", "Café shapes")])
    with pytest.raises(scorecard.InvalidRun, match="single passage"):
        scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [invalid]))


def test_retries_unconditional_and_reads_deduplicated_globally(snapshot, tmp_path):
    cases = [evidence(retry_query="numpy.orbit"), evidence(id="repeat", retry_query="numpy.orbit")]
    read_ids = [item["doc_id"] for item in catalog.search_docs("numpy.orbit", "numpy", 5, snapshot)]
    chars = sum(len(section["heading"] + section["text"]) for section in (catalog.read_doc(doc_id, snapshot) for doc_id in read_ids))
    report = scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, cases))
    assert report["summary"]["workload"] == {"search_attempts": 4, "unique_section_reads": 5, "characters_read": chars}
    assert report["cases"][1]["workload"] == {"search_attempts": 2, "unique_section_reads": 0, "characters_read": 0}
    for case in report["cases"]:
        assert len(case["attempts"]) == 2
        assert case["after_retry"]["retry_executed"] is True
        assert {match["attempt"] for match in case["after_retry"]["requirements"][0]["matches"]} == {1, 2}


def test_retry_can_complete_cross_library_evidence(snapshot, tmp_path):
    case = evidence(library=None, category="multi_passage", retry_query="outer join", requirements=[
        requirement(), requirement("join", "outer join", "https://pandas.pydata.org/docs/user_guide/merging.html")])
    result = scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [case]))["cases"][0]
    assert not result["direct"]["complete_at"]["5"]
    assert result["after_retry"]["complete"] is True
    assert result["after_retry"]["requirements"][1]["matches"][0]["attempt"] == 2


@pytest.mark.parametrize("change", [
    {"query": "!!!"}, {"query": "x" * 301}, {"query": True}, {"category": "unknown"},
    {"requirements": []}, {"requirements": [requirement(quote="word")]},
    {"requirements": [requirement(url="https://evil.test/docs")]},
    {"requirements": [requirement(), requirement()]}, {"retry_query": None},
    {"library": 3}, {"rationale": "Wrong case fields"}, {"kind": "other"},
    {"category": "multi_passage"},
])
def test_invalid_case_configuration_rejected(snapshot, tmp_path, change):
    with pytest.raises(ValueError):
        scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [evidence(**change)]))


def test_duplicate_ids_and_wrong_library_gold_rejected(snapshot, tmp_path):
    with pytest.raises(ValueError, match="Duplicate case"):
        scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [evidence(), evidence()]))
    with pytest.raises(ValueError):
        scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [evidence(library="pandas")]))


def test_diagnostics_are_inspection_without_positive_scores(snapshot, tmp_path):
    cases = [evidence(), diagnostic("unknown_library", library="does-not-exist"),
             diagnostic("unsupported_topic", query="numpy.orbit"),
             diagnostic("unavailable_version", requested_version="999.0")]
    report = scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, cases))
    assert report["summary"]["positive_cases"] == 1
    assert report["summary"]["diagnostic_cases"] == 3
    assert report["summary"]["direct_complete_at"]["5"] == {"numerator": 1, "denominator": 1}
    assert report["cases"][1]["diagnostic"]["rejected"] is True
    assert report["cases"][1]["diagnostic"]["error_type"] == "ValueError"
    assert report["cases"][2]["diagnostic"]["returned_passages"] == 5
    assert "text" in report["cases"][2]["attempts"][0]["passages"][0]
    assert report["cases"][3]["diagnostic"]["available_versions"] == ["2.0"]
    assert report["cases"][3]["diagnostic"]["version_mismatch"] is True


@pytest.mark.parametrize("case", [diagnostic("unavailable_version", requested_version="2.0"),
                                  diagnostic("unavailable_version"), diagnostic("unknown_library"),
                                  diagnostic("unsupported_topic", requested_version="3.0"),
                                  diagnostic("unsupported_topic", requirements=[]),
                                  diagnostic("invalid")])
def test_invalid_diagnostics_fail_run(snapshot, tmp_path, case):
    with pytest.raises(ValueError):
        scorecard.evaluate_suite(snapshot, suite_file(tmp_path, snapshot, [case]))


def test_snapshot_pin_and_concurrent_changes_invalidate(snapshot, tmp_path, monkeypatch):
    suite = suite_file(tmp_path, snapshot, [evidence()])
    original = catalog.search_docs

    def changed(*args, **kwargs):
        results = original(*args, **kwargs)
        with sqlite3.connect(snapshot) as connection:
            connection.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        return []

    monkeypatch.setattr(catalog, "search_docs", changed)
    with pytest.raises(scorecard.InvalidRun, match="changed during"):
        scorecard.evaluate_suite(snapshot, suite)
    with pytest.raises(scorecard.InvalidRun, match="fingerprint"):
        scorecard.evaluate_suite(snapshot, suite)


def test_suite_changed_during_retrieval_invalidates(snapshot, tmp_path, monkeypatch):
    suite = suite_file(tmp_path, snapshot, [evidence()])
    original = catalog.search_docs

    def changed(*args, **kwargs):
        results = original(*args, **kwargs)
        suite.write_text(suite.read_text() + "\n")
        return results

    monkeypatch.setattr(catalog, "search_docs", changed)
    with pytest.raises(scorecard.InvalidRun, match="Suite changed"):
        scorecard.evaluate_suite(snapshot, suite)


@pytest.mark.parametrize("concurrent_report", [False, True])
def test_failed_report_write_preserves_other_reports(snapshot, tmp_path, monkeypatch, capsys, concurrent_report):
    suite = suite_file(tmp_path, snapshot, [evidence()])
    output = tmp_path / "report.json"
    original = Path.open

    class BrokenWriter:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            self.stream = original(self.path, "x", encoding="utf-8")
            return self

        def write(self, payload):
            self.stream.write(payload[:20])
            if concurrent_report:
                output.unlink(missing_ok=True)
                output.write_text("concurrent saved report")
            raise OSError("report disk full")

        def __exit__(self, *args):
            self.stream.close()

    def opened(path, mode="r", *args, **kwargs):
        return BrokenWriter(path) if mode == "x" else original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    assert scorecard.main(["--db", str(snapshot), "--suite", str(suite), "--output", str(output)]) == 2
    assert "report disk full" in capsys.readouterr().err
    if concurrent_report:
        assert output.exists(), "Concurrent saved report was deleted"
        assert output.read_text() == "concurrent saved report"
    else:
        assert not output.exists()


def test_cli_valid_misses_exit_zero_and_write_fingerprinted_report(snapshot, tmp_path):
    suite = suite_file(tmp_path, snapshot, [evidence(query="quantum tunneling")])
    output = tmp_path / "report.json"
    before = snapshot.read_bytes()
    result = cli(snapshot, suite, output)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["summary"]["direct_complete_at"]["5"] == {"numerator": 0, "denominator": 1}
    assert report["verification"]["network_blocking_enforced"] is True
    assert report["fingerprints"]["suite_sha256"] == sha256(suite.read_bytes()).hexdigest()
    assert report["fingerprints"]["database_sha256"] == sha256(before).hexdigest()
    assert snapshot.read_bytes() == before


def test_cli_invalid_input_error_and_overwrite_protection(snapshot, tmp_path):
    suite = suite_file(tmp_path, snapshot, [evidence(requirements=[requirement(quote="missing evidence")])])
    output = tmp_path / "report.json"
    result = cli(snapshot, suite, output)
    assert result.returncode == 2 and "Missing gold" in result.stderr
    assert not output.exists()
    output.write_text("saved report")
    assert cli(snapshot, suite, output).returncode == 2
    assert output.read_text() == "saved report"
    link = tmp_path / "alias.json"
    link.symlink_to(suite)
    assert cli(snapshot, suite, link).returncode == 2
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "absent.json")
    assert cli(snapshot, suite, dangling).returncode == 2
    assert dangling.is_symlink()
    for target in (snapshot, suite):
        before = target.read_bytes()
        assert cli(snapshot, suite, target).returncode == 2
        assert target.read_bytes() == before


@pytest.mark.parametrize("network", ["socket.getaddrinfo('example.com', 443)",
                                      "socket.socket().connect(('127.0.0.1', 9))",
                                      "socket.getnameinfo(('127.0.0.1', 443), socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)"])
def test_cli_blocks_network_even_when_retrieval_catches_error(snapshot, tmp_path, network):
    suite = suite_file(tmp_path, snapshot, [evidence()])
    output = tmp_path / "report.json"
    injection = (
        "import socket\nfrom eval import retrieval_scorecard as s\n"
        "original = s.catalog.search_docs\n"
        "def search(*args, **kwargs):\n"
        "    try:\n        " + network + "\n"
        "    except Exception:\n        pass\n"
        "    return original(*args, **kwargs)\n"
        "s.catalog.search_docs = search\nraise SystemExit(s.main())\n"
    )
    result = cli(snapshot, suite, output, injection=injection)
    assert result.returncode == 2, result.stderr
    assert "Network" in result.stderr
    assert not output.exists()


def test_cli_unexpected_retrieval_errors_do_not_publish(snapshot, tmp_path):
    suite = suite_file(tmp_path, snapshot, [evidence()])
    output = tmp_path / "report.json"
    injection = (
        "from eval import retrieval_scorecard as s\n"
        "def broken(*args, **kwargs):\n    raise RuntimeError('unexpected retrieval failure')\n"
        "s.catalog.search_docs = broken\nraise SystemExit(s.main())\n"
    )
    result = cli(snapshot, suite, output, injection=injection)
    assert result.returncode == 2 and "unexpected retrieval" in result.stderr
    assert not output.exists()


def test_cli_rejects_wal_snapshot_with_unhashed_committed_changes(snapshot, tmp_path):
    with sqlite3.connect(snapshot) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        suite = suite_file(tmp_path, snapshot, [evidence()])
        before = snapshot.read_bytes()
        writer.execute("UPDATE sections SET text='Changed evidence in WAL' WHERE doc_id=?", (f"{2:024x}",))
        writer.commit()
        assert snapshot.read_bytes() == before
        assert writer.execute("SELECT text FROM sections WHERE doc_id=?", (f"{2:024x}",)).fetchone()[0] == "Changed evidence in WAL"
        wal = Path(str(snapshot) + "-wal")
        wal_before = wal.read_bytes()
        output = tmp_path / "report.json"
        result = cli(snapshot, suite, output)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "WAL" in result.stderr
        assert not output.exists()
        assert snapshot.read_bytes() == before
        assert wal.read_bytes() == wal_before
