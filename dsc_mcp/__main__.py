import argparse
import json
from pathlib import Path
import sys

from .catalog import CatalogError, DEFAULT_DB, list_libraries, read_doc, search_docs, sync
from .server import make_server


def main() -> int:
    parser = argparse.ArgumentParser(prog="dsc-mcp")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite snapshot path")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "status", "search", "read", "serve"):
        sub = commands.add_parser(name)
        sub.add_argument("--db", type=Path, default=argparse.SUPPRESS)
        if name == "sync":
            sub.add_argument("libraries", nargs="*")
        elif name == "search":
            sub.add_argument("query")
            sub.add_argument("--library")
            sub.add_argument("--limit", type=int, default=5)
        elif name == "read":
            sub.add_argument("doc_id")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            make_server(args.db).run(transport="stdio")
            return 0
        if args.command == "sync":
            result = sync(args.db, args.libraries)
        elif args.command == "status":
            result = list_libraries(args.db)
        elif args.command == "search":
            result = search_docs(args.query, args.library, args.limit, args.db)
        else:
            result = read_doc(args.doc_id, args.db)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (CatalogError, ValueError, OSError) as exc:
        print(f"dsc-mcp: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
