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
