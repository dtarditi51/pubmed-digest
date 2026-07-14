#!/usr/bin/env python3
"""
PubMed Morning Digest
Queries 10 cardiology/medicine topics, scores papers by quality rubric,
deduplicates against a 60-day rolling window, and generates an HTML digest.

Usage: python3 pubmed_digest.py [--lookback N]
  --lookback N   fetch articles from last N days (default: 2)
"""

import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date
from pathlib import Path

import feedback
import knowledge_graph
import relevance

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE  = BASE_DIR / ".env.local"
SEEN_FILE = BASE_DIR / "seen_pmids.json"
KB_FILE   = BASE_DIR / "knowledge_base.json"
DIGESTS_DIR = BASE_DIR / "digests"

# ── Load .env.local ────────────────────────────────────────────────────────────
def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

env = load_env(ENV_FILE)
NCBI_API_KEY = env.get("NCBI_API_KEY") or os.environ.get("NCBI_API_KEY", "")
DELAY = 0.15 if NCBI_API_KEY else 0.4   # NCBI rate limit: 10/s with key, 3/s without

# ── Topics ─────────────────────────────────────────────────────────────────────
TOPICS = [
    {
        "name": "AI in Medicine",
        "query": (
            '("artificial intelligence"[MeSH Terms] OR "machine learning"[MeSH Terms] '
            'OR "deep learning"[All Fields] OR "large language model"[All Fields] '
            'OR "neural network, computer"[MeSH Terms]) AND '
            '("medicine"[All Fields] OR "clinical"[All Fields] OR "patient care"[All Fields])'
        ),
    },
    {
        "name": "Preventive Cardiology",
        "query": (
            '"preventive cardiology"[All Fields] OR "cardiovascular prevention"[All Fields] '
            'OR ("cardiovascular diseases"[MeSH Terms] AND "prevention and control"[Subheading])'
        ),
    },
    {
        "name": "Quality & Value in Cardiology",
        "query": (
            '("quality improvement"[All Fields] OR "value-based care"[All Fields] '
            'OR "quality of care"[MeSH Terms] OR "appropriateness criteria"[All Fields]) '
            'AND ("cardiology"[All Fields] OR "cardiovascular"[All Fields])'
        ),
    },
    {
        "name": "Private Equity in Medicine",
        "query": (
            '"private equity"[All Fields] AND '
            '("medicine"[All Fields] OR "cardiology"[All Fields] '
            'OR "healthcare"[All Fields] OR "hospital"[All Fields] '
            'OR "physician practice"[All Fields])'
        ),
    },
    {
        "name": "EHR Improvements",
        "query": (
            '("electronic health records"[MeSH Terms] OR "electronic medical records"[All Fields] '
            'OR "EHR"[Title/Abstract] OR "EMR"[Title/Abstract]) AND '
            '("improvement"[All Fields] OR "implementation"[All Fields] '
            'OR "interoperability"[All Fields] OR "usability"[All Fields] OR "burnout"[All Fields])'
        ),
    },
    {
        "name": "AI & LLMs for Cardiology",
        "query": (
            '("large language model"[All Fields] OR "generative artificial intelligence"[All Fields] '
            'OR "ChatGPT"[All Fields] OR "GPT-4"[All Fields] OR "foundation model"[All Fields] '
            'OR "Claude"[All Fields]) AND '
            '("cardiology"[All Fields] OR "cardiovascular"[All Fields] OR "cardiac"[All Fields])'
        ),
    },
    {
        "name": "Heart Failure in Adults",
        "query": (
            '("heart failure"[MeSH Terms] OR "congestive heart failure"[All Fields]) AND '
            '("adult"[MeSH Terms] OR "randomized controlled trial"[Publication Type] '
            'OR "treatment"[All Fields] OR "management"[All Fields] OR "outcomes"[All Fields])'
        ),
    },
    {
        "name": "Lipidology",
        "query": (
            '"lipids"[MeSH Terms] OR "dyslipidemias"[MeSH Terms] '
            'OR "cholesterol, LDL"[MeSH Terms] OR "statin"[All Fields] '
            'OR "PCSK9"[All Fields] OR "inclisiran"[All Fields] '
            'OR "ezetimibe"[All Fields] OR "triglycerides"[MeSH Terms] '
            'OR "familial hypercholesterolemia"[All Fields]'
        ),
    },
    {
        "name": "Hypertension",
        "query": (
            '"hypertension"[MeSH Terms] AND '
            '("drug therapy"[Subheading] OR "prevention and control"[Subheading] '
            'OR "therapy"[Subheading] OR "diagnosis"[Subheading] OR "epidemiology"[Subheading])'
        ),
    },
    {
        "name": "Atrial Fibrillation",
        "query": (
            '"atrial fibrillation"[MeSH Terms] AND '
            '("therapy"[Subheading] OR "drug therapy"[Subheading] '
            'OR "ablation"[All Fields] OR "anticoagulant"[All Fields] '
            'OR "cardioversion"[All Fields] OR "rhythm control"[All Fields] '
            'OR "rate control"[All Fields])'
        ),
    },
]

# Feedback is captured as GitHub Issues so the ⭐/👎 buttons work from any device
# (a Pages digest on your phone cannot reach a server on your Mac). A workflow
# ingests the issue, updates the graph, and closes it.
GH_REPO = "dtarditi51/pubmed-digest"

# One-click feedback: the buttons POST here and a Vercel function opens the
# issue server-side (feedback-api/). If the endpoint errors — token missing,
# Vercel down — the page falls back to opening the prefilled issue form.
FEEDBACK_API = "https://pubmed-feedback.vercel.app/api/feedback"

MAX_PER_TOPIC   = 3
LOOKBACK_DAYS   = 2   # fetch last 2 days to catch overnight indexing lag
DEDUP_DAYS      = 60  # rolling dedup window

# ── Journal tiers ──────────────────────────────────────────────────────────────
TIER3 = [
    "n engl j med", "new england journal of medicine",
    "lancet", "the lancet", "lancet cardiol", "lancet cardiology",
    "lancet digit health",
    "jama", "jama cardiol", "jama cardiology",
    "bmj", "british medical journal",
    "nat med", "nature medicine",
    "j am coll cardiol", "journal of the american college of cardiology",
    "circulation",
    "eur heart j", "european heart journal",
    "jacc heart fail",
    "jacc cardiovasc imaging", "jacc cardiovasc interv",
    "jacc clin electrophysiol",
    "circ heart fail", "circulation heart failure",
    "circ cardiovasc qual outcomes",
    "npj digit med", "npj digital medicine",
    "nejm evid", "nejm evidence",
]

TIER2 = [
    "am heart j", "american heart journal",
    "heart",
    "j am heart assoc", "journal of the american heart association",
    "cardiovasc res", "cardiovascular research",
    "eur j heart fail", "european journal of heart failure",
    "heart fail",
    "hypertension",
    "arterioscler thromb vasc biol",
    "j am soc echocardiogr",
    "europace",
    "heart rhythm",
    "j card fail", "journal of cardiac failure",
    "j hypertens", "journal of hypertension",
    "atherosclerosis",
    "ann intern med", "annals of internal medicine",
    "jmir",
    "circ arrhythm electrophysiol",
    "circ cardiovasc imaging",
    "circ cardiovasc interv",
    "am j cardiol", "american journal of cardiology",
    "int j cardiol",
    "clin cardiol",
    "j clin lipidol",
    "open heart",
    "heart lung circ",
]

def journal_tier(name: str) -> int:
    n = name.lower().strip()
    for t3 in TIER3:
        if t3 in n or n in t3:
            return 3
    for t2 in TIER2:
        if t2 in n or n in t2:
            return 2
    return 1

# ── Study design detection ─────────────────────────────────────────────────────
DESIGN_PATTERNS = [
    (4, r"\b(randomized controlled trial|randomised controlled trial|rct)\b", "RCT"),
    (4, r"\bmeta.?analysis\b",                                                "Meta-analysis"),
    (3, r"\bsystematic review\b",                                             "Systematic Review"),
    (3, r"\bmendelian randomization\b",                                       "Mendelian Randomization"),
    (2, r"\b(prospective cohort|prospective study|prospective observational)\b","Prospective Cohort"),
    (2, r"\b(retrospective cohort|retrospective study|retrospective analysis)\b","Retrospective Cohort"),
    (2, r"\b(registry|registries)\b",                                         "Registry Study"),
    (2, r"\bcase.?control\b",                                                 "Case-Control"),
    (2, r"\bcross.?sectional\b",                                              "Cross-sectional"),
    (1, r"\b(case series|case report)\b",                                     "Case Series/Report"),
    (1, r"\b(narrative review|literature review)\b",                          "Narrative Review"),
    (1, r"\b(review article|review)\b",                                       "Review"),
]

def detect_design(text: str) -> tuple:
    t = text.lower()
    for score, pattern, label in DESIGN_PATTERNS:
        if re.search(pattern, t):
            return score, label
    return 1, "Observational/Other"

# ── Clinical applicability ─────────────────────────────────────────────────────
LOW_SIGNALS = [
    r"\b(in vitro|in vivo animal|mouse model|rat model|murine|cell line)\b",
    r"\b(basic science|molecular mechanism|genetic pathway)\b",
]
HIGH_SIGNALS = [
    r"\b(mortality|survival|morbidity|major adverse|endpoint|outcome)\b",
    r"\b(reduce|reduction|decrease|improve|improvement|benefit)\b",
    r"\b(practice guideline|guideline|clinical recommendation)\b",
    r"\b(randomized|clinical trial|intervention)\b",
    r"\b(hospitaliz|readmission|rehospitaliz)\b",
]

def applicability_score(title: str, abstract: str) -> int:
    t = (title + " " + abstract).lower()
    for pat in LOW_SIGNALS:
        if re.search(pat, t):
            return 0
    return min(sum(1 for pat in HIGH_SIGNALS if re.search(pat, t)), 3)

# ── Sample size ────────────────────────────────────────────────────────────────
SIZE_PATTERNS = [
    r'\bn\s*=\s*([\d,]+)',
    r'([\d,]+)\s+patients',
    r'([\d,]+)\s+participants',
    r'([\d,]+)\s+subjects',
    r'enrolled\s+([\d,]+)',
    r'included\s+([\d,]+)',
    r'total\s+of\s+([\d,]+)',
]

def size_score(abstract: str) -> int:
    for pat in SIZE_PATTERNS:
        m = re.search(pat, abstract, re.IGNORECASE)
        if m:
            try:
                n = int(m.group(1).replace(",", ""))
                if n >= 10_000: return 2
                if n >= 500:    return 1
                return 0
            except ValueError:
                pass
    return 1  # unknown — give benefit of the doubt

# ── Composite scorer ───────────────────────────────────────────────────────────
def score_paper(paper: dict, profile: dict = None) -> dict:
    """
    Quality rubric (12 pts) plus a learned fit (-3..+3).

    Quality says how good a paper is. Fit says how much it looks like the papers
    you star versus the ones you thumbs-down — it is read off the knowledge
    graph, so it sharpens every time you do either.
    """
    title    = paper.get("title", "")
    abstract = paper.get("abstract", "")
    journal  = paper.get("journal", "")
    combined = title + " " + abstract

    jt          = journal_tier(journal)
    ds, dl      = detect_design(combined)
    appl        = applicability_score(title, abstract)
    sz          = size_score(abstract)

    quality = jt + ds + appl + sz   # max = 3+4+3+2 = 12
    fit     = relevance.fit_score(paper, profile or {})
    total   = max(quality + fit, 0)

    return {
        "total":              total,
        "max":                15,
        "pct":                round(total / 15 * 100),
        "journal_tier":       jt,
        "study_design":       dl,
        "study_design_score": ds,
        "applicability":      appl,
        "sample_size_score":  sz,
        "quality":            quality,
        "fit":                fit,
    }

# ── PubMed API helpers ─────────────────────────────────────────────────────────
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

def _params(**kw) -> str:
    p = {"retmode": "xml", "db": "pubmed"}
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    p.update(kw)
    return urllib.parse.urlencode(p)

def _get(url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  [fetch error] {exc}")
                return ""
    return ""

def search_pmids(query: str, lookback: int) -> list:
    mindate = (date.today() - timedelta(days=lookback)).strftime("%Y/%m/%d")
    maxdate = date.today().strftime("%Y/%m/%d")
    url = EUTILS + "esearch.fcgi?" + _params(
        term=query, retmax=50, sort="relevance",
        mindate=mindate, maxdate=maxdate, datetype="edat",
    )
    xml = _get(url)
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
        return [el.text for el in root.findall(".//IdList/Id") if el.text]
    except ET.ParseError:
        return []
    finally:
        time.sleep(DELAY)

def fetch_papers(pmids: list) -> list:
    if not pmids:
        return []
    url = EUTILS + "efetch.fcgi?" + _params(id=",".join(pmids), rettype="abstract")
    xml = _get(url)
    if not xml:
        return []
    papers = []
    try:
        root = ET.fromstring(xml)
        for article in root.findall(".//PubmedArticle"):
            try:
                papers.append(_parse(article))
            except Exception:
                pass
    except ET.ParseError:
        pass
    time.sleep(DELAY)
    return papers

def _parse(article) -> dict:
    pmid  = article.findtext(".//PMID", "")
    title = re.sub(r"<[^>]+>", "", article.findtext(".//ArticleTitle", ""))

    abstract_parts = article.findall(".//AbstractText")
    abstract = " ".join(
        (f"{el.get('Label')}: " if el.get("Label") else "") + (el.text or "")
        for el in abstract_parts
    ).strip()

    authors = []
    for au in article.findall(".//Author"):
        last = au.findtext("LastName", "")
        fore = au.findtext("ForeName", "") or au.findtext("Initials", "")
        if last:
            authors.append(f"{last} {fore}".strip())

    journal = (article.findtext(".//Journal/Title")
               or article.findtext(".//ISOAbbreviation", ""))

    year = (article.findtext(".//PubDate/Year")
            or article.findtext(".//PubDate/MedlineDate", "")[:4])

    doi = next(
        (el.text for el in article.findall(".//ArticleId") if el.get("IdType") == "doi"),
        ""
    )

    pub_types = [pt.text for pt in article.findall(".//PublicationType") if pt.text]

    mesh = [m.findtext("DescriptorName", "") for m in article.findall(".//MeshHeading")]

    return dict(pmid=pmid, title=title, abstract=abstract,
                authors=authors, journal=journal, year=year,
                doi=doi, pub_types=pub_types,
                mesh_terms=[m for m in mesh if m])

# ── Seen-PMIDs store ───────────────────────────────────────────────────────────
def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except Exception:
            pass
    return {}

def save_seen(seen: dict):
    cutoff = (date.today() - timedelta(days=DEDUP_DAYS)).isoformat()
    pruned = {pid: d for pid, d in seen.items() if d >= cutoff}
    SEEN_FILE.write_text(json.dumps(pruned, indent=2))

def filter_candidates(pmids: list, seen, blocked) -> list:
    """
    Drop already-seen and thumbs-downed PMIDs, preserving relevance order.

    `seen` is pruned on a 60-day window, so it can't carry the blocklist: a
    rejected PMID would silently become eligible again two months later.
    `blocked` is never pruned.
    """
    return [p for p in pmids if p not in seen and p not in blocked]

# ── HTML generation ────────────────────────────────────────────────────────────
def _badge_color(pct: int) -> str:
    if pct < 50: return "#dc2626"
    if pct < 70: return "#ea580c"
    if pct < 85: return "#d97706"
    return "#16a34a"

def _issue_url(kind: str, paper: dict) -> str:
    """
    Prefilled GitHub Issue link. Tapping it opens the issue form already filled
    in — one more tap submits, and the feedback workflow does the rest. Works
    the same on a Mac and on an iPhone, with nothing running locally.
    """
    pmid  = paper.get("pmid", "")
    title = (paper.get("title", "") or "")[:90]
    if kind == "star":
        subject = f"⭐ star {pmid}"
        body    = f"> {title}\n\nStarred from the digest. Add notes below (optional).\n"
        label   = "star"
    else:
        subject = f"👎 reject {pmid}"
        body    = (f"> {title}\n\nReason (optional, one line — it shows up in "
                   f"`feedback.py --report`):\n")
        label   = "reject"
    q = urllib.parse.urlencode({"title": subject, "body": body, "labels": label})
    return f"https://github.com/{GH_REPO}/issues/new?{q}"

def _paper_card(paper: dict, rank: int) -> str:
    sc    = paper["_score"]
    pct   = sc["pct"]
    color = _badge_color(pct)
    authors = paper.get("authors", [])
    auth_str = (", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")) or "Authors not available"

    pm_url  = f"https://pubmed.ncbi.nlm.nih.gov/{paper['pmid']}/"
    doi_url = f"https://doi.org/{paper['doi']}" if paper.get("doi") else ""

    abstract = paper.get("abstract", "No abstract available.")
    abstract_disp = (abstract[:420] + "...") if len(abstract) > 420 else abstract

    stars = "★" * sc["journal_tier"] + "☆" * (3 - sc["journal_tier"])

    doi_btn = (f'<a href="{doi_url}" target="_blank" class="btn btn-doi">Full Text</a>'
               if doi_url else "")

    star_url = html.escape(_issue_url("star", paper), quote=True)
    down_url = html.escape(_issue_url("reject", paper), quote=True)
    data_title = html.escape((paper.get("title", "") or "")[:90], quote=True)

    fit = sc.get("fit", 0)
    fit_chip = ""
    if fit > 0:
        fit_chip = (f'<span class="fit-pos" title="Looks like papers you star '
                    f'(learned from the knowledge graph)">⭐ +{fit}</span>')
    elif fit < 0:
        fit_chip = (f'<span class="fit-neg" title="Looks like papers you thumbs-down '
                    f'(learned from the knowledge graph)">👎 {fit}</span>')

    return f"""
<div class="card" id="p{paper['pmid']}">
  <div class="card-top">
    <div class="rank">#{rank}</div>
    <div class="meta">
      <span class="journal">{paper.get('journal','')}</span>
      <span class="stars" title="Journal tier">{stars}</span>
      <span class="yr">{paper.get('year','')}</span>
      <span class="design">{sc['study_design']}</span>
    </div>
    <span class="badge" style="background:{color}">{pct}%</span>
  </div>
  <h3><a href="{pm_url}" target="_blank">{paper.get('title','')}</a></h3>
  <div class="authors">{auth_str}</div>
  <div class="abstract">{abstract_disp}</div>
  <div class="card-bot">
    <div class="breakdown">
      <span title="Journal tier (max 3)">📰 {sc['journal_tier']}/3</span>
      <span title="Study design (max 4)">🔬 {sc['study_design_score']}/4</span>
      <span title="Clinical applicability (max 3)">🏥 {sc['applicability']}/3</span>
      <span title="Sample size (max 2)">👥 {sc['sample_size_score']}/2</span>
      {fit_chip}
    </div>
    <div class="btns">
      <a href="{pm_url}" target="_blank" class="btn btn-pm">PubMed</a>
      {doi_btn}
      <button type="button" class="btn btn-save" data-kind="star" data-pmid="{paper['pmid']}" data-title="{data_title}" data-fallback="{star_url}">⭐ Star</button>
      <button type="button" class="btn btn-down" data-kind="reject" data-pmid="{paper['pmid']}" data-title="{data_title}" data-fallback="{down_url}">👎 Not relevant</button>
    </div>
  </div>
</div>"""

def _topic_section(name: str, papers: list) -> str:
    if not papers:
        return f'<div class="section empty"><h2>{name}</h2><p class="none">No new papers today.</p></div>'
    cards = "".join(_paper_card(p, i + 1) for i, p in enumerate(papers))
    return f"""
<div class="section">
  <h2>{name} <span class="cnt">{len(papers)} new</span></h2>
  {cards}
</div>"""

# Plain string (not an f-string) so the JS braces don't need doubling; the
# endpoint URL is substituted below. One tap POSTs the feedback, flips the
# button to a confirmed state, and remembers it in localStorage so it survives
# reloads. Any failure falls back to the old prefilled-issue-form flow.
FEEDBACK_JS = """
<script>
(function () {
  var API = "__API__";
  var KEY = "pubmed-feedback";
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}

  function finish(btn, kind) {
    btn.textContent = kind === "star" ? "⭐ Starred" : "👎 Noted";
    btn.classList.add("done");
    btn.closest(".btns").querySelectorAll("button[data-kind]").forEach(function (b) {
      b.disabled = true;
    });
  }

  document.querySelectorAll("button[data-kind]").forEach(function (b) {
    if (saved[b.dataset.pmid] === b.dataset.kind) finish(b, b.dataset.kind);
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-kind]");
    if (!btn || btn.disabled) return;
    var kind = btn.dataset.kind, pmid = btn.dataset.pmid, orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = kind === "star" ? "⭐ …" : "👎 …";
    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: kind, pmid: pmid, title: btn.dataset.title })
    }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      saved[pmid] = kind;
      try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
      finish(btn, kind);
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = orig;
      window.open(btn.dataset.fallback, "_blank");
    });
  });
})();
</script>""".replace("__API__", FEEDBACK_API)

def build_html(results: dict) -> str:
    total   = sum(len(v) for v in results.values())
    active  = sum(1 for v in results.values() if v)
    run_dt  = datetime.now().strftime("%A, %B %d, %Y  %I:%M %p")
    sections = "".join(_topic_section(n, p) for n, p in results.items())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PubMed Digest {datetime.now().strftime('%Y-%m-%d')}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f4f8;color:#1a202c;line-height:1.6}}

.hdr{{background:linear-gradient(135deg,#1e3a5f,#2d6a9f);color:#fff;padding:28px 40px;position:sticky;top:0;z-index:100;box-shadow:0 2px 12px rgba(0,0,0,.2)}}
.hdr h1{{font-size:22px;font-weight:700;letter-spacing:-.3px}}
.hdr .sub{{opacity:.75;font-size:13px;margin-top:3px}}
.stats{{display:flex;gap:28px;margin-top:14px}}
.stat .n{{font-size:26px;font-weight:700}}
.stat .l{{font-size:11px;opacity:.65;text-transform:uppercase;letter-spacing:.5px}}

.wrap{{max-width:880px;margin:0 auto;padding:28px 20px}}

.section{{margin-bottom:36px}}
.section.empty{{opacity:.45}}
.section h2{{font-size:18px;font-weight:700;color:#1e3a5f;padding-bottom:7px;border-bottom:2px solid #2d6a9f;margin-bottom:14px;display:flex;align-items:center;gap:10px}}
.cnt{{background:#2d6a9f;color:#fff;font-size:11px;padding:2px 9px;border-radius:20px;font-weight:600}}
.none{{color:#6b7280;font-style:italic;font-size:13px}}

.card{{background:#fff;border-radius:10px;padding:18px 22px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.07);border-left:4px solid #2d6a9f;transition:box-shadow .2s}}
.card:hover{{box-shadow:0 4px 16px rgba(0,0,0,.11)}}

.card-top{{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}}
.rank{{width:26px;height:26px;background:#e8f0fe;color:#1e3a5f;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0}}
.meta{{display:flex;align-items:center;gap:7px;flex:1;flex-wrap:wrap}}
.journal{{font-weight:600;color:#374151;font-size:13px}}
.stars{{color:#f59e0b;font-size:11px}}
.yr{{color:#6b7280;font-size:12px}}
.design{{background:#eff6ff;color:#1d4ed8;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:500}}
.badge{{color:#fff;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;white-space:nowrap}}

.card h3{{font-size:15px;font-weight:600;margin-bottom:5px;line-height:1.4}}
.card h3 a{{color:#1e3a5f;text-decoration:none}}
.card h3 a:hover{{text-decoration:underline;color:#2d6a9f}}
.authors{{color:#6b7280;font-size:12px;margin-bottom:8px}}
.abstract{{font-size:13px;color:#374151;margin-bottom:12px}}

.card-bot{{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}}
.breakdown{{display:flex;gap:10px;font-size:12px;color:#6b7280}}
.breakdown span{{cursor:help}}
.btns{{display:flex;gap:7px}}

.btn{{padding:5px 12px;border-radius:5px;font-size:12px;font-weight:500;cursor:pointer;border:none;text-decoration:none;display:inline-flex;align-items:center;gap:3px}}
.btn-pm{{background:#eff6ff;color:#1d4ed8}}
.btn-pm:hover{{background:#dbeafe}}
.btn-doi{{background:#f0fdf4;color:#166534}}
.btn-doi:hover{{background:#dcfce7}}
.btn-save{{background:#fefce8;color:#92400e}}
.btn-save:hover{{background:#fef9c3}}
.btn-down{{background:#fef2f2;color:#991b1b}}
.btn-down:hover{{background:#fee2e2}}
.fit-pos{{color:#15803d;font-weight:600}}
.fit-neg{{color:#b91c1c;font-weight:600}}

.btn:disabled{{opacity:.55;cursor:default}}
.btn.done{{opacity:1;font-weight:700}}
.btn-save.done{{background:#fde68a;color:#78350f}}
.btn-down.done{{background:#fecaca;color:#7f1d1d}}

footer{{text-align:center;padding:28px;color:#9ca3af;font-size:12px}}
footer code{{background:#f3f4f6;padding:2px 6px;border-radius:3px}}
</style>
</head>
<body>

<div class="hdr">
  <h1>🫀 PubMed Morning Digest</h1>
  <div class="sub">{run_dt} &nbsp;·&nbsp; Scored by study design · journal tier · clinical applicability · sample size</div>
  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="l">New Papers</div></div>
    <div class="stat"><div class="n">{active}/10</div><div class="l">Active Topics</div></div>
    <div class="stat"><div class="n">~5 min</div><div class="l">Read Time</div></div>
  </div>
</div>

<div class="wrap">
  {sections}
</div>

<footer>
  Generated {run_dt} &nbsp;·&nbsp; PubMedAgent &nbsp;·&nbsp; Tap ⭐ Star or 👎 Not relevant on any card — the graph learns from both
</footer>

{FEEDBACK_JS}
</body>
</html>"""

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Parse --lookback flag
    lookback = LOOKBACK_DAYS
    args = sys.argv[1:]
    if "--lookback" in args:
        idx = args.index("--lookback")
        if idx + 1 < len(args):
            try:
                lookback = int(args[idx + 1])
            except ValueError:
                pass

    # Both feedback loops. Blocklisted PMIDs never resurface; the profile learned
    # from ⭐ stars and 👎 rejects re-ranks everything else.
    knowledge_graph.build()          # refresh the graph from the latest feedback
    blocked = feedback.blocklist()
    profile = relevance.build_profile()

    print(f"\n🫀  PubMed Morning Digest — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"    API key: {'✓ present' if NCBI_API_KEY else '✗ missing (rate limited to 3 req/s)'}")
    print(f"    Lookback: {lookback} day(s)  |  Max per topic: {MAX_PER_TOPIC}")
    print(f"    Learned from: ⭐ {profile['n_starred']} starred  |  "
          f"👎 {profile['n_rejected']} rejected  "
          f"({len(profile['concepts'])} graph concepts, {len(blocked)} blocklisted)\n")

    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)

    seen    = load_seen()
    results = {}
    all_new = []
    blocked_hits = 0

    for topic in TOPICS:
        name  = topic["name"]
        query = topic["query"]
        print(f"  🔍 {name:<35}", end=" ", flush=True)

        try:
            pmids = search_pmids(query, lookback)
            hits  = sum(1 for p in pmids if p in blocked)
            blocked_hits += hits
            novel = filter_candidates(pmids, seen, blocked)
            print(f"{len(pmids):>3} found  {len(novel):>3} new", end="  ", flush=True)
            if hits:
                print(f"({hits} blocked)", end="  ", flush=True)

            if not novel:
                results[name] = []
                print()
                continue

            papers = fetch_papers(novel[:25])
            for p in papers:
                p["_score"] = score_paper(p, profile)
            papers.sort(key=lambda p: p["_score"]["total"], reverse=True)

            top = papers[:MAX_PER_TOPIC]
            results[name] = top
            all_new.extend(p["pmid"] for p in papers)
            print(f"→ showing {len(top)}")

        except Exception as exc:
            print(f"ERROR: {exc}")
            results[name] = []

    # Mark all fetched PMIDs as seen
    today = date.today().isoformat()
    for pid in all_new:
        seen.setdefault(pid, today)
    save_seen(seen)

    # Write HTML
    html      = build_html(results)
    date_str  = datetime.now().strftime("%Y-%m-%d")
    out_path  = DIGESTS_DIR / f"digest_{date_str}.html"
    latest    = BASE_DIR / "digest_latest.html"

    out_path.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")

    total = sum(len(v) for v in results.values())
    print(f"\n  ✅ {total} papers across {sum(1 for v in results.values() if v)} topics")
    if blocked_hits:
        print(f"  👎 {blocked_hits} blocklisted hit(s) filtered out")
    print(f"  📄 Digest: {latest}\n")


if __name__ == "__main__":
    main()
