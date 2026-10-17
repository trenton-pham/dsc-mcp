import asyncio
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import socket
import sqlite3
import sys

import httpx
import pytest

from dsc_mcp import catalog, sources
from dsc_mcp.__main__ import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_official_url_policy_and_manual_redirects():
    source = sources.Source("pandas", "https://pandas.pydata.org/docs/user_guide/merging.html")
    for bad in (
        "http://pandas.pydata.org/docs/user_guide/merging.html",
        "https://pandas.pydata.org.evil.test/docs/user_guide/merging.html",
        "https://user@pandas.pydata.org/docs/user_guide/merging.html",
        "https://pandas.pydata.org:443/docs/user_guide/merging.html",
        "https://pandas.pydata.org/docs/%2e%2e/private",
        "https://pandas.pydata.org/docs/user_guide/merging.html?token=x",
        "https://pandas.pydata.org/private",
    ):
        with pytest.raises(ValueError):
            sources.validate_url(bad, "pandas")

    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.test/steal"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="non-official"):
            sources.download(source, client)


def test_content_type_rejected():
    source = sources.LIBRARIES["numpy"].sources[0]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"not html"))) as client:
        with pytest.raises(ValueError, match="Expected HTML"):
            sources.download(source, client)


@pytest.mark.parametrize("content_type", ["text/html ; charset=utf-8", " application/xhtml+xml \t; charset=utf-8"])
def test_content_type_whitespace_preserves_html_bytes(content_type):
    source = sources.LIBRARIES["numpy"].sources[0]
    html = b"<main>\xc3\xa9</main>"
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-type": content_type}, content=html))) as client:
        assert sources.download(source, client) == (source.url, html)


def test_real_layout_excerpts_preserve_signatures_code_and_anchors():
    cases = [
        ("pandas", "https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.merge.html", "pandas_api.html", "pandas.DataFrame.merge"),
        ("python", "https://docs.python.org/3/library/re.html", "python_api.html", "re.findall"),
        ("plotly", "https://plotly.com/python/plotly-express/", "plotly_guide.html", None),
    ]
    for name, url, filename, anchor in cases:
        sections = sources.extract(sources.Source(name, url), url, (FIXTURES / filename).read_bytes())
        assert sections and all(section.text and section.content_hash for section in sections)
        if anchor:
            assert any(section.url.endswith("#" + anchor) for section in sections)
        if name == "pandas":
            merge = next(section for section in sections if section.url.endswith("#pandas.DataFrame.merge"))
            assert "outer: use union of keys" in merge.text
            assert "```" in merge.text
    python = sources.extract(sources.Source("python", cases[1][1]), cases[1][1], (FIXTURES / "python_api.html").read_bytes())
    assert "findall" in python[0].heading and "Return" in python[0].text
    plotly = sources.extract(sources.Source("plotly", cases[2][1]), cases[2][1], (FIXTURES / "plotly_guide.html").read_bytes())
    assert "```" in plotly[0].text
    indented = sources.extract(sources.Source("plotly", cases[2][1]), cases[2][1],
        b'<html><title>Example</title><section class="tutorial-content"><h2 id="demo">Demo</h2><pre>if ready:\n    fig.show()</pre></section></html>')
    assert "\n    fig.show()\n" in indented[0].text


def test_nested_examples_and_prerelease_versions():
    source = sources.Source("numpy", "https://numpy.org/doc/stable/user/basics.broadcasting.html")
    html = b'''<title>NumPy v2.5 Manual</title><main><h1 id="broadcasting">Broadcasting</h1>
    <ul><li><p>Multiply arrays.</p><pre>if ready:\n    result = a * b</pre></li></ul></main>'''
    sections = sources.extract(source, source.url, html)
    assert "Multiply arrays." in sections[0].text
    assert "```\nif ready:\n    result = a * b\n```" in sections[0].text
    with pytest.raises(ValueError, match="Development"):
        sources.extract(source, source.url, html.replace(b"v2.5", b"v2.6.0rc1"))


def test_display_equations_are_retained_in_order():
    source = sources.LIBRARIES["scipy"].sources[0]
    html = br'''<title>SciPy v1.18 Manual</title><main><h1 id="formula">Correlation</h1>
    <p>The coefficient is:</p><div class="math notranslate nohighlight">\[r = \frac{x}{y}\]</div>
    <p>where x is the numerator.</p></main>'''
    text = sources.extract(source, source.url, html)[0].text
    assert text.index("coefficient") < text.index(r"\frac{x}{y}") < text.index("numerator")


def _section(library, text, heading, url):
    return sources.Section(sha256((library + heading).encode()).hexdigest()[:24], library, heading, heading,
                           text, url, "1.0", "2026-10-16T00:00:00+00:00", sha256(text.encode()).hexdigest())


def test_atomic_multi_library_failure_keeps_old_snapshot(tmp_path, monkeypatch):
    db = tmp_path / "docs.sqlite3"
    for name in ("pandas", "numpy"):
        spec = catalog.LIBRARIES[name]
        monkeypatch.setitem(catalog.LIBRARIES, name, sources.Library(name, spec.host, spec.prefixes, spec.paths[:1], spec.coverage))
    old = _section("pandas", "merge tables", "merge", "https://pandas.pydata.org/docs/user_guide/merging.html")
    new = _section("pandas", "new content", "new", old.url)
    monkeypatch.setattr(catalog, "download", lambda source: (source.url, b"html"))
    monkeypatch.setattr(catalog, "extract", lambda source, url, html: [old])
    catalog.sync(db, ["pandas"])
    before = db.read_bytes()

    def extract(source, url, html):
        if source.library == "numpy":
            raise ValueError("source failed")
        return [new]

    monkeypatch.setattr(catalog, "extract", extract)
    with pytest.raises(catalog.CatalogError, match="Existing index retained"):
        catalog.sync(db, ["pandas", "numpy"])
    assert db.read_bytes() == before
    assert catalog.read_doc(old.doc_id, db)["text"] == "merge tables"
    with pytest.raises(catalog.CatalogError, match="Existing index retained"):
        catalog.sync(db, ["pandas", "numpy"])


def test_retrieval_is_local_literal_and_validated(tmp_path, monkeypatch):
    db = tmp_path / "docs.sqlite3"
    for name in ("pandas", "numpy"):
        spec = catalog.LIBRARIES[name]
        monkeypatch.setitem(catalog.LIBRARIES, name, sources.Library(name, spec.host, spec.prefixes, spec.paths[:1], spec.coverage))
    docs = {
        "pandas": _section("pandas", "Merge combines tables and keeps unmatched rows with an outer join.", "DataFrame.merge", "https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.merge.html#pandas.DataFrame.merge"),
        "numpy": _section("numpy", "Broadcasting permits array arithmetic across different shapes.", "Broadcasting", "https://numpy.org/doc/stable/user/basics.broadcasting.html#broadcasting"),
    }
    monkeypatch.setattr(catalog, "download", lambda source: (source.url, b"html"))
    monkeypatch.setattr(catalog, "extract", lambda source, url, html: [docs[source.library]])
    catalog.sync(db, ["pandas", "numpy"])

    def no_network(*args, **kwargs):
        raise AssertionError("Retrieval attempted network access")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    assert catalog.search_docs("combine tables keep unmatched rows", "pandas", db=db)[0]["doc_id"] == docs["pandas"].doc_id
    assert catalog.search_docs('" OR *', db=db) == []
    assert catalog.search_docs("array shapes", "numpy", db=db)[0]["doc_id"] == docs["numpy"].doc_id
    for limit in (True, 1.5, 0, 11):
        with pytest.raises(catalog.CatalogError):
            catalog.search_docs("array", limit=limit, db=db)
    with pytest.raises(ValueError, match="Unknown library"):
        catalog.search_docs("array", "evil", db=db)
    with pytest.raises(catalog.CatalogError, match="absent"):
        catalog.read_doc("a" * 24, db)
    assert catalog.list_libraries(db)[0]["pages"] == 1


def test_refresh_preserves_unselected_libraries_and_replaces_selected(tmp_path, monkeypatch):
    db = tmp_path / "docs.sqlite3"
    for name in ("pandas", "numpy"):
        spec = catalog.LIBRARIES[name]
        monkeypatch.setitem(catalog.LIBRARIES, name, sources.Library(name, spec.host, spec.prefixes, spec.paths[:1], spec.coverage))
    docs = {name: _section(name, name + " old", "old", catalog.LIBRARIES[name].sources[0].url)
            for name in ("pandas", "numpy")}
    monkeypatch.setattr(catalog, "download", lambda source: (source.url, b"html"))
    monkeypatch.setattr(catalog, "extract", lambda source, url, html: [docs[source.library]])
    catalog.sync(db, ["pandas", "numpy"])
    old = docs["pandas"]
    docs["pandas"] = _section("pandas", "new merge evidence", "new", old.url)
    catalog.sync(db, ["pandas"])
    assert catalog.read_doc(docs["numpy"].doc_id, db)["text"] == "numpy old"
    assert catalog.read_doc(docs["pandas"].doc_id, db)["text"] == "new merge evidence"
    with pytest.raises(catalog.CatalogError, match="absent"):
        catalog.read_doc(old.doc_id, db)
    assert catalog.search_docs("old", "pandas", db=db) == []


def test_sync_lock_recovers_and_full_sync_rebuilds_corrupt_index(tmp_path, monkeypatch):
    db = tmp_path / "docs.sqlite3"
    lock = db.with_name(db.name + ".lock")
    lock.write_text("leftover from an exited process")
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(catalog.CatalogError, match="Another sync"):
            catalog.sync(db, ["pandas"])
    db.write_bytes(b"invalid sqlite database")
    monkeypatch.setattr(catalog, "download", lambda source: (source.url, b"html"))
    monkeypatch.setattr(catalog, "extract", lambda source, url, html: [
        _section(source.library, "official evidence", source.url, source.url)])
    catalog.sync(db)
    assert all(row["status"] == "curated" for row in catalog.list_libraries(db))


def test_missing_index_lists_libraries(tmp_path):
    assert all(row["status"] == "missing" for row in catalog.list_libraries(tmp_path / "absent.db"))
    with pytest.raises(catalog.CatalogError, match="Run"):
        catalog.search_docs("merge", db=tmp_path / "absent.db")


def test_missing_fts_reports_full_rebuild_in_catalog_and_cli(tmp_path, monkeypatch, capsys):
    db = tmp_path / "docs.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(catalog.SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(catalog.SCHEMA_VERSION),))
        conn.execute("DROP TABLE sections_fts")
    with pytest.raises(catalog.CatalogError, match="full sync.*rebuild"):
        catalog.search_docs("merge", db=db)
    monkeypatch.setattr(sys, "argv", ["dsc-mcp", "search", "merge", "--db", str(db)])
    assert main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "full sync" in output.err and f"sync --db {db}" in output.err
    assert "Traceback" not in output.err


def test_exact_api_lookup_prefers_signature_before_split_tail(tmp_path, monkeypatch):
    db = tmp_path / "docs.sqlite3"
    spec = catalog.LIBRARIES["plotly"]
    monkeypatch.setitem(catalog.LIBRARIES, "plotly", sources.Library("plotly", spec.host, spec.prefixes, spec.paths[:1], spec.coverage))
    url = "https://plotly.com/python-api-reference/generated/plotly.express.scatter.html#plotly.express.scatter"
    first = _section("plotly", "plotly.express.scatter(data_frame, x, y) returns a figure.", "plotly.express.scatter", url)
    tail = _section("plotly", "scatter", "plotly.express.scatter (part 2)", url)
    guide = _section("plotly", "plotly.express.scatter scatter scatter", "Scatter with Plotly Express", "https://plotly.com/python/line-and-scatter/")
    monkeypatch.setattr(catalog, "download", lambda source: (source.url, b"html"))
    monkeypatch.setattr(catalog, "extract", lambda source, url, html: [first, tail, guide])
    catalog.sync(db, ["plotly"])
    result = catalog.search_docs("plotly.express.scatter", "plotly", db=db)[0]
    assert result["doc_id"] == first.doc_id


def test_stdio_exposes_only_three_tools_and_errors(tmp_path):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    async def check():
        params = StdioServerParameters(command=sys.executable,
                                      args=["-m", "dsc_mcp", "serve", "--db", str(tmp_path / "missing.db")],
                                      cwd=str(Path(__file__).resolve().parents[1]))
        async with Client(params) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {"list_libraries", "search_docs", "read_doc"}
            assert all(tool.annotations.read_only_hint and not tool.annotations.open_world_hint for tool in tools.tools)
            listed = await client.call_tool("list_libraries")
            assert not listed.is_error
            missing = await client.call_tool("search_docs", {"query": "merge"})
            assert missing.is_error and "missing" in str(missing.content).lower()
            bad_limit = await client.call_tool("search_docs", {"query": "merge", "limit": True})
            assert bad_limit.is_error
    asyncio.run(check())


def test_stdio_search_and_read_seeded_snapshot(tmp_path, monkeypatch):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    db = tmp_path / "docs.sqlite3"
    spec = catalog.LIBRARIES["numpy"]
    monkeypatch.setitem(catalog.LIBRARIES, "numpy", sources.Library("numpy", spec.host, spec.prefixes, spec.paths[:1], spec.coverage))
    section = _section("numpy", "Broadcasting allows arrays with different shapes.", "Broadcasting", "https://numpy.org/doc/stable/user/basics.broadcasting.html#broadcasting")
    monkeypatch.setattr(catalog, "download", lambda source: (source.url, b"html"))
    monkeypatch.setattr(catalog, "extract", lambda source, url, html: [section])
    catalog.sync(db, ["numpy"])
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\n"
        "def block_network(event, args):\n"
        "    if event in ('socket.connect', 'socket.getaddrinfo'):\n"
        "        raise RuntimeError('Network disabled for offline MCP test')\n"
        "sys.addaudithook(block_network)\n")

    async def check():
        params = StdioServerParameters(command=sys.executable,
                                      args=["-m", "dsc_mcp", "serve", "--db", str(db)],
                                      env={"PYTHONPATH": str(tmp_path) + os.pathsep + str(Path(__file__).resolve().parents[1])},
                                      cwd=str(Path(__file__).resolve().parents[1]))
        async with Client(params) as client:
            result = await client.call_tool("search_docs", {"query": "different shapes", "library": "numpy"})
            assert not result.is_error and section.doc_id in str(result.content)
            full = await client.call_tool("read_doc", {"doc_id": section.doc_id})
            assert not full.is_error and section.url in str(full.content)
    asyncio.run(check())
