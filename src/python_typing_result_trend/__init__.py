"""Build an interactive trend chart of Python typing conformance test results.

Data source: the git history of ``conformance/results/results.html`` in the
`python/typing <https://github.com/python/typing>`_ repository.

For every commit touching that file we download the file, extract each type
checker's version and pass rate, and keep one data point per
``(checker, date, version)`` — the latest commit of that day. The data points
are saved to ``trend.csv`` and rendered into a self-contained, interactive
``index.html`` (Plotly.js via CDN) that can be served with GitHub Pages.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

REPO = "python/typing"
RESULTS_PATH = "conformance/results/results.html"
API_URL = f"https://api.github.com/repos/{REPO}/commits"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}"

DATA_DIR = Path("data")
HTML_DIR = DATA_DIR / "html"
COMMITS_JSON = DATA_DIR / "commits.json"
CSV_PATH = Path("trend.csv")
HTML_PATH = Path("index.html")

PASS_CLASSES = {"conformant"}
PARTIAL_CLASSES = {"partially-conformant"}
FAIL_CLASSES = {"not-conformant", "nonconformant"}
SCORE_CLASSES = PASS_CLASSES | PARTIAL_CLASSES | FAIL_CLASSES


@dataclass(frozen=True)
class Record:
    when: datetime
    checker: str
    version: str
    pass_rate: float
    sha: str

    @property
    def day(self) -> str:
        return self.when.date().isoformat()


def fetch_commits(session: requests.Session) -> list[dict]:
    """Return every commit touching results.html, newest first.

    Cached in data/commits.json; on reruns only commits newer than the cache
    are fetched (the HTML cache in data/html/ is then safe to reuse in CI).
    """
    cached: list[dict] = json.loads(COMMITS_JSON.read_text(encoding="utf-8")) if COMMITS_JSON.exists() else []
    known_shas = {c["sha"] for c in cached}
    fresh: list[dict] = []
    page = 1
    while True:
        resp = session.get(
            API_URL,
            params={"path": RESULTS_PATH, "per_page": 100, "page": page},
            headers={"Accept": "application/vnd.github+json"},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        done = False
        for commit in batch:
            if commit["sha"] in known_shas:
                done = True  # everything older is already cached
                break
            fresh.append(commit)
        if done or len(batch) < 100:
            break
        page += 1
    if not fresh:
        return cached
    commits = fresh + cached
    DATA_DIR.mkdir(exist_ok=True)
    COMMITS_JSON.write_text(json.dumps(commits), encoding="utf-8")
    print(f"found {len(fresh)} new commits ({len(commits)} total)")
    return commits


def download_html(session: requests.Session, sha: str) -> str:
    """Download results.html at the given commit (cached under data/html/)."""
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    cache = HTML_DIR / f"{sha}.html"
    if not cache.exists():
        resp = session.get(f"{RAW_URL}/{sha}/{RESULTS_PATH}", timeout=30)
        resp.raise_for_status()
        cache.write_text(resp.text, encoding="utf-8")
        time.sleep(0.1)  # be polite to raw.githubusercontent.com
    return cache.read_text(encoding="utf-8")


def _score_cells(cells: list) -> float | None:
    """pass rate = (pass + 0.5 * partial) / total, the convention used by the suite."""
    passed = partial = failed = 0
    for cell in cells:
        classes = set(cell.get("class", []))
        if classes & PASS_CLASSES:
            passed += 1
        elif classes & PARTIAL_CLASSES:
            partial += 1
        elif classes & FAIL_CLASSES:
            failed += 1
    total = passed + partial + failed
    if total == 0:
        return None
    return (passed + 0.5 * partial) / total * 100


def parse_results(html: str) -> list[tuple[str, str, float]]:
    """Extract (checker name, version, pass rate) from one results.html.

    The file went through three layout eras:
    1. 2023-12: one table per checker, ``.tc-name`` outside the tables.
    2. ~2024 to 2026-06: a single multi-column table, ``.tc-name`` in ``th.tc-header``.
    3. 2026-06+: restyled single table with ``thead`` headers and a ``tfoot``
       row containing official totals like ``108.5 / 145 • 74.8%``.
    """
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(".tc-time"):
        node.decompose()

    # Era 3: official totals in <tfoot>.
    headers = [h for h in (th.get_text(strip=True) for th in soup.select('thead th[scope="col"]')) if h]
    tfoot = soup.find("tfoot")
    if headers and tfoot:
        totals = [td.get_text(strip=True) for td in tfoot.find_all("td") if td.get_text(strip=True)]
        rows = []
        for header, total in zip(headers, totals):
            m = re.search(r"([\d.]+)\s*/\s*([\d.]+)", total)
            if m:
                rows.append((header, float(m.group(1)) / float(m.group(2)) * 100))
        return _named(rows)

    # Eras 1-2: count Pass / Partial / Fail cells.
    name_nodes = soup.select(".tc-name")
    rows = []
    if name_nodes and name_nodes[0].find_parent("table") is None:
        # Era 1: each .tc-name is followed by its own table.
        for node in name_nodes:
            table = node.find_next("table")
            cells = [c for c in table.find_all(["td", "th"]) if set(c.get("class", [])) & SCORE_CLASSES]
            rate = _score_cells(cells)
            if rate is not None:
                rows.append((node.get_text(strip=True), rate))
    else:
        # Era 2: one shared table, cells appear in column order.
        n = len(name_nodes)
        columns: list[list] = [[] for _ in range(n)]
        for tr in soup.find_all("tr"):
            cells = [c for c in tr.find_all(["td", "th"]) if set(c.get("class", [])) & SCORE_CLASSES]
            for i, cell in enumerate(cells[:n]):
                columns[i].append(cell)
        for node, cells in zip(name_nodes, columns):
            rate = _score_cells(cells)
            if rate is not None:
                rows.append((node.get_text(strip=True), rate))
    return _named(rows)


def _named(rows: list[tuple[str, float]]) -> list[tuple[str, str, float]]:
    """Split a "name version" header into (lowercase name, version, rate)."""
    out = []
    for header, rate in rows:
        name, _, version = header.partition(" ")
        out.append((name.strip().lower(), version.strip(), rate))
    return out


def collect_records() -> list[Record]:
    session = requests.Session()
    commits = fetch_commits(session)
    commits.sort(key=lambda c: c["commit"]["committer"]["date"])
    records: list[Record] = []
    for i, commit in enumerate(commits, 1):
        sha = commit["sha"]
        when = datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00"))
        html = download_html(session, sha)
        for checker, version, rate in parse_results(html):
            records.append(Record(when, checker, version, rate, sha))
        if i % 20 == 0 or i == len(commits):
            print(f"processed {i}/{len(commits)} commits", flush=True)
    return records


def dedupe(records: list[Record]) -> list[Record]:
    """Keep the latest commit for each (checker, date, version)."""
    latest: dict[tuple[str, str, str], Record] = {}
    for rec in records:
        key = (rec.checker, rec.day, rec.version)
        if key not in latest or rec.when > latest[key].when:
            latest[key] = rec
    return sorted(latest.values(), key=lambda r: (r.when, r.checker))


def write_csv(records: list[Record]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "checker", "version", "pass_rate", "commit"])
        for rec in records:
            writer.writerow([rec.day, rec.checker, rec.version, f"{rec.pass_rate:.1f}", rec.sha])
    print(f"wrote {CSV_PATH} ({len(records)} data points)")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Python Typing Conformance Trend</title>
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
    <style>
        body { font-family: system-ui, "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; }
        header { padding: 1rem 1.5rem 0; }
        h1 { font-size: 1.3rem; margin: 0 0 0.25rem; }
        p { margin: 0.25rem 0; color: #555; font-size: 0.9rem; }
        #chart { width: 100%; height: 82vh; }
        footer { padding: 0.5rem 1.5rem 1rem; color: #888; font-size: 0.8rem; }
    </style>
</head>
<body>
    <header>
        <h1>Python typing conformance test pass rate over time</h1>
        <p>
            One data point per (type checker, date, version), extracted from the history of
            <a href="https://github.com/__REPO__/commits/main/__RESULTS_PATH__">__RESULTS_PATH__</a>
            in <a href="https://github.com/__REPO__">__REPO__</a>.
            Hover a point for details, click legend entries to toggle lines, drag to zoom.
        </p>
    </header>
    <div id="chart"></div>
    <footer>Generated __GENERATED__ · __POINTS__ data points</footer>
    <script>
        const DATA = __DATA__;

        const traces = DATA.map(d => ({
            type: "scatter",
            mode: "lines+markers",
            name: d.checker,
            x: d.x,
            y: d.y,
            text: d.version.map((v, i) =>
                `${d.checker} ${v}<br>${d.x[i].slice(0, 10)} · ${d.y[i].toFixed(1)}%<br>commit ${d.sha[i].slice(0, 7)}`
            ),
            hoverinfo: "text",
            marker: { size: 5 },
            line: { width: 1.5 },
        }));

        const layout = {
            xaxis: { title: "Date" },
            yaxis: { title: "Pass rate (%)", range: [0, 105] },
            hovermode: "closest",
            margin: { t: 20 },
            legend: { orientation: "v" },
        };

        Plotly.newPlot("chart", traces, layout, { responsive: true });
    </script>
</body>
</html>
"""


def write_html(records: list[Record]) -> None:
    by_checker: dict[str, list[Record]] = {}
    for rec in records:
        by_checker.setdefault(rec.checker, []).append(rec)
    payload = [
        {
            "checker": checker,
            "x": [r.when.isoformat() for r in recs],
            "y": [round(r.pass_rate, 2) for r in recs],
            "version": [r.version for r in recs],
            "sha": [r.sha for r in recs],
        }
        for checker, recs in sorted(by_checker.items())
    ]
    html = (
        HTML_TEMPLATE.replace("__DATA__", json.dumps(payload))
        .replace("__GENERATED__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        .replace("__POINTS__", str(len(records)))
        .replace("__REPO__", REPO)
        .replace("__RESULTS_PATH__", RESULTS_PATH)
    )
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"wrote {HTML_PATH}")


def main() -> None:
    records = dedupe(collect_records())
    write_csv(records)
    write_html(records)
