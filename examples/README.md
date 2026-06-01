# Examples — run the Phase 0 pipeline

Self-contained sample source packs and their source files. Everything resolves relative to
the **repository root**, so run the commands from there.

## Prerequisites

- Python 3.10+ (`python3 --version`). No third-party packages — standard library only.
- Run from the repo root: `cd` into the directory that contains the `ai4research/` package.

## The packs

| Pack | What it exercises |
| --- | --- |
| `01_multi_family.jsonl` | Local doc + a YouTube channel (2 videos) + a GitHub repo — all three source families through one spine. |
| `02_inline_only.jsonl` | A single container with `inline_text` — **no external files**, runs anywhere. |
| `03_with_gap.jsonl` | A channel with one staged video and one whose transcript is missing — shows a recorded coverage gap (failed acquisition) on an otherwise finalized run. |

Source content for the file-based packs lives in `examples/sources/`.

## Run

```bash
# from the repo root
python3 -m ai4research demo \
  --topic "latest technologies in skills governance" \
  --source-pack examples/01_multi_family.jsonl \
  --runs-dir runs
```

The command prints the `run_id`, final `status` (`finalized` | `diagnostic_only` | `failed`),
the report path, and per-table row counts.

## Inspect the output

```bash
RUN=$(ls -1dt runs/run_* | head -1)        # newest run

cat "$RUN/exports/final_report.md"          # the report (Markdown)
xdg-open "$RUN/exports/final_report.html"   # or open the HTML in a browser
cat "$RUN/exports/bundle_manifest.json"     # run manifest + status
ls "$RUN/work"                              # one JSONL per table (the working state)

# query the durable SQLite store (all runs land here)
sqlite3 runs/ai4research.db "SELECT run_id, status FROM runs;"
sqlite3 runs/ai4research.db "SELECT gate_id, status FROM gate_results;"
sqlite3 runs/ai4research.db "SELECT status, failure_code FROM acquisition_attempts;"
```

## What Phase 0 does — and does not — do

- **Does:** read the source files/inline text you supply, normalize them, split into spans,
  extract substantive declarative sentences as *evidence*, build one *claim* per evidence
  with a *citation* back to the exact source span, run quality gates, and render a report —
  all deterministic, offline, no LLM.
- **Does not:** fetch anything from the network. A `youtube_channel` item is a URL **plus a
  transcript file you provide**; a `github_repo` item is a URL **plus a file you provide**.
  There is no channel/video discovery and no transcript download — that is Phase 2.
- **Extraction is mechanical:** every qualifying sentence becomes a claim. Phase 0 proves
  *grounding* (nothing reaches the report without a traceable source), not editorial judgement
  about which claims matter. Ranking, dedup, and LLM extraction are later phases.

## Source pack format

JSONL, one container per line, each with nested `items`. Item locators by family:

- `local_document_set` → `{"local_path": "..."}` or `{"inline_text": "..."}`
- `youtube_channel` → `{"url": "...", "local_fixture_path": "transcript.txt"}` (or `inline_text`)
- `github_repo` → `{"url": "...", "local_fixture_path": "file.md"}` (or `inline_text`)

`title`, `creator`, `published_at`, and `provider_metadata` are optional. A missing fixture
file is recorded as a failed acquisition (a visible coverage gap), not a crash.
