#!/usr/bin/env python3
"""
Thumbs-down feedback for PubMedAgent.

A thumbs-down means "this article does not serve the goal of learning +
improving knowledge-graph data quality". Rejections are appended to a single
human-readable file (feedback.json) that doubles as:

  * the blocklist  — these PMIDs are never surfaced in a future pull
  * the negatives  — title + abstract + MeSH of every reject, projected into the
                     knowledge graph as rejected nodes so future scoring steers
                     away from them (see knowledge_graph.py / relevance.py)

The positive half of the loop lives in knowledge_base.json (⭐ starred papers).

Usage:
  python3 feedback.py --reject <PMID|DOI> [--reason "off-target: veterinary"]
  python3 feedback.py --unreject <PMID>          # undo a thumbs-down
  python3 feedback.py --report                   # summary + most common reasons
  python3 feedback.py --list                     # every rejection, newest first

Any change is committed and pushed, so the next scheduled digest learns from it.
Pass --no-push to keep a change local.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE_DIR      = Path(__file__).resolve().parent
FEEDBACK_FILE = BASE_DIR / "feedback.json"
KB_FILE       = BASE_DIR / "knowledge_base.json"
GRAPH_FILE    = BASE_DIR / "knowledge_graph.json"
ENV_FILE      = BASE_DIR / ".env.local"

# This working copy is a clone of the repo GitHub Actions runs the digest from,
# so learned state is committed in place — feedback lands where tomorrow's pull
# will see it. (It used to be copied into a separate ~/Desktop/pubmed-digest-cloud
# clone; that clone is gone and the indirection with it.)
SYNC_FILES  = ["feedback.json", "knowledge_base.json", "knowledge_graph.json"]

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "were", "are",
    "has", "have", "had", "not", "but", "its", "into", "than", "such", "these",
    "those", "which", "who", "whom", "been", "being", "their", "there", "them",
    "study", "studies", "results", "methods", "background", "conclusions",
    "objective", "objectives", "purpose", "aim", "aims", "using", "used", "use",
    "may", "can", "our", "we", "of", "in", "to", "a", "an", "on", "by", "as",
    "at", "is", "it", "be", "or", "no",
}


# ── Store ──────────────────────────────────────────────────────────────────────
def load_feedback(path: Path = None) -> dict:
    path = path or FEEDBACK_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text())
            data.setdefault("rejections", [])
            return data
        except Exception:
            pass
    return {"rejections": []}


def save_feedback(fb: dict, path: Path = None):
    path = path or FEEDBACK_FILE
    fb["last_updated"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(fb, indent=2, ensure_ascii=False), encoding="utf-8")


def blocklist(path: Path = None) -> set:
    """PMIDs that must never be surfaced again. Never pruned."""
    return {r["pmid"] for r in load_feedback(path).get("rejections", []) if r.get("pmid")}


def negatives(path: Path = None) -> list:
    return load_feedback(path).get("rejections", [])


def add_rejection(pmid: str, title: str = "", reason: str = "", abstract: str = "",
                  mesh_terms=None, journal: str = "", authors=None, year: str = "",
                  timestamp: str = None, path: Path = None) -> dict:
    """Record a thumbs-down. Re-rejecting an existing PMID updates its reason."""
    fb  = load_feedback(path)
    ts  = timestamp or datetime.now().isoformat(timespec="seconds")
    rec = {
        "pmid":       str(pmid),
        "title":      title,
        "reason":     reason,
        "timestamp":  ts,
        "journal":    journal,
        "year":       year,
        "abstract":   abstract,
        "authors":    list(authors or []),
        "mesh_terms": list(mesh_terms or []),
    }
    for i, existing in enumerate(fb["rejections"]):
        if existing["pmid"] == str(pmid):
            rec["timestamp"] = existing.get("timestamp", ts)  # keep the original
            fb["rejections"][i] = rec
            save_feedback(fb, path)
            return rec
    fb["rejections"].append(rec)
    save_feedback(fb, path)
    return rec


def remove_rejection(pmid: str, path: Path = None) -> bool:
    fb     = load_feedback(path)
    before = len(fb["rejections"])
    fb["rejections"] = [r for r in fb["rejections"] if r["pmid"] != str(pmid)]
    if len(fb["rejections"]) == before:
        return False
    save_feedback(fb, path)
    return True


# ── Tokenisation (shared with relevance.py) ────────────────────────────────────
def tokens(text: str) -> set:
    return {
        t for t in re.findall(r"[a-z][a-z0-9-]{2,}", (text or "").lower())
        if t not in STOPWORDS
    }


def doc_tokens(rec: dict) -> set:
    """Token set for a paper-shaped dict. MeSH terms count too."""
    parts = [rec.get("title", ""), rec.get("abstract", "")]
    parts += list(rec.get("mesh_terms") or [])
    return tokens(" ".join(parts))


# ── PubMed lookup (capture by PMID or DOI) ─────────────────────────────────────
def _load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _api_key() -> str:
    return _load_env(ENV_FILE).get("NCBI_API_KEY") or os.environ.get("NCBI_API_KEY", "")


def resolve_to_pmid(ident: str) -> str:
    """Accept a PMID or a DOI; return a PMID ('' if it can't be resolved)."""
    ident = ident.strip()
    if ident.isdigit():
        return ident
    params = {"db": "pubmed", "term": f"{ident}[DOI]", "retmode": "xml"}
    key = _api_key()
    if key:
        params["api_key"] = key
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
           + urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            root = ET.fromstring(r.read().decode("utf-8"))
        el = root.find(".//IdList/Id")
        return el.text if el is not None and el.text else ""
    except Exception:
        return ""
    finally:
        time.sleep(0.15 if key else 0.4)


def fetch_article(pmid: str) -> dict:
    """Pull title/abstract/MeSH so the rejection carries a usable negative example."""
    params = {"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"}
    key = _api_key()
    if key:
        params["api_key"] = key
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
           + urllib.parse.urlencode(params))
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            root = ET.fromstring(r.read().decode("utf-8"))
    except Exception:
        return {}
    article = root.find(".//PubmedArticle")
    if article is None:
        return {}

    title = re.sub(r"<[^>]+>", "", article.findtext(".//ArticleTitle", ""))
    abstract = " ".join(
        (f"{el.get('Label')}: " if el.get("Label") else "") + (el.text or "")
        for el in article.findall(".//AbstractText")
    ).strip()
    authors = []
    for au in article.findall(".//Author"):
        last = au.findtext("LastName", "")
        fore = au.findtext("ForeName", "") or au.findtext("Initials", "")
        if last:
            authors.append(f"{last} {fore}".strip())
    mesh = [m.findtext("DescriptorName", "") for m in article.findall(".//MeshHeading")]
    journal = (article.findtext(".//Journal/Title")
               or article.findtext(".//ISOAbbreviation", ""))
    year = (article.findtext(".//PubDate/Year")
            or article.findtext(".//PubDate/MedlineDate", "")[:4])

    return {
        "pmid":       str(pmid),
        "title":      title,
        "abstract":   abstract,
        "authors":    authors,
        "mesh_terms": [m for m in mesh if m],
        "journal":    journal,
        "year":       year,
    }


# ── Cloud sync ─────────────────────────────────────────────────────────────────
def sync_cloud(message: str) -> bool:
    """
    Commit the learned state and push it. Best-effort: a failure here never
    loses the local feedback, it just means tomorrow's scheduled digest hasn't
    seen it yet.
    """
    if os.environ.get("CI"):
        return False                      # the workflow commits its own changes
    if not (BASE_DIR / ".git").exists():
        print(f"  ⚠️   {BASE_DIR} is not a git repo — feedback saved locally only.")
        return False

    def git(*a):
        return subprocess.run(["git", "-C", str(BASE_DIR), *a],
                              capture_output=True, text=True)

    tracked = [n for n in SYNC_FILES if (BASE_DIR / n).exists()]
    if not tracked:
        return False
    if not git("diff", "--quiet", "HEAD", "--", *tracked).returncode:
        return False                      # nothing actually changed

    # Commit by pathspec so unrelated staged work can't ride along on a
    # "feedback:" commit — this is the user's working repo, not a bare mirror.
    if git("commit", "-m", message, "--", *tracked).returncode:
        print("  ⚠️   Commit failed — feedback saved locally only.")
        return False
    push = git("push")
    if push.returncode:
        print(f"  ⚠️   Push failed ({push.stderr.strip().splitlines()[-1:]!r:.80}) "
              f"— committed locally, push it when you can.")
        return False

    print("  ☁️   Pushed — tomorrow's digest will use it.")
    return True


# ── Commands ───────────────────────────────────────────────────────────────────
def cmd_reject(ident: str, reason: str = "", push: bool = True):
    import knowledge_graph as kg

    pmid = resolve_to_pmid(ident)
    if not pmid:
        print(f"  ❌  Could not resolve '{ident}' to a PMID.")
        return

    meta = fetch_article(pmid)
    if not meta:
        print(f"  ⚠️   Could not fetch {pmid} from PubMed — recording the rejection,")
        print(f"      but with no title/abstract/MeSH it can't act as a negative example.")

    rec = add_rejection(
        pmid=pmid,
        title=meta.get("title", ""),
        reason=reason,
        abstract=meta.get("abstract", ""),
        mesh_terms=meta.get("mesh_terms", []),
        journal=meta.get("journal", ""),
        authors=meta.get("authors", []),
        year=meta.get("year", ""),
    )

    title = rec["title"] or "(no title)"
    print(f"  👎  Rejected {pmid} — {title[:70]}{'...' if len(title) > 70 else ''}")
    if reason:
        print(f"      Reason: {reason}")

    # Rebuild from both loops rather than patching in place: the graph may be
    # stale or absent, and a reject is only meaningful next to the stars.
    kg.reject_pmid(pmid, paper=rec)
    g = kg.build()
    starred  = sum(1 for n in g["nodes"] if n["type"] == "Paper" and n["status"] == "active")
    rejected = sum(1 for n in g["nodes"] if n["type"] == "Paper" and n["status"] == "rejected")
    print(f"      Blocklisted — will not be surfaced again.")
    print(f"      Knowledge graph: ⭐ {starred} starred vs 👎 {rejected} rejected; "
          f"its MeSH terms and journal now steer future scoring away.")

    if push:
        sync_cloud(f"feedback: 👎 {pmid}" + (f" ({reason})" if reason else ""))


def cmd_unreject(pmid: str, push: bool = True):
    import knowledge_graph as kg

    if not remove_rejection(pmid):
        print(f"  ⚠️   {pmid} was not in the feedback store.")
        return
    kg.build()   # cleanest way to drop it from the negative side
    print(f"  ↩️   {pmid} removed from the blocklist and the negative set.")
    if push:
        sync_cloud(f"feedback: ↩️ un-reject {pmid}")


def cmd_list():
    negs = sorted(negatives(), key=lambda r: r.get("timestamp", ""), reverse=True)
    if not negs:
        print("  No thumbs-downs recorded yet.")
        return
    print(f"\n  👎 {len(negs)} rejected article(s), newest first\n")
    for r in negs:
        print(f"  PMID {r['pmid']}  [{r.get('timestamp','')[:10]}]  {r.get('journal','')}")
        print(f"       {r.get('title','(no title)')[:78]}")
        if r.get("reason"):
            print(f"       Reason: {r['reason']}")
        print()


def cmd_report():
    import knowledge_graph as kg

    negs = negatives()
    if not negs:
        print("\n  👎 No thumbs-downs recorded yet. Nothing to report.\n")
        return

    reasons  = Counter((r.get("reason") or "(no reason given)").strip().lower() for r in negs)
    journals = Counter(r.get("journal", "") for r in negs if r.get("journal"))
    mesh     = Counter(m for r in negs for m in (r.get("mesh_terms") or []))
    dates    = [r.get("timestamp", "")[:10] for r in negs if r.get("timestamp")]

    print(f"\n  👎 Thumbs-down report — {len(negs)} rejected article(s)")
    if dates:
        print(f"     Span: {min(dates)} → {max(dates)}")
    print(f"     Store: {FEEDBACK_FILE}\n")

    print("  Most common reasons")
    for reason, n in reasons.most_common(10):
        print(f"    {n:>3}×  {reason}")

    if journals:
        print("\n  Journals most often rejected")
        for j, n in journals.most_common(5):
            print(f"    {n:>3}×  {j}")

    if mesh:
        print("\n  MeSH terms most common among rejects")
        for m, n in mesh.most_common(8):
            print(f"    {n:>3}×  {m}")

    weights = kg.preference_weights(kg.load_graph())
    away = sorted((kv for kv in weights.items() if kv[1] < 0), key=lambda kv: kv[1])[:8]
    if away:
        print("\n  The graph is now steering away from")
        for cid, w in away:
            print(f"    {w:+.2f}  {cid}")

    usable = sum(1 for r in negs if r.get("abstract") or r.get("mesh_terms"))
    print(f"\n  {len(negs)} PMID(s) blocklisted · {usable} usable as negative examples\n")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    push = "--no-push" not in args
    cmd  = args[0]

    if cmd == "--report":
        cmd_report()
    elif cmd == "--list":
        cmd_list()
    elif cmd == "--unreject" and len(args) > 1:
        cmd_unreject(args[1], push=push)
    elif cmd == "--reject" and len(args) > 1:
        reason = ""
        if "--reason" in args:
            i = args.index("--reason")
            if i + 1 < len(args):
                reason = args[i + 1]
        cmd_reject(args[1], reason, push=push)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
