"""Build an interactive trend chart of Python typing conformance test results.

Data source: the git history of ``conformance/results/results.html`` in the
`python/typing <https://github.com/python/typing>`_ repository.

For every commit touching that file we download the file and extract each
type checker's version and pass rate. Per checker we keep one point per day
and version (the latest commit of that day), then drop points that repeat
the previous point's version and pass rate — a point on the chart means the
checker's version or its pass rate changed.

Outputs:
- ``index.html`` — self-contained interactive chart (Plotly.js basic bundle
  via CDN) with SEO meta tags, JSON-LD structured data and a crawlable
  latest-results table; ready to be served with GitHub Pages.
- ``trend.csv`` — all data points.
- ``preview.png`` — static chart image used as Open Graph preview.
- ``robots.txt`` / ``sitemap.xml``.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests
from bs4 import BeautifulSoup

REPO = "python/typing"
RESULTS_PATH = "conformance/results/results.html"
API_URL = f"https://api.github.com/repos/{REPO}/commits"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}"

SITE_URL = "https://driftcell.github.io/typing-conformance-trend/"
SITE_REPO_URL = "https://github.com/driftcell/typing-conformance-trend"
SITE_TITLE = "Python Type Checker Conformance Trend"
SITE_DESCRIPTION = (
    "How mypy, pyright, ty, zuban, pyrefly and other Python type checkers score "
    "on the official typing specification conformance test suite over time."
)

DATA_DIR = Path("data")
HTML_DIR = DATA_DIR / "html"
COMMITS_JSON = DATA_DIR / "commits.json"
CSV_PATH = Path("trend.csv")
HTML_PATH = Path("index.html")
PREVIEW_PATH = Path("preview.png")
ROBOTS_PATH = Path("robots.txt")
SITEMAP_PATH = Path("sitemap.xml")

PASS_CLASSES = {"conformant"}
PARTIAL_CLASSES = {"partially-conformant"}
FAIL_CLASSES = {"not-conformant", "nonconformant"}
SCORE_CLASSES = PASS_CLASSES | PARTIAL_CLASSES | FAIL_CLASSES

# Checkers whose lines are hidden by default (still toggleable via the legend):
# those no longer present in the suite's latest run, plus basilisk, which
# reached its score by gaming the tests.
HIDDEN_BY_DEFAULT = {"basilisk"}


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


@dataclass(frozen=True)
class SuiteEra:
    """A period during which the test suite had a constant number of tests."""

    start: datetime
    end: datetime
    size: int


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


def parse_results(html: str) -> tuple[list[tuple[str, str, float]], int | None]:
    """Extract (checker name, version, pass rate) plus suite size from one results.html.

    The file went through three layout eras:
    1. 2023-12: one table per checker, ``.tc-name`` outside the tables.
    2. ~2024 to 2026-06: a single multi-column table, ``.tc-name`` in ``th.tc-header``.
    3. 2026-06+: restyled single table with ``thead`` headers and a ``tfoot``
       row containing official totals like ``108.5 / 145 • 74.8%``.

    The suite size is the tfoot denominator (era 3) or the per-checker count of
    scored cells (eras 1-2, taking the max in case a checker skips tests).
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
        sizes = []
        for header, total in zip(headers, totals):
            m = re.search(r"([\d.]+)\s*/\s*([\d.]+)", total)
            if m:
                rows.append((header, float(m.group(1)) / float(m.group(2)) * 100))
                sizes.append(int(float(m.group(2))))
        return _named(rows), max(sizes, default=None)

    # Eras 1-2: count Pass / Partial / Fail cells.
    name_nodes = soup.select(".tc-name")
    rows = []
    sizes = []
    if name_nodes and name_nodes[0].find_parent("table") is None:
        # Era 1: each .tc-name is followed by its own table.
        for node in name_nodes:
            table = node.find_next("table")
            cells = [c for c in table.find_all(["td", "th"]) if set(c.get("class", [])) & SCORE_CLASSES]
            rate = _score_cells(cells)
            if rate is not None:
                rows.append((node.get_text(strip=True), rate))
                sizes.append(len(cells))
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
                sizes.append(len(cells))
    return _named(rows), max(sizes, default=None)


def _named(rows: list[tuple[str, float]]) -> list[tuple[str, str, float]]:
    """Split a "name version" header into (lowercase name, version, rate)."""
    out = []
    for header, rate in rows:
        name, _, version = header.partition(" ")
        out.append((name.strip().lower(), version.strip(), rate))
    return out


def collect_records() -> tuple[list[Record], list[tuple[datetime, str, int]]]:
    session = requests.Session()
    commits = fetch_commits(session)
    commits.sort(key=lambda c: c["commit"]["committer"]["date"])
    records: list[Record] = []
    suite_sizes: list[tuple[datetime, str, int]] = []
    for i, commit in enumerate(commits, 1):
        sha = commit["sha"]
        when = datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00"))
        html = download_html(session, sha)
        rows, size = parse_results(html)
        for checker, version, rate in rows:
            records.append(Record(when, checker, version, rate, sha))
        if size is not None:
            suite_sizes.append((when, sha, size))
        if i % 20 == 0 or i == len(commits):
            print(f"processed {i}/{len(commits)} commits", flush=True)
    return records, suite_sizes


def suite_eras(sizes: list[tuple[datetime, int]]) -> list[SuiteEra]:
    """Merge consecutive commits with the same suite size into eras.

    Each era runs from the first commit with its size up to the first commit of
    the next era; the last era ends at the newest commit (padded a little so the
    band stays visible when the newest commit itself changed the size).
    """
    eras: list[SuiteEra] = []
    for when, size in sorted(sizes):
        if eras and eras[-1].size == size:
            continue
        eras.append(SuiteEra(when, when, size))
    for i in range(len(eras) - 1):
        eras[i] = SuiteEra(eras[i].start, eras[i + 1].start, eras[i].size)
    if eras:
        last = eras[-1]
        end = max(when for when, _ in sizes)
        if end <= last.start:
            end = last.start + timedelta(days=5)
        eras[-1] = SuiteEra(last.start, end, last.size)
    return eras


def dedupe(records: list[Record]) -> list[Record]:
    """Keep the latest commit for each (checker, date, version)."""
    latest: dict[tuple[str, str, str], Record] = {}
    for rec in records:
        key = (rec.checker, rec.day, rec.version)
        if key not in latest or rec.when > latest[key].when:
            latest[key] = rec
    return sorted(latest.values(), key=lambda r: (r.when, r.checker))


def drop_repeats(records: list[Record]) -> list[Record]:
    """Drop points that carry no new information, i.e. both the checker's
    version and its pass rate are identical to the previous kept point."""
    by_checker: dict[str, list[Record]] = {}
    for rec in records:
        by_checker.setdefault(rec.checker, []).append(rec)
    kept: list[Record] = []
    for recs in by_checker.values():
        prev: Record | None = None
        for rec in sorted(recs, key=lambda r: r.when):
            if prev and rec.version == prev.version and round(rec.pass_rate, 6) == round(prev.pass_rate, 6):
                continue
            kept.append(rec)
            prev = rec
    return sorted(kept, key=lambda r: (r.when, r.checker))


def write_csv(records: list[Record]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "checker", "version", "pass_rate", "commit"])
        for rec in records:
            writer.writerow([rec.day, rec.checker, rec.version, f"{rec.pass_rate:.1f}", rec.sha])
    print(f"wrote {CSV_PATH} ({len(records)} data points)")


def _latest_per_checker(records: list[Record]) -> dict[str, Record]:
    latest: dict[str, Record] = {}
    for rec in records:
        if rec.checker not in latest or rec.when > latest[rec.checker].when:
            latest[rec.checker] = rec
    return latest


def hidden_checkers(records: list[Record], raw_records: list[Record]) -> set[str]:
    """Checkers hidden by default: retired from the suite, plus HIDDEN_BY_DEFAULT."""
    latest_sha = max(raw_records, key=lambda r: r.when).sha
    current = {r.checker for r in raw_records if r.sha == latest_sha}
    return ({r.checker for r in records} - current) | HIDDEN_BY_DEFAULT


def plot_preview(records: list[Record], eras: list[SuiteEra], hidden: set[str]) -> None:
    """Render a static chart image (1200x630) used as the Open Graph preview."""
    by_checker: dict[str, list[Record]] = {}
    for rec in records:
        if rec.checker not in hidden:
            by_checker.setdefault(rec.checker, []).append(rec)

    fig, ax = plt.subplots(figsize=(12, 6.3), dpi=100)
    span = max(r.when for r in records) - min(r.when for r in records)
    labeled = 0
    for i, era in enumerate(eras):
        ax.axvspan(era.start, era.end, color="#5078b4", alpha=0.1 if i % 2 else 0.04, linewidth=0)
        if (era.end - era.start) / span >= 0.03:
            ax.text(
                era.start + (era.end - era.start) / 2,
                101 if labeled % 2 else 96,
                f"{era.size} tests",
                ha="center",
                va="top",
                fontsize=9,
                color="#8a97a8",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 1.5},
            )
            labeled += 1
    for checker, recs in sorted(by_checker.items()):
        ax.plot(
            [r.when for r in recs],
            [r.pass_rate for r in recs],
            marker=".",
            markersize=7,
            linewidth=2,
            label=checker,
        )
    ymin = int(min(r.pass_rate for recs in by_checker.values() for r in recs) // 10 * 10)
    ax.set_ylim(ymin, 105)
    ax.set_title(SITE_TITLE, fontsize=22)
    ax.set_ylabel("Pass rate (%)", fontsize=15)
    ax.tick_params(labelsize=12)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    ax.grid(alpha=0.3)
    ax.legend(fontsize=12, loc="center left", bbox_to_anchor=(1.0, 0.5))
    fig.tight_layout()
    fig.savefig(PREVIEW_PATH)
    plt.close(fig)
    print(f"wrote {PREVIEW_PATH}")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>__TITLE__</title>
    <meta name="description" content="__DESC__">
    <link rel="canonical" href="__SITE_URL__">
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="__TITLE__">
    <meta property="og:title" content="__TITLE__">
    <meta property="og:description" content="__DESC__">
    <meta property="og:url" content="__SITE_URL__">
    <meta property="og:image" content="__SITE_URL__preview.png">
    <meta property="og:image:width" content="1200">
    <meta property="og:image:height" content="630">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="__TITLE__">
    <meta name="twitter:description" content="__DESC__">
    <meta name="twitter:image" content="__SITE_URL__preview.png">
    <script type="application/ld+json">__JSONLD__</script>
    <script src="https://cdn.plot.ly/plotly-basic-2.35.2.min.js" charset="utf-8"></script>
    <style>
        body { font-family: system-ui, "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; color: #1a1a1a; }
        header, main, footer { max-width: 960px; margin: 0 auto; padding: 0 1.5rem; }
        header { padding-top: 1.5rem; }
        h1 { font-size: 1.5rem; margin: 0 0 0.5rem; }
        h2 { font-size: 1.1rem; margin: 1.5rem 0 0.5rem; }
        p { margin: 0.4rem 0; color: #444; font-size: 0.95rem; line-height: 1.6; }
        #chart { width: 100%; height: 75vh; min-height: 400px; }
        table { border-collapse: collapse; margin: 0.5rem 0; font-size: 0.95rem; }
        th, td { padding: 0.35rem 1rem; text-align: left; border-bottom: 1px solid #e2e2e2; }
        th { color: #555; font-weight: 600; }
        td.rate { font-variant-numeric: tabular-nums; }
        tbody tr:hover { background: #f6f6f6; }
        .note { color: #777; font-size: 0.85rem; }
        footer { padding: 1rem 1.5rem 2rem; color: #888; font-size: 0.8rem; }
    </style>
</head>
<body>
    <header>
        <h1>Python type checker conformance trend</h1>
        <p>
            This page tracks how well popular Python type checkers &mdash; mypy, pyright, ty,
            zuban, pyrefly and pycroscope &mdash; conform to the official
            <a href="https://github.com/python/typing/blob/main/conformance/results/results.html">Python typing
            specification conformance test suite</a> over time.
            A point is added whenever a checker's version or pass rate changes.
            Shaded background bands mark periods in which the test suite had the same
            number of tests (labeled at the top). Checkers no longer run by the suite
            are hidden by default. Hover a point for the exact version and commit,
            click legend entries to toggle lines, drag to zoom.
        </p>
    </header>
    <div id="chart"></div>
    <noscript><p>The interactive chart requires JavaScript; the latest results are listed below.</p></noscript>
    <main>
        <section>
            <h2>Latest results</h2>
            <p>Pass rates from the most recent published conformance run
            (<time datetime="__AS_OF__">__AS_OF__</time>):</p>
            <table>
                <thead>
                    <tr><th>#</th><th>Type checker</th><th>Version</th><th>Pass rate</th></tr>
                </thead>
                <tbody>
__SNAPSHOT_ROWS__
                </tbody>
            </table>
            <p class="note">__RETIRED__ also appeared in the suite in the past but were removed
            later; their lines are hidden by default (click a legend entry to show one).</p>
        </section>
        <section>
            <h2>How the data is built</h2>
            <p>
                Every data point is extracted from the git history of
                <a href="https://github.com/__REPO__/commits/main/__RESULTS_PATH__">__RESULTS_PATH__</a>
                in the <a href="https://github.com/__REPO__">__REPO__</a> repository. The pass rate
                follows the suite's own convention: (Pass + 0.5 &times; Partial) / total tests.
                Note that the test suite itself grows over time (from 42 to 145+ tests); the
                shaded bands delimit periods with a constant number of tests, so pass rates
                across band boundaries are not strictly comparable.
            </p>
            <p>
                The chart is regenerated weekly by GitHub Actions. The underlying data points are
                available as <a href="trend.csv">trend.csv</a>, and the source code lives on
                <a href="__SITE_REPO_URL__">GitHub</a>.
            </p>
        </section>
    </main>
    <footer>Generated __GENERATED__ · __POINTS__ data points</footer>
    <script>
        const DATA = __DATA__;
        const ERAS = __ERAS__;

        const traces = DATA.map(d => ({
            type: "scatter",
            mode: "lines+markers",
            name: d.checker,
            x: d.x,
            y: d.y,
            text: d.version.map((v, i) =>
                `${d.checker} ${v}<br>${d.x[i].slice(0, 10)} · ${d.y[i].toFixed(1)}% of ${d.tests[i] ?? "?"} tests<br>commit ${d.sha[i].slice(0, 7)}`
            ),
            hoverinfo: "text",
            marker: { size: 5 },
            line: { width: 1.5 },
            visible: d.hidden ? "legendonly" : true,
        }));

        // Alternating background bands for each suite-size era, plus staggered
        // "N tests" labels. Labels are recomputed on zoom: an era is labeled
        // when the part of it inside the current view spans at least ~4% of
        // the visible range, and the label is centered on that visible part.
        const shapes = ERAS.map((era, i) => ({
            type: "rect",
            xref: "x",
            yref: "paper",
            x0: era.start,
            x1: era.end,
            y0: 0,
            y1: 1,
            fillcolor: i % 2 ? "rgba(80, 120, 180, 0.10)" : "rgba(80, 120, 180, 0.04)",
            line: { width: 0 },
            layer: "below",
        }));

        const gd = document.getElementById("chart");
        const dataX = DATA.flatMap(d => d.x.map(t => new Date(t).getTime()));
        const dataRange = [Math.min(...dataX), Math.max(...dataX)];

        function viewRange() {
            const xa = gd.layout && gd.layout.xaxis; // no .layout before the first render
            if (xa && xa.range && !xa.autorange) return xa.range.map(v => new Date(v).getTime());
            return dataRange;
        }

        function eraAnnotations() {
            const [v0, v1] = viewRange();
            const span = v1 - v0;
            return ERAS
                .map(era => {
                    const lo = Math.max(new Date(era.start).getTime(), v0);
                    const hi = Math.min(new Date(era.end).getTime(), v1);
                    return { era, lo, hi, frac: (hi - lo) / span };
                })
                .filter(o => o.frac >= 0.04)
                .map((o, i) => ({
                    x: new Date((o.lo + o.hi) / 2),
                    y: i % 2 ? 0.93 : 0.99,
                    xref: "x",
                    yref: "paper",
                    yanchor: "top",
                    text: `${o.era.size} tests`,
                    showarrow: false,
                    font: { size: 10, color: "#8a97a8" },
                    bgcolor: "rgba(255, 255, 255, 0.85)",
                    borderpad: 2,
                }));
        }

        const layout = {
            xaxis: { title: "Date" },
            // Autorange: fits the visible lines and re-fits when legend-only
            // traces are toggled on.
            yaxis: { title: "Pass rate (%)" },
            hovermode: "closest",
            margin: { t: 20 },
            legend: { orientation: "v" },
            shapes,
            annotations: eraAnnotations(),
        };

        Plotly.newPlot(gd, traces, layout, { responsive: true }).then(() => {
            let updating = false;
            gd.on("plotly_relayout", () => {
                if (updating) return; // triggered by our own annotation refresh
                const next = eraAnnotations();
                if (JSON.stringify(next) === JSON.stringify(gd.layout.annotations || [])) return;
                updating = true;
                Plotly.relayout(gd, { annotations: next }).then(() => { updating = false; });
            });
        });
    </script>
</body>
</html>
"""


def write_html(
    records: list[Record],
    raw_records: list[Record],
    generated: datetime,
    eras: list[SuiteEra],
    size_by_sha: dict[str, int],
    hidden: set[str],
) -> None:
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
            "tests": [size_by_sha.get(r.sha) for r in recs],
            "hidden": checker in hidden,
        }
        for checker, recs in sorted(by_checker.items())
    ]

    # Background bands: one per suite-size era. Which eras get an on-chart
    # label is decided in the browser, based on the currently visible x range.
    eras_payload = [
        {"start": era.start.isoformat(), "end": era.end.isoformat(), "size": era.size}
        for era in eras
    ]

    # Latest snapshot table: checkers present in the newest commit's results.
    latest_sha = max(raw_records, key=lambda r: r.when).sha
    current = {r.checker for r in raw_records if r.sha == latest_sha}
    latest = _latest_per_checker(records)
    snapshot = sorted((latest[c] for c in current), key=lambda r: r.pass_rate, reverse=True)
    rows = "\n".join(
        f'                    <tr><td>{i}</td><td>{r.checker}</td>'
        f"<td>{r.version}</td><td class=\"rate\">{r.pass_rate:.1f}%</td></tr>"
        for i, r in enumerate(snapshot, 1)
    )
    retired = ", ".join(sorted(set(latest) - current)) or "None"
    as_of = max(r.when for r in snapshot).date().isoformat()

    jsonld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": SITE_TITLE,
            "description": SITE_DESCRIPTION,
            "url": SITE_URL,
            "isBasedOn": f"https://github.com/{REPO}",
            "keywords": ["Python", "typing", "type checker", "conformance", *sorted(by_checker)],
            "variableMeasured": "conformance test pass rate (%)",
            "dateModified": as_of,
            "distribution": {
                "@type": "DataDownload",
                "contentUrl": f"{SITE_URL}trend.csv",
                "encodingFormat": "text/csv",
            },
        },
        ensure_ascii=False,
    )

    html = HTML_TEMPLATE
    for key, value in {
        "__DATA__": json.dumps(payload),
        "__ERAS__": json.dumps(eras_payload),
        "__SNAPSHOT_ROWS__": rows,
        "__RETIRED__": retired,
        "__AS_OF__": as_of,
        "__JSONLD__": jsonld,
        "__GENERATED__": generated.strftime("%Y-%m-%d %H:%M UTC"),
        "__POINTS__": str(len(records)),
        "__TITLE__": SITE_TITLE,
        "__DESC__": SITE_DESCRIPTION,
        "__SITE_URL__": SITE_URL,
        "__SITE_REPO_URL__": SITE_REPO_URL,
        "__REPO__": REPO,
        "__RESULTS_PATH__": RESULTS_PATH,
    }.items():
        html = html.replace(key, value)
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"wrote {HTML_PATH}")


def write_static_files(generated: datetime) -> None:
    ROBOTS_PATH.write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}sitemap.xml\n",
        encoding="utf-8",
    )
    SITEMAP_PATH.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE_URL}</loc><lastmod>{generated.date().isoformat()}</lastmod></url>\n"
        "</urlset>\n",
        encoding="utf-8",
    )
    print(f"wrote {ROBOTS_PATH} and {SITEMAP_PATH}")


def main() -> None:
    raw_records, suite_sizes = collect_records()
    records = drop_repeats(dedupe(raw_records))
    eras = suite_eras([(when, size) for when, _, size in suite_sizes])
    size_by_sha = {sha: size for _, sha, size in suite_sizes}
    hidden = hidden_checkers(records, raw_records)
    generated = datetime.now(timezone.utc)
    write_csv(records)
    write_html(records, raw_records, generated, eras, size_by_sha, hidden)
    plot_preview(records, eras, hidden)
    write_static_files(generated)
