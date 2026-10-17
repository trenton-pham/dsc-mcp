from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
import time
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
import httpx


@dataclass(frozen=True)
class Source:
    library: str
    url: str


@dataclass(frozen=True)
class Library:
    name: str
    host: str
    prefixes: tuple[str, ...]
    paths: tuple[str, ...]
    coverage: str

    @property
    def sources(self) -> tuple[Source, ...]:
        return tuple(Source(self.name, f"https://{self.host}{path}") for path in self.paths)


LIBRARIES: dict[str, Library] = {
    "pandas": Library("pandas", "pandas.pydata.org", ("/docs/",), (
        "/docs/user_guide/indexing.html", "/docs/user_guide/merging.html", "/docs/user_guide/groupby.html",
        "/docs/user_guide/missing_data.html", "/docs/user_guide/io.html", "/docs/user_guide/10min.html",
        "/docs/reference/api/pandas.DataFrame.merge.html", "/docs/reference/api/pandas.DataFrame.groupby.html",
        "/docs/reference/api/pandas.read_csv.html", "/docs/reference/api/pandas.DataFrame.fillna.html",
    ), "Selected user guide and DataFrame/IO API pages"),
    "numpy": Library("numpy", "numpy.org", ("/doc/stable/",), (
        "/doc/stable/user/absolute_beginners.html", "/doc/stable/user/basics.broadcasting.html",
        "/doc/stable/user/basics.indexing.html", "/doc/stable/reference/arrays.ndarray.html",
        "/doc/stable/reference/random/index.html", "/doc/stable/reference/generated/numpy.reshape.html",
        "/doc/stable/reference/generated/numpy.asarray.html", "/doc/stable/reference/random/generator.html",
    ), "Selected array, indexing, broadcasting, and random pages"),
    "matplotlib": Library("matplotlib", "matplotlib.org", ("/stable/",), (
        "/stable/users/explain/quick_start.html", "/stable/users/explain/axes/axes_intro.html",
        "/stable/users/explain/figure/figure_intro.html", "/stable/users/explain/axes/legend_guide.html",
        "/stable/api/_as_gen/matplotlib.pyplot.subplots.html", "/stable/api/_as_gen/matplotlib.axes.Axes.plot.html",
        "/stable/api/_as_gen/matplotlib.axes.Axes.scatter.html", "/stable/api/_as_gen/matplotlib.pyplot.savefig.html",
    ), "Selected plotting guides and pyplot/Axes APIs"),
    "plotly": Library("plotly", "plotly.com", ("/python/", "/python-api-reference/"), (
        "/python/plotly-express/", "/python/line-charts/", "/python/line-and-scatter/",
        "/python/bar-charts/", "/python/subplots/", "/python/creating-and-updating-figures/",
        "/python-api-reference/generated/plotly.express.scatter.html",
        "/python-api-reference/generated/plotly.express.line.html",
        "/python-api-reference/generated/plotly.express.bar.html",
    ), "Selected Plotly Express tutorials and function APIs"),
    "scikit-learn": Library("scikit-learn", "scikit-learn.org", ("/stable/",), (
        "/stable/common_pitfalls.html", "/stable/modules/preprocessing.html", "/stable/modules/cross_validation.html",
        "/stable/modules/model_evaluation.html", "/stable/modules/compose.html", "/stable/supervised_learning.html",
        "/stable/modules/generated/sklearn.pipeline.Pipeline.html",
        "/stable/modules/generated/sklearn.model_selection.cross_val_score.html",
        "/stable/modules/generated/sklearn.preprocessing.StandardScaler.html",
        "/stable/modules/generated/sklearn.metrics.classification_report.html",
    ), "Selected preprocessing, pipeline, validation, metrics, and model pages"),
    "scipy": Library("scipy", "docs.scipy.org", ("/doc/scipy/",), (
        "/doc/scipy/tutorial/stats.html", "/doc/scipy/tutorial/optimize.html",
        "/doc/scipy/reference/stats.html", "/doc/scipy/reference/optimize.html",
        "/doc/scipy/reference/generated/scipy.stats.ttest_ind.html",
        "/doc/scipy/reference/generated/scipy.stats.pearsonr.html",
        "/doc/scipy/reference/generated/scipy.optimize.minimize.html",
    ), "Selected statistics and optimization tutorials/APIs"),
    "requests": Library("requests", "requests.readthedocs.io", ("/en/latest/",), (
        "/en/latest/user/quickstart/", "/en/latest/user/advanced/", "/en/latest/user/authentication/",
        "/en/latest/community/faq/", "/en/latest/api/",
    ), "Selected Requests usage and developer API pages"),
    "beautifulsoup4": Library("beautifulsoup4", "www.crummy.com", ("/software/BeautifulSoup/bs4/doc/",), (
        "/software/BeautifulSoup/bs4/doc/",
    ), "The official Beautiful Soup manual, split into anchored sections"),
    "python": Library("python", "docs.python.org", ("/3/library/",), (
        "/3/library/re.html", "/3/library/collections.html", "/3/library/csv.html",
        "/3/library/json.html", "/3/library/datetime.html", "/3/library/pathlib.html",
        "/3/library/statistics.html", "/3/library/itertools.html", "/3/library/functools.html",
    ), "Selected standard-library modules used in data work"),
}
ALIASES = {"sklearn": "scikit-learn", "bs4": "beautifulsoup4", "beautiful-soup": "beautifulsoup4"}


@dataclass(frozen=True)
class Section:
    doc_id: str
    library: str
    title: str
    heading: str
    text: str
    url: str
    version: str
    downloaded_at: str
    content_hash: str


def canonical_library(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Library must be a name. Use list_libraries to see supported libraries.")
    key = ALIASES.get(name.strip().lower(), name.strip().lower())
    if key not in LIBRARIES:
        raise ValueError(f"Unknown library {name!r}. Use list_libraries to see supported libraries.")
    return key


def validate_url(url: str, library: str) -> str:
    """Accept only exact official HTTPS hosts and allowed path roots."""
    spec = LIBRARIES[canonical_library(library)]
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != spec.host or parts.port is not None or parts.username or parts.password:
        raise ValueError(f"Rejected non-official URL for {library}: {url}")
    if parts.query or parts.fragment or not parts.path.startswith("/") or "\\" in parts.path:
        raise ValueError(f"Rejected URL parameters or path for {library}: {url}")
    decoded = unquote(parts.path)
    if decoded != parts.path or any(segment in (".", "..") for segment in decoded.split("/")) or "//" in decoded:
        raise ValueError(f"Rejected encoded or traversing URL for {library}: {url}")
    if not any(decoded.startswith(prefix) for prefix in spec.prefixes):
        raise ValueError(f"Rejected path outside official docs for {library}: {url}")
    return url


def download(source: Source, client: httpx.Client | None = None) -> tuple[str, bytes]:
    """Stream at most 32 MiB and inspect every redirect before following it."""
    url = validate_url(source.url, source.library)
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False,
                                    headers={"User-Agent": "dsc-mcp/0.1 (+local documentation index)"})
    try:
        for attempt in range(3):
            current = url
            try:
                for _ in range(6):
                    with client.stream("GET", current, follow_redirects=False) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError(f"Redirect without Location: {current}")
                            current = validate_url(urljoin(current, location), source.library)
                            continue
                        if response.status_code in (429, 500, 502, 503, 504):
                            raise httpx.HTTPStatusError(f"HTTP {response.status_code} for {current}", request=response.request, response=response)
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if content_type and content_type not in ("text/html", "application/xhtml+xml"):
                            raise ValueError(f"Expected HTML documentation at {current}, got {content_type}")
                        chunks: list[bytes] = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 32 * 1024 * 1024:
                                raise ValueError(f"Documentation page exceeds 32 MiB: {current}")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        if not body:
                            raise ValueError(f"Empty documentation page: {current}")
                        return current, body
                raise ValueError(f"Too many redirects: {url}")
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")
    finally:
        if owned:
            client.close()


def _version(title: str, library: str) -> str:
    match = re.search(r"(?:Beautiful Soup|Requests|Matplotlib|scikit-learn|SciPy|NumPy|pandas|Python|documentation)\s+(?:v)?(\d+\.\d+(?:\.\d+)?)", title, re.I)
    if match:
        return match.group(1)
    if library == "plotly":
        match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\s+documentation", title)
        if match:
            return match.group(1)
    return "rolling (version not stated)"


def extract(source: Source, url: str, html: bytes, downloaded_at: str | None = None) -> list[Section]:
    validate_url(url, source.library)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True).split(" — ")[0] if soup.title else url
    full_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    version = _version(full_title, source.library)
    if re.search(r"\b(dev|development|alpha|beta|rc\d*)\b|\d+\.\d+(?:\.\d+)?[.-]?(?:dev|a|b|rc)\d*", full_title, re.I):
        raise ValueError(f"Development documentation rejected: {url}")
    selectors = ("section.tutorial-content",) if source.library == "plotly" and "/python/" in url else ()
    selectors += ("article[role=main]", "article.bd-article", "div.body[role=main]", "[role=main]", "main")
    root = next((node for selector in selectors if (node := soup.select_one(selector))), None)
    if root is None:
        raise ValueError(f"No documentation main region found: {url}")
    for node in root.select("script, style, nav, aside, footer, form, .output_area, .sphx-glr-download, .admonition.advertisement, .headerlink"):
        node.decompose()
    observed = downloaded_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    sections: list[Section] = []
    heading = title
    anchor = ""
    blocks: list[str] = []
    part = 1

    def flush() -> None:
        nonlocal blocks, part
        body = "\n\n".join(blocks).strip()
        if not body:
            return
        content_hash = sha256(body.encode()).hexdigest()
        section_url = f"{url}#{anchor}" if anchor else url
        doc_id = sha256(f"{source.library}\0{section_url}\0{heading}\0{len(sections)}\0{content_hash}".encode()).hexdigest()[:24]
        sections.append(Section(doc_id, source.library, title, heading if part == 1 else f"{heading} (part {part})", body,
                                section_url, version, observed, content_hash))
        blocks = []
        part += 1

    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "dt", "p", "pre", "li", "table", "div"]):
        if node.name == "div" and "math" not in node.get("class", []):
            continue
        if any(parent.name in ("pre", "table", "dt") or "math" in parent.get("class", []) or
               (parent.name == "li" and not parent.find(["p", "pre", "table", "dt"]))
               for parent in node.parents if parent is not root):
            continue
        if node.name == "li" and node.find(["p", "pre", "table", "dt"]):
            continue
        if node.name in ("h1", "h2", "h3", "h4", "h5", "h6") or (node.name == "dt" and node.get("id")):
            flush()
            heading = node.get_text(" ", strip=True)
            anchor = node.get("id") or (node.parent.get("id") if isinstance(node.parent, Tag) else "") or ""
            part = 1
            if node.name == "dt":
                blocks.append(heading)
            continue
        if node.name == "pre":
            raw = node.get_text().strip("\n")
            value = f"```\n{raw}\n```" if raw else ""
        elif node.name == "table":
            value = "\n".join(" | ".join(cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])) for row in node.find_all("tr"))
        else:
            value = node.get_text(" ", strip=True)
        if value:
            if sum(map(len, blocks)) + len(value) > 12000 and blocks:
                flush()
            blocks.append(value)
    flush()
    if not sections:
        raise ValueError(f"No readable documentation sections found: {url}")
    return sections
