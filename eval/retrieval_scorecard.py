"""Score labelled retrieval evidence against one pinned, offline snapshot."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.parse import quote, urldefrag

from dsc_mcp import catalog, sources


class InvalidRun(ValueError):
    pass


@dataclass(frozen=True)
class Alternative:
    url: str
    quote: str


@dataclass(frozen=True)
class Requirement:
    name: str
    alternatives: tuple[Alternative, ...]


@dataclass(frozen=True)
class EvidenceCase:
    id: str
    category: str
    library: str | None
    query: str
    requirements: tuple[Requirement, ...]
    retry_query: str | None = None
    kind: str = "evidence"


@dataclass(frozen=True)
class DiagnosticCase:
    id: str
    diagnostic: str
    library: str
    query: str
    rationale: str
    requested_version: str | None = None
    kind: str = "diagnostic"


def fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        digest = sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def text_value(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRun(f"{label} must be a nonempty string")
    return value


def shape(value, required: set[str], optional: set[str], label: str):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise InvalidRun(f"Invalid fields in {label}")


def query_value(value) -> str:
    query = text_value(value, "query")
    if len(query) > 300 or not re.search(r"\w", query):
        raise InvalidRun("Query must have searchable words and at most 300 characters")
    return query


def known_library(value) -> str:
    return sources.canonical_library(text_value(value, "library"))


def parse_suite(raw: dict) -> tuple[str, str, list[EvidenceCase | DiagnosticCase]]:
    shape(raw, {"schema_version", "suite_id", "database_sha256", "cases"}, set(), "suite")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise InvalidRun("Unsupported suite schema_version")
    suite_id = text_value(raw["suite_id"], "suite_id")
    db_hash = raw["database_sha256"]
    if not isinstance(db_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", db_hash):
        raise InvalidRun("database_sha256 must be a lowercase SHA-256")
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise InvalidRun("cases must be a nonempty list")
    cases: list[EvidenceCase | DiagnosticCase] = []
    ids = set()
    for item in raw["cases"]:
        if not isinstance(item, dict):
            raise InvalidRun("Every case must be an object")
        case_id = text_value(item.get("id"), "case id")
        if case_id in ids:
            raise InvalidRun(f"Duplicate case id {case_id}")
        ids.add(case_id)
        query = query_value(item.get("query"))
        if item.get("kind") == "evidence":
            shape(item, {"id", "kind", "category", "library", "query", "requirements"}, {"retry_query"}, case_id)
            category = item["category"]
            if category not in ("legacy_api", "legacy_natural", "paraphrase", "multi_passage", "disambiguation"):
                raise InvalidRun(f"Invalid evidence category in {case_id}")
            library = known_library(item["library"]) if item["library"] is not None else None
            groups = item["requirements"]
            if not isinstance(groups, list) or not groups:
                raise InvalidRun(f"{case_id} needs nonempty requirements")
            requirements = []
            names = set()
            for group in groups:
                shape(group, {"name", "alternatives"}, set(), case_id)
                name = text_value(group["name"], "requirement name")
                if name in names:
                    raise InvalidRun(f"Duplicate requirement name in {case_id}")
                names.add(name)
                if not isinstance(group["alternatives"], list) or not group["alternatives"]:
                    raise InvalidRun(f"{case_id}/{name} needs nonempty alternatives")
                alternatives = []
                for alternative in group["alternatives"]:
                    shape(alternative, {"url", "quote"}, set(), case_id)
                    url = text_value(alternative["url"], "evidence URL")
                    quotation = text_value(alternative["quote"], "evidence quote")
                    if len(re.findall(r"\w+", quotation)) < 2:
                        raise InvalidRun(f"{case_id}/{name} needs a multiword quotation")
                    page, _ = urldefrag(url)
                    permitted = False
                    for candidate in ([library] if library else sources.LIBRARIES):
                        try:
                            sources.validate_url(page, candidate)
                            permitted = True
                            break
                        except ValueError:
                            pass
                    if not permitted:
                        raise InvalidRun(f"Non-official evidence URL in {case_id}: {url}")
                    alternatives.append(Alternative(url, quotation))
                requirements.append(Requirement(name, tuple(alternatives)))
            if category == "multi_passage" and len(requirements) < 2:
                raise InvalidRun(f"{case_id} needs multiple requirements")
            retry = query_value(item["retry_query"]) if "retry_query" in item else None
            cases.append(EvidenceCase(case_id, category, library, query, tuple(requirements), retry))
        elif item.get("kind") == "diagnostic":
            shape(item, {"id", "kind", "diagnostic", "library", "query", "rationale"}, {"requested_version"}, case_id)
            diagnostic = item["diagnostic"]
            if diagnostic not in ("unknown_library", "unsupported_topic", "unavailable_version"):
                raise InvalidRun(f"Invalid diagnostic in {case_id}")
            rationale = text_value(item["rationale"], "rationale")
            library = text_value(item["library"], "library")
            if diagnostic == "unknown_library":
                try:
                    sources.canonical_library(library)
                except ValueError:
                    pass
                else:
                    raise InvalidRun(f"{case_id} does not name an unknown library")
            else:
                library = known_library(library)
            version = None
            if diagnostic == "unavailable_version":
                version = text_value(item.get("requested_version"), "requested_version")
            elif "requested_version" in item:
                raise InvalidRun(f"Unexpected requested_version in {case_id}")
            cases.append(DiagnosticCase(case_id, diagnostic, library, query, rationale, version))
        else:
            raise InvalidRun(f"Invalid case kind in {case_id}")
    return suite_id, db_hash, cases


def matches(alternative: Alternative, section: dict) -> bool:
    actual = section["url"] if "#" in alternative.url else urldefrag(section["url"])[0]
    return actual == alternative.url and normalized(alternative.quote) in normalized(section["heading"] + " " + section["text"])


def validate_gold(cases, corpus: list[dict], libraries: list[dict]):
    metadata = {item["library"]: item for item in libraries}
    for case in cases:
        if isinstance(case, DiagnosticCase):
            if case.diagnostic == "unavailable_version" and case.requested_version in metadata[case.library]["documentation_versions"]:
                raise InvalidRun(f"{case.id} requests a documentation version present in the snapshot")
            continue
        sections = [section for section in corpus if case.library is None or section["library"] == case.library]
        for requirement in case.requirements:
            for alternative in requirement.alternatives:
                if not any(matches(alternative, section) for section in sections):
                    raise InvalidRun(f"Missing gold evidence for {case.id}/{requirement.name}: {alternative.url}")
        if case.category == "multi_passage" and any(
                all(any(matches(alt, section) for alt in requirement.alternatives) for requirement in case.requirements)
                for section in sections):
            raise InvalidRun(f"{case.id} can satisfy all requirements with a single passage")


def evidence_coverage(case: EvidenceCase, ranked: list[tuple[int, int, dict]]) -> list[dict]:
    return [{"name": requirement.name,
             "alternatives": [{"url": alt.url, "quote": alt.quote} for alt in requirement.alternatives],
             "matches": [
        {"doc_id": section["doc_id"], "url": section["url"], "attempt": attempt,
         "rank": rank, "alternative": index + 1}
        for attempt, rank, section in ranked
        for index, alternative in enumerate(requirement.alternatives) if matches(alternative, section)
    ]} for requirement in case.requirements]


def evaluate_suite(db: Path, suite_path: Path) -> dict:
    db, suite_path = Path(db), Path(suite_path)
    evaluator_hash = fingerprint(Path(__file__))
    suite_bytes = suite_path.read_bytes()
    suite_id, expected_hash, cases = parse_suite(json.loads(suite_bytes))
    before = fingerprint(db)
    if before != expected_hash:
        raise InvalidRun("Database fingerprint differs from suite; review labels for this snapshot")
    uri = f"file:{quote(str(db.resolve()))}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            raise InvalidRun("WAL snapshots are unsupported; use a standalone index from manual sync and review its fingerprint")
        connection.row_factory = sqlite3.Row
        corpus = [dict(row) for row in connection.execute("SELECT * FROM sections")]
    libraries = catalog.list_libraries(db)
    validate_gold(cases, corpus, libraries)
    cache: dict[str, dict] = {}
    workload = {"search_attempts": 0, "unique_section_reads": 0, "characters_read": 0}
    reports = []

    def retrieve(query, library, attempt, case_workload):
        workload["search_attempts"] += 1
        case_workload["search_attempts"] += 1
        results = catalog.search_docs(query, library, 5, db)
        ranked = []
        passages = []
        for rank, result in enumerate(results, 1):
            doc_id = result["doc_id"]
            if doc_id not in cache:
                cache[doc_id] = catalog.read_doc(doc_id, db)
                size = len(cache[doc_id]["heading"] + cache[doc_id]["text"])
                for counters in (workload, case_workload):
                    counters["unique_section_reads"] += 1
                    counters["characters_read"] += size
            section = cache[doc_id]
            ranked.append((attempt, rank, section))
            passages.append({key: section[key] for key in ("doc_id", "library", "heading", "url", "version", "downloaded_at")}
                            | {"rank": rank, "text": section["text"]})
        return ranked, {"query": query, "passages": passages}

    for case in cases:
        counters = {key: 0 for key in workload}
        report = {"id": case.id, "kind": case.kind, "library": case.library,
                  "query": case.query, "workload": counters}
        if isinstance(case, EvidenceCase):
            direct, first = retrieve(case.query, case.library, 1, counters)
            coverage = evidence_coverage(case, direct)
            report |= {"category": case.category, "attempts": [first], "direct": {
                "requirements": coverage, "complete_at": {
                    str(cutoff): all(any(match["rank"] <= cutoff for match in group["matches"]) for group in coverage)
                    for cutoff in (1, 3, 5)}}}
            combined = direct
            if case.retry_query is not None:
                retry, second = retrieve(case.retry_query, case.library, 2, counters)
                report["attempts"].append(second)
                combined = direct + retry
            cumulative = evidence_coverage(case, combined)
            report["after_retry"] = {"retry_executed": case.retry_query is not None,
                                     "complete": all(group["matches"] for group in cumulative),
                                     "requirements": cumulative}
        elif case.diagnostic == "unknown_library":
            workload["search_attempts"] += 1
            counters["search_attempts"] += 1
            try:
                catalog.search_docs(case.query, case.library, 5, db)
            except ValueError as exc:
                report["diagnostic"] = {"type": case.diagnostic, "rationale": case.rationale,
                                        "rejected": True, "error_type": type(exc).__name__, "message": str(exc)}
            else:
                raise InvalidRun(f"{case.id} unknown library was not rejected")
        else:
            _, attempt = retrieve(case.query, case.library, 1, counters)
            report["attempts"] = [attempt]
            report["diagnostic"] = {"type": case.diagnostic, "rationale": case.rationale,
                                    "returned_passages": len(attempt["passages"]), "abstention_scored": False}
            if case.diagnostic == "unavailable_version":
                metadata = next(item for item in libraries if item["library"] == case.library)
                report["diagnostic"] |= {"requested_version": case.requested_version,
                                         "available_versions": metadata["documentation_versions"],
                                         "version_mismatch": True}
        reports.append(report)
    after = fingerprint(db)
    if after != before:
        raise InvalidRun("Database changed during evaluation")
    if sha256(suite_path.read_bytes()).hexdigest() != sha256(suite_bytes).hexdigest():
        raise InvalidRun("Suite changed during evaluation")
    if fingerprint(Path(__file__)) != evaluator_hash:
        raise InvalidRun("Evaluator changed during evaluation")
    evidence = [report for report in reports if report["kind"] == "evidence"]
    return {"schema_version": 1, "suite_id": suite_id,
            "fingerprints": {"suite_sha256": sha256(suite_bytes).hexdigest(), "database_sha256": before,
                             "evaluator_sha256": evaluator_hash},
            "verification": {"database_unchanged": True, "network_blocking_enforced": False},
            "policy": {"search_limit": 5, "direct_cutoffs": [1, 3, 5], "read_policy": "all results in rank order",
                       "retry_policy": "every authored retry unconditionally", "read_cache": "global by doc_id",
                       "characters": "Unicode heading plus full text, excluding separators and preflight reads",
                       "cumulative_policy": "first five direct plus first five retry; no combined rank cutoff",
                       "diagnostics": "excluded from positive denominators; no answer refusal scoring"},
            "documentation": libraries, "cases": reports,
            "summary": {"positive_cases": len(evidence), "diagnostic_cases": len(reports) - len(evidence),
                        "direct_complete_at": {str(cutoff): {"numerator": sum(report["direct"]["complete_at"][str(cutoff)] for report in evidence),
                                                             "denominator": len(evidence)} for cutoff in (1, 3, 5)},
                        "complete_after_retry": {"numerator": sum(report["after_retry"]["complete"] for report in evidence),
                                                 "denominator": len(evidence)}, "workload": workload}}


@contextmanager
def network_blocker():
    attempts = []
    enabled = True

    def audit(event, args):
        if enabled and event in {"socket.connect", "socket.getaddrinfo", "socket.getnameinfo", "socket.gethostbyname",
                                 "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg"}:
            attempts.append(event)
            raise InvalidRun(f"Network attempt blocked: {event}")

    sys.addaudithook(audit)
    try:
        yield attempts
        if attempts:
            raise InvalidRun("Network was attempted during evaluation")
    finally:
        enabled = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--suite", type=Path, default=Path(__file__).with_name("challenge_questions.json"))
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise InvalidRun("Output already exists; choose a new report path")
        if args.output.resolve() in {args.db.resolve(), args.suite.resolve(), Path(__file__).resolve()}:
            raise InvalidRun("Output must not replace an input")
        with network_blocker() as attempts:
            report = evaluate_suite(args.db, args.suite)
            if attempts:
                raise InvalidRun("Network was attempted during evaluation")
            report["verification"]["network_blocking_enforced"] = True
            payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        with tempfile.TemporaryDirectory(prefix=".retrieval-scorecard-", dir=args.output.parent) as directory:
            staged = Path(directory) / "report.json"
            with staged.open("x", encoding="utf-8") as stream:
                stream.write(payload)
            os.link(staged, args.output)
        print(f"Saved valid retrieval scorecard to {args.output}")
        return 0
    except Exception as exc:
        print(f"Invalid scorecard run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
