#!/usr/bin/env python3
"""
Apply one feedback issue to the knowledge base / feedback store / graph.

Run by .github/workflows/feedback.yml whenever an issue is opened from the
digest's ⭐ Star or 👎 Not relevant button. Prints a one-line summary on stdout,
which the workflow posts back as the issue comment before closing it.

  python3 apply_feedback.py --title "⭐ star 42098926" --body "...optional notes..."
  python3 apply_feedback.py --title "👎 reject 42098926" --body "...optional reason..."
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import feedback
import knowledge_graph as kg

BASE_DIR = Path(__file__).resolve().parent
KB_FILE  = BASE_DIR / "knowledge_base.json"

# Lines the digest pre-fills into the issue body — not something the user typed.
TEMPLATE_MARKERS = (
    "Starred from the digest",
    "Reason (optional",
    "Add notes below",
)


def parse_title(title: str):
    """'⭐ star 42098926' / '👎 reject 42098926' -> ('star'|'reject', pmid)."""
    m = re.search(r"\b(star|reject)\b\D*(\d+)", title, re.IGNORECASE)
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2)


def parse_note(body: str, title_line: str = "") -> str:
    """Everything the user actually typed, template and echoed title stripped."""
    out = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            continue                      # the article title the digest pre-filled
        if any(marker in line for marker in TEMPLATE_MARKERS):
            continue
        out.append(line)
    return " ".join(out).strip()


def load_kb() -> list:
    if KB_FILE.exists():
        try:
            return json.loads(KB_FILE.read_text()).get("papers", [])
        except Exception:
            pass
    return []


def save_kb(papers: list):
    KB_FILE.write_text(
        json.dumps({"papers": papers, "last_updated": date.today().isoformat()},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def do_star(pmid: str, note: str) -> str:
    if pmid in feedback.blocklist():
        return (f"⚠️ PMID {pmid} is on the thumbs-down blocklist — not starred. "
                f"Un-reject it first if that was a mistake.")

    papers = load_kb()
    if any(str(p.get("pmid")) == pmid for p in papers):
        return f"📚 PMID {pmid} was already in the knowledge base."

    art = feedback.fetch_article(pmid)
    if not art or not art.get("title"):
        return f"❌ Could not fetch PMID {pmid} from PubMed."

    art.update({
        "doi":        "",
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "saved_date": date.today().isoformat(),
        "notes":      note,
        "tags":       [],
    })
    papers.append(art)
    save_kb(papers)
    kg.ingest_paper(art)
    return (f"⭐ Starred **{art['title'][:90]}** — knowledge base now has "
            f"{len(papers)} papers, and the graph will steer toward its MeSH terms.")


def do_reject(pmid: str, reason: str) -> str:
    art = feedback.fetch_article(pmid)
    rec = feedback.add_rejection(
        pmid=pmid,
        title=art.get("title", ""),
        reason=reason,
        abstract=art.get("abstract", ""),
        mesh_terms=art.get("mesh_terms", []),
        journal=art.get("journal", ""),
        authors=art.get("authors", []),
        year=art.get("year", ""),
    )
    changed = kg.reject_pmid(pmid, paper=rec)
    title = rec["title"] or f"PMID {pmid}"
    msg = (f"👎 Rejected **{title[:90]}** — blocklisted from future pulls, and "
           f"{changed} graph element(s) marked rejected so scoring steers away.")
    if reason:
        msg += f"\n\nReason recorded: _{reason}_"
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="")
    args = ap.parse_args()

    kind, pmid = parse_title(args.title)
    if not kind:
        print("⚠️ Not a feedback issue — expected a title like `⭐ star 12345` "
              "or `👎 reject 12345`. Ignored.")
        return 0

    note = parse_note(args.body, args.title)
    result = do_star(pmid, note) if kind == "star" else do_reject(pmid, note)

    kg.build()   # rebuild so both sides of the graph stay consistent
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
