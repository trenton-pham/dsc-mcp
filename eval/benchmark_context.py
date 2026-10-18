"""Compare offline MCP payload size with oracle full-page documentation input."""

import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


ROOT = Path(__file__).resolve().parents[1]


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def baseline_page(case, db):
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM sections WHERE library=? ORDER BY rowid", (case["library"],))
            if case["expected_url"].split("#")[0] in row["url"].split("#")[0]]
    if not rows:
        raise ValueError(f"Missing baseline page for {case['query']}")
    urls = {row["url"].split("#")[0] for row in rows}
    if len(urls) != 1:
        raise ValueError(f"Ambiguous baseline page for {case['query']}")
    return {"url": urls.pop(), "title": rows[0]["title"],
            "versions": sorted({row["version"] for row in rows}),
            "downloaded_at": sorted({row["downloaded_at"] for row in rows}),
            "text": "\n\n".join(row["heading"] + "\n" + row["text"] for row in rows)}


def has_evidence(case, sections):
    return any(case["expected_url"] in section["url"] and
               case["needle"].lower() in (section["heading"] + " " + section["text"]).lower()
               for section in sections)


def payload_chars(messages, text_only=False):
    total = 0
    for message in messages:
        total += len(serialize(message["request"]))
        response = message["response"]
        if text_only:
            total += sum(len(block["text"]) for block in response["content"])
        else:
            total += len(serialize(response))
    return total


async def benchmark(db, repeats, read_limit=5):
    cases = json.loads((ROOT / "eval/questions.json").read_text())
    before = sha256(db.read_bytes()).hexdigest()
    # Block network in both processes; stdio retrieval must remain offline.
    guard = ("import sys\n"
             "def offline(event, args):\n"
             "    if event in ('socket.connect', 'socket.getaddrinfo'):\n"
             "        raise RuntimeError('Network disabled for context benchmark')\n"
             "sys.addaudithook(offline)\n")
    exec(guard)
    server_code = guard + "from dsc_mcp.__main__ import main\nraise SystemExit(main())\n"
    params = StdioServerParameters(command=sys.executable,
        args=["-c", server_code, "serve", "--db", str(db)], cwd=str(ROOT))
    runs = []
    transcripts = []
    async with Client(params) as client:
        discovery = (await client.list_tools()).model_dump(by_alias=True, exclude_none=True)
        assert {tool["name"] for tool in discovery["tools"]} == {
            "list_libraries", "search_docs", "read_doc"}
        schema_chars = len(serialize(discovery))
        for repetition in range(repeats):
            run = []
            for case in cases:
                messages = []

                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    if result.is_error:
                        raise RuntimeError(f"MCP error from {name}: {result.content}")
                    messages.append({"request": {"name": name, "arguments": arguments},
                                     "response": result.model_dump(by_alias=True, exclude_none=True)})
                    if result.structured_content is not None:
                        return result.structured_content
                    assert len(result.content) == 1 and result.content[0].type == "text"
                    return json.loads(result.content[0].text)

                async def retrieve():
                    await call("list_libraries", {})
                    sections = []
                    direct = False
                    retried = False
                    for query in [case["query"]] + ([case["retry_query"]] if case.get("retry_query") else []):
                        results = (await call("search_docs", {
                            "query": query, "library": case["library"], "limit": 5}))["result"]
                        # Read a fixed prefix. The rubric never chooses a favorable section.
                        for result in results[:read_limit]:
                            sections.append(await call("read_doc", {"doc_id": result["doc_id"]}))
                        if has_evidence(case, sections):
                            direct = query == case["query"]
                            retried = not direct
                            break
                    return sections, direct, retried

                if repetition % 2 == 0:
                    page = baseline_page(case, db)
                    sections, direct, retried = await retrieve()
                else:
                    sections, direct, retried = await retrieve()
                    page = baseline_page(case, db)
                assert case["needle"].lower() in page["text"].lower()
                baseline_chars = len(serialize({"request": {"url": page["url"]}, "response": page}))
                envelope_chars = schema_chars + payload_chars(messages)
                text_chars = schema_chars + payload_chars(messages, text_only=True)
                row = {"library": case["library"], "query": case["query"],
                       "baseline_chars": baseline_chars, "mcp_envelope_chars": envelope_chars,
                       "mcp_text_chars": text_chars,
                       "reduction_pct": 100 * (1 - envelope_chars / baseline_chars),
                       "text_reduction_pct": 100 * (1 - text_chars / baseline_chars),
                       "evidence_pass": has_evidence(case, sections), "direct": direct,
                       "retry": retried, "tool_calls": len(messages), "sections_read": len(sections)}
                run.append(row)
                if repetition == 0:
                    transcripts.append({"case": case, "baseline": page, "mcp": messages})
            runs.append(run)
    after = sha256(db.read_bytes()).hexdigest()
    assert before == after, "Benchmark changed the documentation index"
    assert all(run == runs[0] for run in runs), "Payload counts changed across repetitions"
    rows = runs[0]
    totals = {key: sum(row[key] for row in rows)
              for key in ("baseline_chars", "mcp_envelope_chars", "mcp_text_chars")}
    summary = {"cases": len(cases), "libraries": len({case["library"] for case in cases}),
               "read_limit": read_limit, "search_limit": 5,
               "repetitions": repeats, "database_sha256": before, "database_unchanged": before == after,
               "network_blocked": True, "token_estimator": "Unicode characters / 4; not model billing",
               "schema_chars_per_task": schema_chars, "totals": totals,
               "estimated_tokens": {key: value / 4 for key, value in totals.items()},
               "pooled_reduction_pct": 100 * (1 - totals["mcp_envelope_chars"] / totals["baseline_chars"]),
               "text_pooled_reduction_pct": 100 * (1 - totals["mcp_text_chars"] / totals["baseline_chars"]),
               "median_reduction_pct": median(row["reduction_pct"] for row in rows),
               "range_reduction_pct": [min(row["reduction_pct"] for row in rows),
                                        max(row["reduction_pct"] for row in rows)],
               "evidence_passes": sum(row["evidence_pass"] for row in rows),
               "direct_passes": sum(row["direct"] for row in rows),
               "retry_passes": sum(row["retry"] for row in rows), "tool_errors": 0,
               "schema": discovery, "runs": runs}
    return summary, transcripts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--read-limit", type=int, choices=(1, 5), default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    db = args.db.resolve()
    if not db.is_file():
        parser.error("--db must name an existing synced index")
    if args.output.resolve() in (db, db.parent):
        parser.error("--output must be a separate results directory")
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("--output must name a new results directory")
    summary, transcripts = asyncio.run(benchmark(db, args.repeats, args.read_limit))
    for name, value in (("summary.json", summary), ("transcripts.json", transcripts)):
        (args.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(serialize({key: value for key, value in summary.items() if key not in ("schema", "runs")}))
    return 0 if summary["evidence_passes"] == summary["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
