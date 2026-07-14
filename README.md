# PubMed Daily Digest

Runs `pubmed_digest.py` every morning on GitHub Actions (independent of any laptop)
and publishes the result to GitHub Pages.

- **Schedule:** 09:30 UTC daily (≈ 5:30 AM US Eastern) — see `.github/workflows/digest.yml`.
- **Output:** the latest digest is served at the repo's GitHub Pages URL; dated archives
  live under `/digests/`.
- **State:** `seen_pmids.json` (a pmid → first-seen-date map) is committed back after each
  run so de-duplication carries over day to day.
- **Secret:** `NCBI_API_KEY` is stored as a GitHub Actions secret, never committed.

## Run it now
Actions tab → *PubMed Daily Digest* → **Run workflow**.

## Change the time
Edit the `cron` line in `.github/workflows/digest.yml` (the value is in UTC).

## Feedback loop

The digest learns from two signals, and both are captured straight from the page —
on a Mac or an iPhone, with nothing running locally:

| Button | What it does |
|---|---|
| ⭐ **Star** | One tap. The page POSTs to a small Vercel function (`feedback-api/`) that opens the GitHub Issue for you; the `Digest Feedback` workflow then adds the paper to `knowledge_base.json`, ingests it into the knowledge graph as endorsed knowledge, commits, and closes the issue. |
| 👎 **Not relevant** | Same, but the paper is blocklisted (never surfaced again) and its node and edges are marked `rejected` in the graph. |

If the endpoint is unreachable (or its `FEEDBACK_GITHUB_TOKEN` env var is unset),
the buttons fall back to opening the prefilled issue form — the original two-tap
flow. The endpoint's token is a fine-grained PAT scoped to this repo with
*Issues: Read and write* only.

**How it hones the digest.** `knowledge_graph.json` projects every judged paper into
nodes (Paper, MeshTerm, Journal, Author) and edges. Each concept gets a weight:

    w(concept) = fraction of starred papers touching it − fraction of rejected papers touching it

Concepts your stars cluster on rise, concepts your rejects cluster on fall, and
anything both sides touch cancels out. A candidate article is scored `quality + fit`:
quality is the original 12-point rubric, and `fit` is −3..+3 read off that graph, plus
the same signal over title/abstract text (fresh PubMed records often carry no MeSH
terms yet). The more you star and reject, the sharper it gets.

Rejections are kept in the graph rather than deleted — a rejection is knowledge, and
it is what gives the scorer its negative side.

    python3 feedback.py --reject <PMID|DOI> --reason "..."   # thumbs-down from the CLI
    python3 feedback.py --report          # what's been thumbs-downed, and why
    python3 knowledge_graph.py --stats    # what the graph steers toward / away from
    python3 test_feedback.py              # 33 tests, no network
