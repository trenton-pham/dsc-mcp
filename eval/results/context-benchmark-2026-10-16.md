# DSC MCP documentation-context benchmark

Run date: October 16, 2026, America/Los_Angeles.

The MCP server reduced aggregate documentation payload size against full extracted official pages. The result depends strongly on the client reading policy and response representation. The typical question did not save context under the conservative full-envelope count.

## Results

All token counts below are estimates from Unicode character counts divided by four. They are not tokenizer measurements or billed usage. Totals cover one complete 18-question suite, not the five repetitions added together.

| Policy | Baseline estimate | MCP text estimate | Text reduction | MCP full-envelope estimate | Envelope reduction | Evidence checks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Read all top five sections | 170,040 | 111,858 | 34.2% | 160,980 | 5.3% | 18/18 |
| Read first section only | 170,040 | 64,722 | 61.9% | 108,434 | 36.2% | 16/18 |

The median per-question envelope reduction was -69.1% for top five and -5.7% for top one. Negative reduction means more context. The top-five per-question range was -802.2% to 76.9%. The top-one range was -576.4% to 83.6%.

The top-five policy increased envelope size on 13 of 18 questions. Large full manuals dominate aggregate savings. The test does not justify a claim that most questions consume fewer tokens.

## What the two sides did

- No MCP: load the correct full official page from the existing snapshot. The baseline receives oracle page discovery, with no search, network, navigation, or tool-schema cost. Page text is reconstructed in SQLite insertion order from every section on that page, including section headings and repeated examples. URL fragments are removed for grouping. It is extracted article text, not raw HTML or a measured web-browser session.
- MCP: start the actual installed stdio server and discover its tools. For each independent task, count all three tool schemas and call list_libraries, search_docs with limit five, and read_doc for a fixed prefix of either five or one result. No evidence oracle picks a favorable section. Search payloads, irrelevant sections, requests, and repeated snippets remain in the count.
- Both policies use the existing suite and evidence rubric without modifying retrieval. The only scripted keyword retry is the one already configured for the pandas join question. Retry is triggered when the URL-and-keyword rubric fails. The evaluation rubric therefore guides retry decisions, but does not choose which hits to read.
- Text estimate: count tool request JSON, concatenated MCP text blocks, and tool schemas. Baseline counts its synthetic page request and page JSON. This assumes a host exposes text content once.
- Envelope estimate: count serialized MCP response objects, including both text and structuredContent where supplied, plus requests and schemas. Each captured field is counted once. This conservative proxy includes response duplication, but is not evidence that a particular host sends both forms to its model.
- Both counts omit the common question, model reasoning, generated answers, host/system instructions, transport framing, initialization messages, prompt caching, and hidden provider accounting. Server initialization instructions are also excluded. No model calls occur inside the benchmark.

## Evidence and reproducibility

The source snapshot contains 67 pages and 1,692 sections across nine libraries. SHA-256: `cc6021699dc8b7b0fd2d497d29c435db32878c4f3a7367bca804b169f1062e3d`. Its bytes were unchanged after both policies. Network connection and DNS resolution audit events were blocked in the benchmark and server processes. Retrieval ran successfully with the block active.

Each policy ran five times. Baseline and MCP processing order alternated by repetition. All per-question results and character counts were identical across repetitions. There were zero MCP tool errors across 660 top-five calls and 280 top-one calls. These are deterministic repeatability checks, not independent statistical samples of model behavior.

The top-five policy passed 17/18 directly and 18/18 after the configured retry. The top-one policy passed 15/18 directly and 16/18 after retry. Its two misses were the natural-language Plotly scatter and SciPy independent-samples questions. Failure exit status one for top-one is intentional and records incomplete evidence coverage.

The rubric checks the expected source URL and a supporting keyword in a read section. It is a retrieval-evidence check. It does not score answer correctness, completeness, executable code, or hallucinations. The 18 questions are the existing development evaluation set, not held-out questions. Some questions reuse the same page.

The measured size floor is the schema/catalog/search response overhead and full section size. On short API pages, these can exceed the complete page. Reading five sections amplifies the overhead. There is no measured latency or throughput result, so machine load and network timing cannot explain these deterministic counts.

Changing the fixed character-per-token divisor to three or five changes the absolute estimates but leaves every percentage unchanged. That sensitivity does not validate a real tokenizer or remove text-specific tokenization differences.

## Resume wording

Conservative claim that includes response duplication:

> Built an offline MCP documentation server for nine libraries, reducing estimated aggregate documentation payload tokens by 5.3% versus full-page input across 18 curated queries, with 18/18 expected-evidence matches.

Text-content claim, only when the measurement scope can be explained:

> Built an offline MCP documentation server that reduced estimated aggregate documentation-text context by 34% versus full-page input across 18 curated queries spanning nine libraries.

Keep the qualifier "estimated aggregate documentation-text context" with the 34% figure. Do not substitute "total LLM tokens," "API cost," "answer accuracy," or "inference speed." The 36.2% envelope and 61.9% text reductions from top-one require disclosing its 16/18 evidence coverage. They should not be presented as equivalent-quality savings.

## Per-question comparison

Percentages use the conservative envelope count. Negative numbers mean increased context.

| Library | Query | Full-page estimate | Top-five reduction | Top-one reduction | Top-one evidence |
| --- | --- | ---: | ---: | ---: | --- |
| pandas | combine tables and keep unmatched rows | 9,840 | -84.9% | 2.3% | Pass |
| pandas | pandas.DataFrame.merge | 2,013 | -345.0% | -244.0% | Pass |
| numpy | multiply arrays with different shapes | 2,560 | -271.9% | -117.4% | Pass |
| numpy | numpy.reshape | 850 | -802.2% | -576.4% | Pass |
| matplotlib | create several plots in one figure | 4,988 | -77.9% | -10.0% | Pass |
| matplotlib | matplotlib.pyplot.subplots | 3,793 | -183.6% | -65.4% | Pass |
| plotly | make interactive scatter plots from a data frame | 5,326 | -31.1% | 2.3% | Fail |
| plotly | plotly.express.scatter | 4,098 | -186.7% | -130.5% | Pass |
| scikit-learn | avoid data leakage during cross validation | 5,798 | -60.3% | 4.1% | Pass |
| scikit-learn | sklearn.pipeline.Pipeline | 5,648 | -52.1% | -13.5% | Pass |
| scipy | compare means of two independent samples | 2,960 | -255.9% | -91.4% | Fail |
| scipy | scipy.stats.ttest_ind | 2,960 | -203.6% | -147.6% | Pass |
| requests | avoid waiting forever for a server response | 4,538 | -40.2% | -1.3% | Pass |
| requests | requests.Session | 16,465 | 54.7% | 70.8% | Pass |
| beautifulsoup4 | find all links in an HTML page | 31,322 | 76.9% | 81.9% | Pass |
| beautifulsoup4 | BeautifulSoup find_all | 31,322 | 73.7% | 83.6% | Pass |
| python | return list of regex matches in string | 17,778 | 63.5% | 72.6% | Pass |
| python | re.findall | 17,778 | 71.5% | 76.9% | Pass |

## Snapshot provenance

Versions identify downloaded documentation. Installed data science package versions were not checked.

| Library | Documentation version | Example official source from the measured snapshot |
| --- | --- | --- |
| pandas | 3.0.6 | [Merge, join, concatenate and compare](https://pandas.pydata.org/docs/user_guide/merging.html) |
| numpy | 2.5 | [Broadcasting](https://numpy.org/doc/stable/user/basics.broadcasting.html) |
| matplotlib | 3.11.2 | [Quick start guide](https://matplotlib.org/stable/users/explain/quick_start.html) |
| plotly | rolling (version not stated) | [Scatter plots in Python](https://plotly.com/python/line-and-scatter/) |
| scikit-learn | 1.9.1 | [12. Common pitfalls and recommended practices](https://scikit-learn.org/stable/common_pitfalls.html) |
| scipy | 1.18.0 | [ttest_ind](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_ind.html) |
| requests | 2.34.2 | [Quickstart](https://requests.readthedocs.io/en/latest/user/quickstart/) |
| beautifulsoup4 | 4.14.3 | [Beautiful Soup Documentation](https://www.crummy.com/software/BeautifulSoup/bs4/doc/) |
| python | 3.15.0 | [re](https://docs.python.org/3/library/re.html) |

The per-case transcripts preserve each page version and download time. Plotly pages may have different version labels; this table shows the first measured page for each library.

## Rerun and artifacts

```sh
.venv/bin/python eval/benchmark_context.py --db data/docs.sqlite3 --output eval/results/context-2026-10-16 --repeats 5 --read-limit 5
.venv/bin/python eval/benchmark_context.py --db data/docs.sqlite3 --output eval/results/context-top1-2026-10-16 --repeats 5 --read-limit 1
.venv/bin/python -m pytest -q
.venv/bin/python eval/evaluate.py --db data/docs.sqlite3
```

Verification passed all 19 automated tests. The existing real-index evaluator reported 17/18 direct matches and one supported keyword retry.

Raw summaries include all five repetitions and discovered tool schemas. Transcripts preserve one full repetition. The CSV has one row per question and policy.

- [Top-five summary](context-2026-10-16/summary.json)
- [Top-five transcripts](context-2026-10-16/transcripts.json)
- [Top-one summary](context-top1-2026-10-16/summary.json)
- [Top-one transcripts](context-top1-2026-10-16/transcripts.json)
- [Per-question CSV](context-comparison-2026-10-16.csv)

Environment: Python 3.14.7, MCP SDK 2.3.0, SQLite 3.53.4. Base commit `4365d27b813f285f865cfb027b5020e5c9176706`. The added benchmark does not change production retrieval, extraction, ranking, sources, or dependencies.

Explain the Number shaped the decision to report pooled and median reductions and separate the two payload representations. Model the Domain kept the experiment in the existing per-question record shape and two fixed reading policies.

Skipped paid model comparisons, generated-answer scoring, tokenizer installation, and broad browser benchmarking. The remaining risk is treating a curated context-size estimate as measured end-to-end token savings.
