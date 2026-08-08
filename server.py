#!/usr/bin/env python3
"""
PubMed Agent local server — http://localhost:8765
Serves the daily digest and handles one-click saves to knowledge_base.json.

Start:  python3 ~/dev/PubMedAgent/server.py
Stop:   Ctrl+C

Install to run automatically on login:
  cp ~/dev/PubMedAgent/com.danieltarditi.pubmedagent.server.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.danieltarditi.pubmedagent.server.plist
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import feedback
import knowledge_graph

PORT     = 8765
BASE_DIR = Path(__file__).resolve().parent
KB_FILE  = BASE_DIR / "knowledge_base.json"
ENV_FILE = BASE_DIR / ".env.local"

# ── Env ────────────────────────────────────────────────────────────────────────
def _load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

_env = _load_env(ENV_FILE)
NCBI_API_KEY = _env.get("NCBI_API_KEY", "")
DELAY = 0.15 if NCBI_API_KEY else 0.4

# ── PubMed fetch ───────────────────────────────────────────────────────────────
def _fetch_paper(pmid):
    params = {"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"}
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")

def _parse(xml_text, pmid):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    article = root.find(".//PubmedArticle")
    if not article:
        return None

    title    = re.sub(r"<[^>]+>", "", article.findtext(".//ArticleTitle", ""))
    parts    = article.findall(".//AbstractText")
    abstract = " ".join(
        (f"{el.get('Label')}: " if el.get("Label") else "") + (el.text or "")
        for el in parts
    ).strip()

    authors = []
    for au in article.findall(".//Author"):
        last = au.findtext("LastName", "")
        fore = au.findtext("ForeName", "") or au.findtext("Initials", "")
        if last:
            authors.append(f"{last} {fore}".strip())

    journal = article.findtext(".//Journal/Title") or article.findtext(".//ISOAbbreviation", "")
    year    = (article.findtext(".//PubDate/Year")
               or article.findtext(".//PubDate/MedlineDate", "")[:4])
    doi     = next(
        (el.text for el in article.findall(".//ArticleId") if el.get("IdType") == "doi"), ""
    )
    mesh      = [m.findtext("DescriptorName", "") for m in article.findall(".//MeshHeading")]
    pub_types = [pt.text for pt in article.findall(".//PublicationType") if pt.text]

    return {
        "pmid":       pmid,
        "title":      title,
        "authors":    authors,
        "journal":    journal,
        "year":       year,
        "abstract":   abstract,
        "doi":        doi,
        "doi_url":    f"https://doi.org/{doi}" if doi else "",
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "mesh_terms": [m for m in mesh if m],
        "pub_types":  pub_types,
        "saved_date": date.today().isoformat(),
        "notes":      "",
        "tags":       [],
    }

# ── Knowledge base I/O ─────────────────────────────────────────────────────────
def _load_kb():
    if KB_FILE.exists():
        try:
            return json.loads(KB_FILE.read_text()).get("papers", [])
        except Exception:
            pass
    return []

def _write_kb(papers):
    KB_FILE.write_text(
        json.dumps({"papers": papers, "last_updated": date.today().isoformat()}, indent=2),
        encoding="utf-8",
    )

def save_pmids(pmids, note=""):
    """Fetch and save a list of PMIDs. Returns list of result dicts."""
    papers   = _load_kb()
    existing = {p["pmid"] for p in papers}
    blocked  = feedback.blocklist()
    results  = []

    for pmid in pmids:
        pmid = str(pmid).strip()
        if pmid in blocked:
            results.append({"pmid": pmid, "status": "blocked",
                            "error": "thumbs-downed — not ingested"})
            continue
        if pmid in existing:
            results.append({"pmid": pmid, "status": "already_saved",
                            "title": next(p["title"] for p in papers if p["pmid"] == pmid)})
            continue
        try:
            xml  = _fetch_paper(pmid)
            data = _parse(xml, pmid)
            if data:
                if note:
                    data["notes"] = note
                papers.append(data)
                existing.add(pmid)
                knowledge_graph.ingest_paper(data)
                results.append({"pmid": pmid, "status": "saved", "title": data["title"]})
            else:
                results.append({"pmid": pmid, "status": "error", "error": "parse failed"})
        except Exception as exc:
            results.append({"pmid": pmid, "status": "error", "error": str(exc)})
        time.sleep(DELAY)

    _write_kb(papers)
    return results

# ── HTTP handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress default access log spam; print only errors
        if args and str(args[1]) not in ("200", "304"):
            print(f"  [{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/digest"):
            latest = BASE_DIR / "digest_latest.html"
            if latest.exists():
                self._html(latest.read_bytes())
            else:
                self._html(b"<h2>No digest yet. Run pubmed_digest.py first.</h2>")

        elif path == "/status":
            papers = _load_kb()
            self._json(200, {
                "status":       "running",
                "kb_count":     len(papers),
                "digest_ready": (BASE_DIR / "digest_latest.html").exists(),
            })

        elif path == "/knowledge-base":
            papers = _load_kb()
            self._json(200, {"papers": papers, "count": len(papers)})

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/save":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
                return

            pmids = payload.get("pmids", [])
            note  = payload.get("note", "")

            if not pmids:
                self._json(400, {"error": "no pmids provided"})
                return

            print(f"  💾 Save request: {pmids}")
            results = save_pmids(pmids, note)
            ok = all(r["status"] in ("saved", "already_saved") for r in results)
            self._json(200 if ok else 207, {"results": results})

        else:
            self._json(404, {"error": "not found"})


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\n🫀  PubMed Agent server running")
    print(f"    Digest:         http://localhost:{PORT}/")
    print(f"    Knowledge base: http://localhost:{PORT}/knowledge-base")
    print(f"    Status:         http://localhost:{PORT}/status")
    print(f"    Stop:           Ctrl+C\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
