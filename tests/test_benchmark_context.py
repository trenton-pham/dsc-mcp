import json
import sqlite3
import sys

import pytest

from eval import benchmark_context

from eval.benchmark_context import baseline_page, has_evidence, payload_chars, serialize


def test_baseline_reconstructs_whole_page_including_anchored_target(tmp_path):
    db = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sections (library, url, title, heading, text, version, downloaded_at)")
        conn.executemany("INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?)", [
            ("numpy", "https://numpy.org/page#first", "Page", "First", "Some context", "1.0", "date"),
            ("numpy", "https://numpy.org/page#target", "Page", "Target", "Evidence", "1.0", "date"),
            ("numpy", "https://numpy.org/other", "Other", "Other", "Exclude me", "1.0", "date"),
        ])
    page = baseline_page({"library": "numpy", "query": "example", "expected_url": "/page#target"}, db)
    assert page["url"] == "https://numpy.org/page"
    assert page["text"] == "First\nSome context\n\nTarget\nEvidence"


def test_counts_requests_and_duplicate_wire_fields_without_double_parsing():
    section = {"url": "https://numpy.org/page#target", "heading": "Target", "text": "Evidence"}
    messages = [{"request": {"name": "read_doc"}, "response": {
        "content": [{"type": "text", "text": serialize(section)}], "structuredContent": section}}]
    assert payload_chars(messages) == len(serialize(messages[0]["request"])) + len(serialize(messages[0]["response"]))
    assert payload_chars(messages, text_only=True) == len(serialize(messages[0]["request"])) + len(serialize(section))
    case = {"expected_url": "/page#target", "needle": "evidence"}
    assert has_evidence(case, [section])
    assert not has_evidence({**case, "expected_url": "/other"}, [section])
    assert not has_evidence({**case, "needle": "missing"}, [section])


@pytest.mark.parametrize("existing", ["directory", "file", "symlink", "dangling_symlink"])
def test_cli_rejects_existing_output_before_benchmark(tmp_path, monkeypatch, capsys, existing):
    db = tmp_path / "docs.sqlite3"
    db.write_bytes(b"saved index")
    output = tmp_path / "results"
    if existing == "directory":
        output.mkdir()
        (output / "summary.json").symlink_to(db)
    elif existing == "file":
        output.write_text("saved report")
    elif existing == "symlink":
        target = tmp_path / "saved-results"
        target.mkdir()
        (target / "summary.json").symlink_to(db)
        output.symlink_to(target, target_is_directory=True)
    else:
        output.symlink_to(tmp_path / "absent", target_is_directory=True)
    invoked = []

    async def benchmark(*args):
        invoked.append(True)
        return {"evidence_passes": 1, "cases": 1}, []

    monkeypatch.setattr(benchmark_context, "benchmark", benchmark)
    monkeypatch.setattr(sys, "argv", ["benchmark_context", "--db", str(db), "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        benchmark_context.main()
    assert error.value.code == 2
    assert "new results directory" in capsys.readouterr().err
    assert not invoked
    assert db.read_bytes() == b"saved index"
    if existing == "file":
        assert output.read_text() == "saved report"
    elif existing == "dangling_symlink":
        assert output.is_symlink()
        assert not output.exists()
    else:
        assert (output / "summary.json").is_symlink()


def test_cli_writes_reports_to_new_output_directory(tmp_path, monkeypatch):
    db = tmp_path / "docs.sqlite3"
    db.write_bytes(b"saved index")
    output = tmp_path / "results"
    summary = {"evidence_passes": 1, "cases": 1}

    async def benchmark(*args):
        assert output.is_dir()
        return summary, []

    monkeypatch.setattr(benchmark_context, "benchmark", benchmark)
    monkeypatch.setattr(sys, "argv", ["benchmark_context", "--db", str(db), "--output", str(output)])
    assert benchmark_context.main() == 0
    assert json.loads((output / "summary.json").read_text()) == summary
    assert json.loads((output / "transcripts.json").read_text()) == []
    assert db.read_bytes() == b"saved index"
