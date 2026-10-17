import argparse
import json
from pathlib import Path
import sys

from dsc_mcp.catalog import CatalogError, search_docs, read_doc


def has_evidence(case: dict, query: str, db: Path) -> bool:
    for result in search_docs(query, case["library"], 5, db):
        if case["expected_url"] in result["url"]:
            section = read_doc(result["doc_id"], db)
            if case["needle"].lower() in (section["heading"] + " " + section["text"]).lower():
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--direct-only", action="store_true", help="Exclude the documented agent keyword retry")
    args = parser.parse_args()
    cases = json.loads((Path(__file__).parent / "questions.json").read_text())
    failed = 0
    direct = 0
    retried = 0
    for case in cases:
        try:
            found = has_evidence(case, case["query"], args.db)
            if found:
                direct += 1
            elif not args.direct_only and case.get("retry_query"):
                found = has_evidence(case, case["retry_query"], args.db)
                if found:
                    retried += 1
                    print(f"RETRY {case['library']}: {case['query']} -> {case['retry_query']}")
            if not found:
                failed += 1
                print(f"FAIL {case['library']}: {case['query']}")
        except CatalogError as exc:
            failed += 1
            print(f"FAIL {case['library']}: {case['query']} ({exc})")
    print(f"{direct}/{len(cases)} direct; {retried} supported after keyword retry; {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
