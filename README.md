# DSC MCP

DSC MCP gives an agent local search and citations for a curated set of official data science documentation. It indexes selected pages for pandas, NumPy, Matplotlib, Plotly, scikit-learn, SciPy, Requests, Beautiful Soup, and the Python standard library. The index is a dated snapshot; `status` reports its source versions and download time. Coverage is curated. If a question lacks evidence, the agent must acknowledge the gap. You can extend coverage by adding approved official pages to the registry and refreshing the index.

## Install and sync

Use Python 3.11 or later. In the project directory:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m dsc_mcp sync --db data/docs.sqlite3
.venv/bin/python -m dsc_mcp status --db data/docs.sqlite3
```

`sync` is the only command that downloads pages. Refresh selected libraries with `sync pandas numpy --db data/docs.sqlite3`. It downloads every selected page before publishing one new SQLite file. If any page fails, the current file stays intact. Another sync cannot run at the same time. The operating system releases the sync lock when the process exits, including after a crash. A full sync also rebuilds an unreadable or outdated index.

This local version supports macOS and Linux. It uses the operating system's file locking through Python's `fcntl` module.

The full pinned environment used for development is in `requirements.lock`. It includes test packages and transitive dependencies. The package itself declares only its three runtime dependencies.

To reproduce that dependency set, install `requirements.lock` before the project:

```sh
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
```

## Search and serve

```sh
.venv/bin/python -m dsc_mcp search "avoid data leakage" --library scikit-learn --db data/docs.sqlite3
.venv/bin/python -m dsc_mcp read DOC_ID --db data/docs.sqlite3
.venv/bin/python -m dsc_mcp serve --db /absolute/path/to/dsc_mcp/data/docs.sqlite3
```

The MCP server uses stdio and exposes exactly `list_libraries`, `search_docs`, and `read_doc`. Register this checkout with Codex:

```sh
codex mcp add dsc_docs -- /Users/trentonpham/Code/dsc_mcp/.venv/bin/python -m dsc_mcp serve --db /Users/trentonpham/Code/dsc_mcp/data/docs.sqlite3
codex mcp get dsc_docs
```

The installed server uses `default_tools_approval_mode = "writes"` in its Codex configuration. Its three tools declare that they are read-only and use a closed local index. This permits those reads while preserving approval prompts for any future write tools.

Adjust those absolute paths if you move the project. Start a new Codex chat in this directory after registration so it discovers the server and reads `AGENTS.md`. That file requires documentation lookup, citations, and an insufficient-evidence response instead of an unofficial fallback. To use the same rule in another project, add the data science instructions from `AGENTS.md` to that project's instructions too.

Search returns section IDs, snippets, official URLs, versions, and download dates. Read a section before citing it. The catalog records the documentation site's version, which can differ from a package installed in your environment.

## How it works

```text
Official HTTPS pages -> main article and anchored sections -> temporary SQLite + FTS5 -> atomic file replacement
                                                               |       |
                                                     local CLI search   MCP stdio tools
```

`sources.py` holds the explicit page list and URL policy. It validates each redirect, bounds response size, and extracts headings, API signatures, tables, and fenced code from the main documentation region. `catalog.py` owns the read-only queries and snapshot publication. `server.py` maps catalog errors to MCP tool errors. There is no crawler, model, code runner, or HTTP server. Retrieval does not use the network.

## Follow one question

Ask Codex, "How do I combine two pandas tables and keep unmatched rows? Use dsc_docs and cite the evidence."

1. Codex is the MCP host. Its MCP client discovers the server's three tools.
2. The agent calls `search_docs` with `library="pandas"`. Word search may miss this paraphrase, so the agent retries with API keywords such as `merge outer`.
3. SQLite searches the downloaded text and returns ranked sections with source links.
4. The agent calls `read_doc` with a returned section ID and checks the actual passage.
5. The agent explains the appropriate join using that evidence and cites the official URL and documentation version.

The server provides evidence. Codex still reasons about the question and writes the answer. This is retrieval into the current conversation, not model training. Your server does not need a language model or an API key.

## Why these boundaries exist

One registry contains all approved hosts, paths, and selected pages. Keeping that policy together makes adding a source an inspectable code change. Redirects pass the same checks as initial requests, and tools never accept arbitrary URLs.

The manual sync command has network access; the retrieval tools only read SQLite. That separation makes offline retrieval testable and prevents a search request from becoming an unrestricted fetch. Stdio connects local programs without a public listening port, accounts, or hosting.

SQLite FTS5 keeps the first version small and inspectable. It matches words rather than meaning, so the agent may need to retry a question with API keywords. Embeddings can wait until a separate evaluation demonstrates a need.

The temporary index and atomic replacement keep incomplete refreshes out of search results. The server opens a fresh read-only connection for each call and sees either the previous or the new snapshot.

The source restriction is enforced in this server. Instructions guide Codex's use of the evidence, but this project cannot guarantee that the surrounding agent ignores its training or other tools. It also does not enforce that your installed data science packages match the downloaded documentation.

Run the focused tests with `.venv/bin/python -m pytest -q`. After syncing, run `.venv/bin/python eval/evaluate.py --db data/docs.sqlite3` to check nine API and nine natural-language questions against real indexed pages. A question passes only when a top-five result cites the expected page and contains the stated evidence. The evaluator reports direct matches separately from an explicitly configured keyword retry. Use `--direct-only` to measure the original wording without retries. A keyword retry is a scripted example of the agent workflow, not semantic search inside this server.

The verified snapshot has 67 pages and 1,692 sections. It supports 17 of the 18 evaluation questions directly. The pandas join question requires its documented keyword retry. A real Codex demonstration independently searched `merge outer`, read the official merge passages, cited pandas documentation version 3.0.6, and correctly reported that PyTorch is not covered. These checks do not prove that every generated answer is correct.

The source registry applies **Model the Domain** by keeping library identity and coverage in one structure. **Boundary Discipline** puts source validation at the downloader. **Prove It Works** requires testing the actual offline tools and Codex connection, rather than relying on successful imports.
