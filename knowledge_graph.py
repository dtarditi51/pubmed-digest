#!/usr/bin/env python3
"""
Knowledge graph over the papers you've judged — the memory PubMedAgent learns from.

Two sources feed it, and both matter:

  ⭐ knowledge_base.json  starred papers  -> Paper nodes, status: active
  👎 feedback.json        thumbs-downs    -> Paper nodes, status: rejected

  Nodes   Paper      pmid:42098926      status: active | rejected
          MeshTerm   mesh:Heart Failure status: active | rejected
          Journal    journal:Circulation
          Author     author:Jiang Juming

  Edges   pmid:X --has_mesh-->     mesh:Y     status mirrors its source Paper
          pmid:X --published_in--> journal:Y
          pmid:X --authored_by-->  author:Y

Rejected papers are kept in the graph rather than deleted, because a rejection
is knowledge: it tells us which MeSH terms, journals and authors to steer away
from. They are marked `rejected` throughout, are excluded from every "what do I
know" view, and never count as endorsed knowledge — but their edges give the
scorer its negative side (see relevance.py). A concept node reachable only from
rejected papers is itself marked rejected.

Usage:
  python3 knowledge_graph.py --build    # (re)build from knowledge_base + feedback
  python3 knowledge_graph.py --stats    # node/edge counts, what it has learned
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import feedback

BASE_DIR   = Path(__file__).resolve().parent
KB_FILE    = BASE_DIR / "knowledge_base.json"
GRAPH_FILE = BASE_DIR / "knowledge_graph.json"

ACTIVE   = "active"
REJECTED = "rejected"

_TYPE_BY_PREFIX = {"mesh": "MeshTerm", "journal": "Journal", "author": "Author"}


# ── Store ──────────────────────────────────────────────────────────────────────
def load_graph(path: Path = GRAPH_FILE) -> dict:
    if path and path.exists():
        try:
            g = json.loads(path.read_text())
            g.setdefault("nodes", [])
            g.setdefault("edges", [])
            return g
        except Exception:
            pass
    return {"nodes": [], "edges": []}


def save_graph(g: dict, path: Path = GRAPH_FILE):
    g["last_updated"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(g, indent=2, ensure_ascii=False), encoding="utf-8")


def paper_node_id(pmid: str) -> str:
    return f"pmid:{pmid}"


def concept_ids(paper: dict) -> list:
    """The concept-node ids a paper-shaped dict would attach to."""
    ids = [f"mesh:{m}" for m in (paper.get("mesh_terms") or [])]
    if paper.get("journal"):
        ids.append(f"journal:{paper['journal']}")
    ids += [f"author:{a}" for a in (paper.get("authors") or [])]
    return ids


def _concept_edges_for(paper: dict) -> list:
    out = []
    for m in paper.get("mesh_terms") or []:
        out.append((f"mesh:{m}", m, "MeshTerm", "has_mesh"))
    if paper.get("journal"):
        out.append((f"journal:{paper['journal']}", paper["journal"], "Journal", "published_in"))
    for a in paper.get("authors") or []:
        out.append((f"author:{a}", a, "Author", "authored_by"))
    return out


# ── Build ──────────────────────────────────────────────────────────────────────
def build(kb_path: Path = None, graph_path: Path = None,
          feedback_path: Path = None) -> dict:
    """
    Rebuild from both feedback loops. Starred papers land active; thumbs-downed
    papers land rejected — including ones you never starred, which arrive from
    feedback.json (that is where most rejects live: you reject them precisely
    because you would never have starred them).
    """
    kb_path    = kb_path or KB_FILE
    graph_path = graph_path or GRAPH_FILE

    blocked = feedback.blocklist(feedback_path) if feedback_path else feedback.blocklist()
    rejects = feedback.negatives(feedback_path) if feedback_path else feedback.negatives()

    try:
        starred = json.loads(kb_path.read_text()).get("papers", []) if kb_path.exists() else []
    except Exception:
        starred = []

    nodes, edges = {}, []

    def add_paper(paper: dict, status: str):
        pmid = str(paper.get("pmid", ""))
        if not pmid:
            return
        pid = paper_node_id(pmid)
        if pid in nodes:
            return
        node = {
            "id":     pid,
            "type":   "Paper",
            "label":  paper.get("title", ""),
            "status": status,
            "year":   paper.get("year", ""),
        }
        if status == REJECTED:
            node["rejected_at"] = datetime.now().isoformat(timespec="seconds")
        nodes[pid] = node
        for cid, label, ctype, rel in _concept_edges_for(paper):
            if cid not in nodes:
                nodes[cid] = {"id": cid, "type": ctype, "label": label, "status": status}
            elif status == ACTIVE:
                # any endorsement promotes a concept out of rejected-only
                nodes[cid]["status"] = ACTIVE
            edges.append({"src": pid, "rel": rel, "dst": cid, "status": status})

    # Rejected first, so an endorsement later can promote a shared concept.
    for rec in rejects:
        add_paper(rec, REJECTED)
    for paper in starred:
        pmid = str(paper.get("pmid", ""))
        if pmid in blocked:
            continue  # a starred paper you later rejected: the reject wins
        add_paper(paper, ACTIVE)

    g = {"nodes": list(nodes.values()), "edges": edges}
    save_graph(g, graph_path)
    return g


# ── What the graph has learned ─────────────────────────────────────────────────
def preference_weights(g: dict) -> dict:
    """
    Signed weight per concept node:

        w(c) = frac_of_starred_papers_linked(c) - frac_of_rejected_papers_linked(c)

    +1.0  every paper you starred touches it, none you rejected did
    -1.0  every paper you rejected touches it, none you starred did
     0.0  both sides touch it equally — carries no preference information

    This is the graph learning: the concepts your endorsements cluster on rise,
    the ones your rejections cluster on fall, and anything common to both cancels.
    """
    papers = {n["id"]: n for n in g.get("nodes", []) if n["type"] == "Paper"}
    n_pos  = sum(1 for n in papers.values() if n.get("status") == ACTIVE)
    n_neg  = sum(1 for n in papers.values() if n.get("status") == REJECTED)

    pos_links, neg_links = {}, {}
    for e in g.get("edges", []):
        bucket = pos_links if e.get("status") == ACTIVE else neg_links
        bucket.setdefault(e["dst"], set()).add(e["src"])

    weights = {}
    for cid in set(pos_links) | set(neg_links):
        p = len(pos_links.get(cid, ())) / n_pos if n_pos else 0.0
        n = len(neg_links.get(cid, ())) / n_neg if n_neg else 0.0
        w = p - n
        if w:
            weights[cid] = w
    return weights


# ── Incremental updates ────────────────────────────────────────────────────────
def reject_pmid(pmid: str, graph_path: Path = None, paper: dict = None) -> int:
    """
    Flip a paper's node and its edges to rejected, adding it to the graph first
    if it was never starred. A concept left with no active edge becomes rejected
    too. Returns the number of elements changed.
    """
    graph_path = graph_path or GRAPH_FILE
    g   = load_graph(graph_path)
    pid = paper_node_id(str(pmid))
    changed = 0

    known = {n["id"]: n for n in g["nodes"]}
    if pid not in known and paper:
        # never starred — bring it in purely as a negative example
        node = {"id": pid, "type": "Paper", "label": paper.get("title", ""),
                "status": REJECTED, "year": paper.get("year", ""),
                "rejected_at": datetime.now().isoformat(timespec="seconds")}
        g["nodes"].append(node)
        known[pid] = node
        changed += 1
        existing = {(e["src"], e["rel"], e["dst"]) for e in g["edges"]}
        for cid, label, ctype, rel in _concept_edges_for(paper):
            if cid not in known:
                cnode = {"id": cid, "type": ctype, "label": label, "status": REJECTED}
                g["nodes"].append(cnode)
                known[cid] = cnode
                changed += 1
            if (pid, rel, cid) not in existing:
                g["edges"].append({"src": pid, "rel": rel, "dst": cid, "status": REJECTED})
                changed += 1
    else:
        node = known.get(pid)
        if node and node.get("status") != REJECTED:
            node["status"] = REJECTED
            node["rejected_at"] = datetime.now().isoformat(timespec="seconds")
            changed += 1
        for e in g["edges"]:
            if e["src"] == pid and e.get("status") != REJECTED:
                e["status"] = REJECTED
                changed += 1

    if changed:
        _restatus_concepts(g)
        save_graph(g, graph_path)
    return changed


def unreject_pmid(pmid: str, graph_path: Path = None) -> int:
    graph_path = graph_path or GRAPH_FILE
    g   = load_graph(graph_path)
    pid = paper_node_id(str(pmid))
    changed = 0

    for n in g["nodes"]:
        if n["id"] == pid and n.get("status") == REJECTED:
            n["status"] = ACTIVE
            n.pop("rejected_at", None)
            changed += 1
    for e in g["edges"]:
        if e["src"] == pid and e.get("status") == REJECTED:
            e["status"] = ACTIVE
            changed += 1

    if changed:
        _restatus_concepts(g)
        save_graph(g, graph_path)
    return changed


def _restatus_concepts(g: dict) -> None:
    """A concept is active iff at least one active edge reaches it."""
    endorsed = {e["dst"] for e in g["edges"] if e.get("status") == ACTIVE}
    for n in g["nodes"]:
        if n["type"] != "Paper":
            n["status"] = ACTIVE if n["id"] in endorsed else REJECTED


def ingest_paper(paper: dict, graph_path: Path = None,
                 feedback_path: Path = None) -> bool:
    """
    Add a starred paper as endorsed knowledge. Refuses thumbs-downed PMIDs —
    a rejected article never enters the graph as knowledge.
    """
    graph_path = graph_path or GRAPH_FILE
    pmid = str(paper.get("pmid", ""))
    if not pmid:
        return False
    blocked = (feedback.blocklist(feedback_path) if feedback_path
               else feedback.blocklist())
    if pmid in blocked:
        return False

    g   = load_graph(graph_path)
    pid = paper_node_id(pmid)
    known = {n["id"]: n for n in g["nodes"]}
    if pid in known and known[pid].get("status") == ACTIVE:
        return False

    if pid in known:
        known[pid]["status"] = ACTIVE
        known[pid].pop("rejected_at", None)
    else:
        node = {"id": pid, "type": "Paper", "label": paper.get("title", ""),
                "status": ACTIVE, "year": paper.get("year", "")}
        g["nodes"].append(node)
        known[pid] = node

    existing = {(e["src"], e["rel"], e["dst"]): e for e in g["edges"]}
    for cid, label, ctype, rel in _concept_edges_for(paper):
        if cid not in known:
            cnode = {"id": cid, "type": ctype, "label": label, "status": ACTIVE}
            g["nodes"].append(cnode)
            known[cid] = cnode
        key = (pid, rel, cid)
        if key in existing:
            existing[key]["status"] = ACTIVE
        else:
            g["edges"].append({"src": pid, "rel": rel, "dst": cid, "status": ACTIVE})

    _restatus_concepts(g)
    save_graph(g, graph_path)
    return True


# ── CLI ────────────────────────────────────────────────────────────────────────
def cmd_stats(graph_path: Path = None):
    graph_path = graph_path or GRAPH_FILE
    g = load_graph(graph_path)
    if not g["nodes"]:
        print("  Graph is empty. Run: python3 knowledge_graph.py --build")
        return

    papers   = [n for n in g["nodes"] if n["type"] == "Paper"]
    starred  = sum(1 for n in papers if n["status"] == ACTIVE)
    rejected = len(papers) - starred
    by_type  = {}
    for n in g["nodes"]:
        by_type.setdefault(n["type"], 0)
        by_type[n["type"]] += 1

    print(f"\n  🕸  Knowledge graph — {len(g['nodes'])} nodes, {len(g['edges'])} edges")
    for t, c in sorted(by_type.items()):
        print(f"     {t:<10} {c:>4}")
    print(f"\n     ⭐ starred  {starred:>3}")
    print(f"     👎 rejected {rejected:>3}")

    w = preference_weights(g)
    likes    = sorted((kv for kv in w.items() if kv[1] > 0), key=lambda kv: -kv[1])[:8]
    dislikes = sorted((kv for kv in w.items() if kv[1] < 0), key=lambda kv: kv[1])[:8]
    if likes:
        print("\n  Steering toward")
        for cid, val in likes:
            print(f"    +{val:.2f}  {cid}")
    if dislikes:
        print("\n  Steering away from")
        for cid, val in dislikes:
            print(f"    {val:.2f}  {cid}")
    print(f"\n     {graph_path}\n")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
    elif args[0] == "--build":
        g = build()
        papers = sum(1 for n in g["nodes"] if n["type"] == "Paper")
        print(f"  🕸  Built graph: {papers} papers, {len(g['nodes'])} nodes, "
              f"{len(g['edges'])} edges → {GRAPH_FILE}")
    elif args[0] == "--stats":
        cmd_stats()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
