#!/usr/bin/env python3
"""
Two-sided relevance scoring — the learning half of PubMedAgent.

Positive signal  ⭐ starred papers  (knowledge_base.json)
Negative signal  👎 thumbs-downs   (feedback.json)

Both are projected into the knowledge graph, and the graph is what the scorer
reads: a candidate article is compared against the MeSH terms, journals and
authors that your endorsed papers cluster around, and against the ones your
rejects cluster around. Text (title + abstract) is scored the same way, because
freshly-indexed PubMed articles often carry no MeSH terms yet.

The result is a `fit` in [-3, +3], added to the existing 12-point quality
rubric. Quality says how good a paper is; fit says how much it looks like the
papers you keep versus the ones you throw away. As you star and reject more,
both profiles sharpen and the digest hones in.
"""

import json
import math
from collections import Counter
from pathlib import Path

import feedback
import knowledge_graph as kg

BASE_DIR = Path(__file__).resolve().parent

# |fit| thresholds -> points. Signed: positive raw score boosts, negative penalises.
FIT_BANDS = [(0.28, 3), (0.16, 2), (0.06, 1)]
MAX_FIT   = 3

# How much the graph's concept signal (MeSH/journal/author) counts relative to
# raw text. Concepts are the higher-quality signal, but only when the candidate
# actually has them — new articles often don't, and then text carries it alone.
CONCEPT_WEIGHT = 0.5

# Damping reference length: a long abstract shouldn't accumulate a score by
# sheer volume of words.
REF_LEN = 60.0


def _sim(items, profile: dict) -> float:
    """
    How much of `profile`'s total weight this item set touches, damped by size.
    0.0 when the profile is empty or nothing overlaps.
    """
    if not profile or not items:
        return 0.0
    mass = sum(profile.values())
    if mass <= 0:
        return 0.0
    hit = sum(profile.get(i, 0.0) for i in items)
    damp = 1.0 / math.sqrt(max(len(items), 1) / REF_LEN)
    return (hit / mass) * damp


def _split(signed: dict):
    """Split a signed weight map into (positive side, negative side-as-positive)."""
    pos = {k: w for k, w in signed.items() if w > 0}
    neg = {k: -w for k, w in signed.items() if w < 0}
    return pos, neg


def _token_weights(positives: list, negatives: list) -> dict:
    """
    Signed per-token weight:
        w(t) = frac_of_starred_containing(t) - frac_of_rejected_containing(t)

    A token both sides share nets out near zero, so shared clinical vocabulary
    ("patients", "cardiac") can't drive the score in either direction. Only
    vocabulary that discriminates does.
    """
    pos_df, neg_df = Counter(), Counter()
    for rec in positives:
        for t in feedback.doc_tokens(rec):
            pos_df[t] += 1
    for rec in negatives:
        for t in feedback.doc_tokens(rec):
            neg_df[t] += 1

    n_pos, n_neg = len(positives), len(negatives)
    weights = {}
    for t in set(pos_df) | set(neg_df):
        w = (pos_df[t] / n_pos if n_pos else 0.0) - (neg_df[t] / n_neg if n_neg else 0.0)
        if w:
            weights[t] = w
    return weights


def build_profile(kb_path: Path = None, feedback_path: Path = None,
                  graph_path: Path = None) -> dict:
    """Assemble the learned profile from the graph plus the starred/rejected text."""
    kb_path       = kb_path or kg.KB_FILE
    feedback_path = feedback_path or feedback.FEEDBACK_FILE
    graph_path    = graph_path or kg.GRAPH_FILE

    try:
        papers = json.loads(kb_path.read_text()).get("papers", []) if kb_path.exists() else []
    except Exception:
        papers = []

    blocked   = feedback.blocklist(feedback_path)
    negatives = feedback.negatives(feedback_path)
    positives = [p for p in papers if str(p.get("pmid")) not in blocked]

    concepts = kg.preference_weights(kg.load_graph(graph_path))
    tokens   = _token_weights(positives, negatives)

    return {
        "concepts":  concepts,
        "tokens":    tokens,
        "n_starred": len(positives),
        "n_rejected": len(negatives),
    }


def fit_score(paper: dict, profile: dict) -> int:
    """
    -3 .. +3. Positive means "looks like what you star", negative means "looks
    like what you thumbs-down", 0 means the profile has nothing to say.
    """
    if not profile:
        return 0

    tok_pos, tok_neg = _split(profile.get("tokens", {}))
    con_pos, con_neg = _split(profile.get("concepts", {}))

    toks     = feedback.doc_tokens(paper)
    text_fit = _sim(toks, tok_pos) - _sim(toks, tok_neg)

    con_ids = kg.concept_ids(paper)
    if con_ids:
        con_fit = _sim(con_ids, con_pos) - _sim(con_ids, con_neg)
        raw = (1 - CONCEPT_WEIGHT) * text_fit + CONCEPT_WEIGHT * con_fit
    else:
        raw = text_fit

    sign = 1 if raw >= 0 else -1
    for threshold, points in FIT_BANDS:
        if abs(raw) >= threshold:
            return sign * min(points, MAX_FIT)
    return 0


def explain(paper: dict, profile: dict) -> str:
    """Human-readable reason for a fit score — used by the report command."""
    con_ids = kg.concept_ids(paper)
    concepts = profile.get("concepts", {})
    hits = sorted(
        ((c, concepts[c]) for c in con_ids if c in concepts and concepts[c]),
        key=lambda kv: abs(kv[1]), reverse=True,
    )[:4]
    if not hits:
        return "no learned concepts in common"
    return ", ".join(
        f"{'+' if w > 0 else '−'}{c.split(':', 1)[1]}" for c, w in hits
    )
