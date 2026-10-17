from pathlib import Path

from eval import evaluate


def test_evidence_requires_both_official_target_and_supporting_passage(monkeypatch):
    case = {"library": "pandas", "expected_url": "/merging.html", "needle": "outer"}
    result = {"url": "https://pandas.pydata.org/docs/merging.html", "doc_id": "example"}
    section = {"heading": "Join", "text": "Use outer for the union of keys."}
    monkeypatch.setattr(evaluate, "search_docs", lambda *args: [result])
    monkeypatch.setattr(evaluate, "read_doc", lambda *args: section)
    assert evaluate.has_evidence(case, "merge", Path("unused.db"))
    section["text"] = "This passage does not explain that join."
    assert not evaluate.has_evidence(case, "merge", Path("unused.db"))
    section["text"] = "Use outer for the union of keys."
    result["url"] = "https://pandas.pydata.org/docs/groupby.html"
    assert not evaluate.has_evidence(case, "merge", Path("unused.db"))
