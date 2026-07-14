// One-click ⭐/👎 feedback endpoint for the PubMed digest.
//
// The digest is a static GitHub Pages site, so the browser can't create the
// feedback issue itself without shipping a GitHub token to the public page.
// This function holds the token server-side: the page POSTs {kind, pmid, title},
// and we open the same prefilled issue the old form flow produced. The existing
// feedback.yml workflow then ingests and closes it — nothing else changed.
//
// Env: FEEDBACK_GITHUB_TOKEN — fine-grained PAT, repo dtarditi51/pubmed-digest,
// permission "Issues: Read and write" only.

const GH_REPO = "dtarditi51/pubmed-digest";

module.exports = async (req, res) => {
  // The page lives on github.io, so every real request is cross-origin.
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });

  const token = process.env.FEEDBACK_GITHUB_TOKEN;
  if (!token) return res.status(503).json({ error: "FEEDBACK_GITHUB_TOKEN not configured" });

  const { kind, pmid, title = "" } = req.body || {};
  if (!["star", "reject"].includes(kind) || !/^\d{1,10}$/.test(String(pmid))) {
    return res.status(400).json({ error: "expected {kind: star|reject, pmid: digits}" });
  }

  // Body is just the quoted paper title — apply_feedback.py strips "> " lines,
  // so a one-click issue carries no note (notes were optional in the form too).
  const issue = {
    title: `${kind === "star" ? "⭐ star" : "👎 reject"} ${pmid}`,
    body: `> ${String(title).slice(0, 90)}\n`,
    labels: [kind === "star" ? "star" : "reject"],
  };

  const r = await fetch(`https://api.github.com/repos/${GH_REPO}/issues`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "pubmed-digest-feedback",
    },
    body: JSON.stringify(issue),
  });

  if (!r.ok) {
    const detail = await r.text().catch(() => "");
    return res.status(502).json({ error: `github ${r.status}`, detail: detail.slice(0, 200) });
  }
  const data = await r.json();
  return res.status(200).json({ ok: true, issue: data.number });
};
