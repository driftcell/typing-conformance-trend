# python-typing-result-trend

Interactive trend chart of [Python typing conformance test](https://typing.python.org/en/latest/conformance/results.html) pass rates, built from the git history of
[`conformance/results/results.html`](https://github.com/python/typing/commits/main/conformance/results/results.html) in `python/typing`.

- X axis: commit date; Y axis: pass rate (%)
- One data point per (type checker, date, version) — the latest commit of that day
- Pass rate = (Pass + 0.5 × Partial) / total, matching the suite's own convention

## Usage

```sh
uv run python-typing-result-trend
```

Outputs:

- `index.html` — self-contained interactive chart (Plotly.js via CDN); hover points for version/commit details, click the legend to toggle lines
- `trend.csv` — the extracted data points
- `data/` — local cache of downloaded `results.html` snapshots (safe to delete; will be re-downloaded)

## Publish with GitHub Pages

The generated `index.html` is fully static and can be served as-is:

1. Commit `index.html` and push this repository to GitHub.
2. In the GitHub repo: **Settings → Pages → Build and deployment**, choose **Deploy from a branch**, then select branch `main` and folder `/ (root)`.
3. After a minute the chart is live at `https://<your-user>.github.io/<repo>/`.

Re-run the script and push again to refresh the chart with new conformance results.

## Automatic updates

[`.github/workflows/update.yml`](.github/workflows/update.yml) rebuilds and pushes `index.html` / `trend.csv` every Monday (UTC), keeping the published chart up to date. It can also be triggered manually via **Actions → Update trend chart → Run workflow**. The `data/` download cache is reused between runs, so only new commits are fetched.
