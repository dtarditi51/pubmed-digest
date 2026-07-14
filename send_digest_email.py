#!/usr/bin/env python3
"""
Email the compact digest rendering (digest_email.html) via Resend.

Run by digest.yml right after the digest is generated and deployed. Uses the
Resend sandbox sender, which may only deliver to the account owner's own
address — exactly this use case. Swap FROM for a verified-domain sender if a
domain is ever added to the Resend account.

  RESEND_API_KEY=... python3 send_digest_email.py
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent
EMAIL_FILE = BASE_DIR / "digest_email.html"

TO         = "drdanieltarditi@gmail.com"
FROM       = "PubMed Digest <onboarding@resend.dev>"
DIGEST_URL = "https://dtarditi51.github.io/pubmed-digest/"


def main():
    key = os.environ.get("RESEND_API_KEY", "")
    if not key:
        sys.exit("RESEND_API_KEY not set")
    if not EMAIL_FILE.exists():
        sys.exit(f"{EMAIL_FILE.name} not found — run pubmed_digest.py first")

    body = EMAIL_FILE.read_text(encoding="utf-8")
    m = re.match(r"<!-- papers:(\d+) -->", body)
    n = m.group(1) if m else "?"

    payload = {
        "from": FROM,
        "to": [TO],
        "subject": f"🫀 PubMed Digest — {datetime.now().strftime('%A, %B %d')} ({n} papers)",
        "html": body,
        "text": f"{n} new papers this morning. Read and rate them here: {DIGEST_URL}",
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Cloudflare fronts api.resend.com and rejects urllib's default
            # Python-urllib/3.x agent with a 403 (error 1010).
            "User-Agent": "pubmed-digest/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"📧 Sent to {TO}: {r.read().decode()}")
    except urllib.error.HTTPError as e:
        sys.exit(f"Resend {e.code}: {e.read().decode()[:300]}")


if __name__ == "__main__":
    main()
