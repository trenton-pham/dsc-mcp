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

## Stronger offline retrieval scorecard

The challenge scorecard measures retrieval of reviewed documentation evidence. It is separate from the original 18-question evaluator and context benchmark, whose questions, rubric, and behavior remain unchanged. It does not call a model, download sources, change ranking, or evaluate generated answers.

Run it against the snapshot named by the challenge manifest:

```sh
.venv/bin/python eval/retrieval_scorecard.py --db data/docs.sqlite3 --output eval/results/retrieval-baseline-2026-10-18.json
```

`--db` and `--output` are required. `--suite` selects another manifest; its default is `eval/challenge_questions.json` beside the evaluator. Choose a new output filename for each saved run. Existing reports are never overwritten. Exit status zero means the scorecard is valid, including any retrieval misses. Status two means invalid configuration, incompatible gold evidence, an execution error, a network attempt, or a changed snapshot. Keep using the original evaluator and automated tests as the regression gate.

The context benchmark's `--output` must name a new directory. It claims that directory before starting the benchmark and rejects existing directories, files, and symlinks. Use a fresh path when rerunning commands from historical reports.

The public challenge has 36 cases across all nine libraries. It retains the original 18 questions with stronger labels and adds nine paraphrases, three questions requiring separate passages, three context-disambiguation questions, and three unsupported-request diagnostics. It is an openly inspected challenge set, not an independently held-out estimate of general accuracy. Queries and labels are fixed before running it; retrieval is not tuned to improve this baseline.

Each evidence case lists named requirements. Every requirement must be met, and each can accept alternative passages. A passage must contain a reviewed, multiword quotation after whitespace normalization and case-insensitive comparison. The quotation and its accepted URL must belong to the same section. A URL with a fragment matches that section URL exactly; a fragmentless URL matches the exact base page. Quotes are matched against the section heading and full text, not its search preview. These checks establish that labelled evidence was retrieved; they do not independently prove that the label supports a complete answer.

Before searching, the evaluator checks every declared alternative against the whole read-only index and verifies the manifest's database SHA-256. It rejects SQLite WAL mode, where committed data may reside outside the fingerprinted main file. Use a standalone snapshot produced by manual sync; the evaluator never checkpoints or changes the index. Missing source evidence is an invalid suite, not a search miss. Multi-passage cases require at least two requirements that cannot all be satisfied by a single section. The cross-library case searches without a library filter.

The evaluator reads the first five results in rank order. It reports complete evidence among the first one, three, and five results, along with per-requirement matches so partial support remains visible. If a case includes a scripted keyword retry, that retry always runs, even when the original query succeeds. Direct scores remain separate from cumulative evidence across the original five results and the retry's five results. Retry keywords are authored in the suite; this does not measure an agent discovering a better query.

Unsupported requests are diagnostics, excluded from positive-evidence denominators. An unknown library must trigger the existing catalog rejection. An uncovered topic returns candidate passages for inspection. A requested documentation version absent from the metadata records that mismatch. None measures answer refusal, and missing version coverage does not establish that an API is unavailable.

JSON reports preserve the fixed execution policy, fingerprints, downloaded documentation versions and dates, ranked candidates, matched requirements, and workload counts. Reads are cached by section ID across the suite. Search attempts, actual unique section reads, and characters in newly read headings and bodies exclude corpus preflight work. These counts are not per-question model costs, MCP wire payloads, tokens, latency, or billing. Retrieval runs with network attempts blocked, and the database fingerprint must remain unchanged.

The guard monitors Python socket audit events; it is not an operating-system network sandbox. Reports are fully written in a private temporary directory before atomic publication without replacing an existing destination.

The [initial baseline](eval/results/retrieval-baseline-2026-10-18.json) finds complete labelled evidence for 16 of 33 positive cases in the first result, 20 in the first three, and 23 in the first five. The one scripted retry brings cumulative coverage to 24 of 33. The three diagnostics remain separate. The run records 37 search attempts and 131 unique section reads, with network blocking enforced and database bytes unchanged. These are baseline evidence measurements, not generated-answer accuracy.

The [safety-verified replay](eval/results/retrieval-baseline-2026-10-18-safety-fixes.json) preserves every evidence result and workload count. Its evaluator fingerprint identifies the added WAL rejection. The initial baseline and other saved reports remain unchanged.

After a manual sync, do not update the manifest fingerprint alone. Review every quotation and accepted URL through `dsc_docs`, recheck the unsupported-topic rationale and requested-version metadata, and verify that the multi-passage questions still need separate sections. Update the reviewed labels and snapshot fingerprint together, increment the suite ID, and save a new baseline. Reports from different snapshots or suite revisions are different measurements.
