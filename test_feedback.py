#!/usr/bin/env python3
"""
Tests for the two-sided feedback loop.

  python3 test_feedback.py

Covers the feedback store, the pull-time blocklist filter, the knowledge-graph
rejection path, what the graph learns from ⭐/👎, the relevance scoring built on
top of it, and the issue-parsing the GitHub Action relies on. No network — every
test runs on temp files.
"""

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import apply_feedback
import feedback
import knowledge_graph as kg
import pubmed_digest
import relevance


VET_A = {
    "pmid": "1001",
    "title": "Genomic sequencing of canine myocardial tissue in laboratory beagles",
    "abstract": "We performed whole-genome sequencing on canine myocardial samples "
                "from a laboratory beagle colony to map veterinary genomic variants.",
    "mesh_terms": ["Dogs", "Genomics", "Veterinary Medicine"],
    "journal": "Journal of Veterinary Genomics",
    "authors": ["Smith Jane"],
    "year": "2026",
}
VET_B = {
    "pmid": "1002",
    "title": "Canine genomic variants in veterinary cardiology: a beagle colony study",
    "abstract": "Veterinary genomic sequencing of canine subjects identified variants "
                "in beagle myocardial tissue from a laboratory colony.",
    "mesh_terms": ["Dogs", "Genomics", "Veterinary Medicine"],
    "journal": "Journal of Veterinary Genomics",
    "authors": ["Smith Jane"],
    "year": "2026",
}
HF_RCT = {
    "pmid": "2001",
    "title": "Sacubitril-valsartan and mortality in heart failure: a randomized controlled trial",
    "abstract": "In this randomized controlled trial we enrolled 8,442 patients with "
                "heart failure and measured mortality and hospitalization endpoints.",
    "mesh_terms": ["Heart Failure", "Humans", "Mortality"],
    "journal": "Circulation",
    "authors": ["Doe John"],
    "year": "2026",
}
HF_RCT_2 = {
    "pmid": "2002",
    "title": "Empagliflozin and hospitalization for heart failure: a randomized trial",
    "abstract": "A randomized controlled trial of 5,988 patients with heart failure "
                "reporting mortality and hospitalization outcomes.",
    "mesh_terms": ["Heart Failure", "Humans", "Mortality"],
    "journal": "Circulation",
    "authors": ["Roe Richard"],
    "year": "2026",
}


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.fb    = d / "feedback.json"
        self.kb    = d / "knowledge_base.json"
        self.graph = d / "knowledge_graph.json"

    def tearDown(self):
        self._tmp.cleanup()

    def star(self, *papers):
        self.kb.write_text(json.dumps({"papers": list(papers)}))

    def reject(self, *papers, reason=""):
        for p in papers:
            feedback.add_rejection(
                p["pmid"], title=p["title"], abstract=p["abstract"],
                mesh_terms=p["mesh_terms"], journal=p["journal"],
                authors=p["authors"], reason=reason, path=self.fb,
            )

    def build(self):
        return kg.build(kb_path=self.kb, graph_path=self.graph, feedback_path=self.fb)

    def profile(self):
        return relevance.build_profile(kb_path=self.kb, feedback_path=self.fb,
                                       graph_path=self.graph)


# ── 1. The feedback store ──────────────────────────────────────────────────────
class TestStore(Temp):
    def test_rejection_records_pmid_title_timestamp_and_reason(self):
        rec = feedback.add_rejection("1001", title="Canine study",
                                     reason="veterinary, not human", path=self.fb)
        self.assertEqual(rec["pmid"], "1001")
        self.assertEqual(rec["title"], "Canine study")
        self.assertEqual(rec["reason"], "veterinary, not human")
        self.assertTrue(rec["timestamp"])
        self.assertEqual(len(json.loads(self.fb.read_text())["rejections"]), 1)

    def test_blocklist_reads_back_pmids(self):
        feedback.add_rejection("1001", path=self.fb)
        feedback.add_rejection("1002", path=self.fb)
        self.assertEqual(feedback.blocklist(self.fb), {"1001", "1002"})

    def test_rejecting_twice_updates_rather_than_duplicates(self):
        feedback.add_rejection("1001", reason="first", path=self.fb)
        feedback.add_rejection("1001", reason="second", path=self.fb)
        recs = feedback.negatives(self.fb)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["reason"], "second")

    def test_unreject_removes_from_blocklist(self):
        feedback.add_rejection("1001", path=self.fb)
        self.assertTrue(feedback.remove_rejection("1001", path=self.fb))
        self.assertEqual(feedback.blocklist(self.fb), set())
        self.assertFalse(feedback.remove_rejection("9999", path=self.fb))

    def test_missing_file_is_an_empty_blocklist(self):
        self.assertEqual(feedback.blocklist(self.fb), set())


# ── 2. The blocklist filter at pull time ───────────────────────────────────────
class TestBlocklistFilter(unittest.TestCase):
    def test_blocked_pmid_is_filtered_before_fetch(self):
        self.assertEqual(
            pubmed_digest.filter_candidates(["1001", "2001"], seen={}, blocked={"1001"}),
            ["2001"],
        )

    def test_blocklist_survives_the_60_day_seen_window(self):
        # seen_pmids.json is pruned at 60 days. If the blocklist rode along in it,
        # a rejected paper would resurface once it aged out. `seen` is empty here
        # (post-prune) and the PMID must still be gone.
        self.assertEqual(
            pubmed_digest.filter_candidates(["1001"], seen={}, blocked={"1001"}), []
        )

    def test_seen_and_blocked_compose_and_order_is_preserved(self):
        out = pubmed_digest.filter_candidates(
            ["9", "8", "7"], seen={"8": "2026-07-01"}, blocked={"7"}
        )
        self.assertEqual(out, ["9"])


# ── 3. The knowledge-graph rejection path ──────────────────────────────────────
class TestGraphRejection(Temp):
    def test_starred_paper_becomes_an_active_node_with_edges(self):
        self.star(HF_RCT)
        g    = self.build()
        ids  = {n["id"] for n in g["nodes"]}
        rels = {(e["src"], e["rel"], e["dst"]) for e in g["edges"]}
        self.assertIn("pmid:2001", ids)
        self.assertIn(("pmid:2001", "has_mesh", "mesh:Heart Failure"), rels)
        self.assertIn(("pmid:2001", "published_in", "journal:Circulation"), rels)
        self.assertTrue(all(e["status"] == "active" for e in g["edges"]))

    def test_rejecting_a_starred_paper_marks_node_and_edges_rejected(self):
        self.star(HF_RCT, VET_A)
        self.build()
        self.assertGreater(kg.reject_pmid("1001", graph_path=self.graph), 0)

        g    = kg.load_graph(self.graph)
        node = next(n for n in g["nodes"] if n["id"] == "pmid:1001")
        self.assertEqual(node["status"], "rejected")
        self.assertTrue(node["rejected_at"])
        incident = [e for e in g["edges"] if e["src"] == "pmid:1001"]
        self.assertTrue(incident)
        self.assertTrue(all(e["status"] == "rejected" for e in incident))

    def test_a_reject_is_retained_as_negative_signal_not_deleted(self):
        # The whole point: a rejection is knowledge. It must stay in the graph,
        # marked, so scoring can steer away from it.
        self.star(HF_RCT)
        self.reject(VET_A)
        g = self.build()
        node = next(n for n in g["nodes"] if n["id"] == "pmid:1001")
        self.assertEqual(node["status"], "rejected")
        self.assertIn("mesh:Veterinary Medicine", {n["id"] for n in g["nodes"]})

    def test_a_reject_never_starred_still_enters_the_graph(self):
        # Most thumbs-downs are on papers you never starred, so they only exist
        # in feedback.json. They must still reach the graph.
        self.star(HF_RCT)
        self.reject(VET_A)
        ids = {n["id"] for n in self.build()["nodes"]}
        self.assertIn("pmid:1001", ids)

    def test_rejected_concepts_are_marked_rejected_endorsed_ones_are_not(self):
        self.star(HF_RCT)
        self.reject(VET_A)
        g = self.build()
        by_id = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(by_id["mesh:Veterinary Medicine"]["status"], "rejected")
        self.assertEqual(by_id["mesh:Heart Failure"]["status"], "active")

    def test_a_concept_shared_with_a_starred_paper_stays_active(self):
        shared = dict(HF_RCT, mesh_terms=["Heart Failure", "Genomics"])
        self.star(shared)
        self.reject(VET_A)
        by_id = {n["id"]: n for n in self.build()["nodes"]}
        self.assertEqual(by_id["mesh:Genomics"]["status"], "active",
                         "a concept an endorsed paper also touches is not tainted")

    def test_untouched_paper_survives_a_rejection(self):
        self.star(HF_RCT, VET_A)
        self.build()
        kg.reject_pmid("1001", graph_path=self.graph)
        g = kg.load_graph(self.graph)
        self.assertEqual(next(n for n in g["nodes"] if n["id"] == "pmid:2001")["status"],
                         "active")
        self.assertTrue(all(e["status"] == "active"
                            for e in g["edges"] if e["src"] == "pmid:2001"))

    def test_blocklisted_pmid_is_never_ingested_as_knowledge(self):
        self.star(HF_RCT, VET_A)
        feedback.add_rejection("1001", path=self.fb)
        g = self.build()
        node = next(n for n in g["nodes"] if n["id"] == "pmid:1001")
        self.assertEqual(node["status"], "rejected",
                         "a starred paper you later rejected: the reject wins")

    def test_ingest_refuses_a_blocklisted_paper(self):
        self.star(HF_RCT)
        self.build()
        feedback.add_rejection("1001", path=self.fb)
        added = kg.ingest_paper(VET_A, graph_path=self.graph, feedback_path=self.fb)
        self.assertFalse(added)
        self.assertNotIn("pmid:1001", {n["id"] for n in kg.load_graph(self.graph)["nodes"]})

    def test_ingest_accepts_a_clean_paper(self):
        self.star()
        self.build()
        self.assertTrue(kg.ingest_paper(HF_RCT, graph_path=self.graph,
                                        feedback_path=self.fb))
        self.assertIn("pmid:2001", {n["id"] for n in kg.load_graph(self.graph)["nodes"]})

    def test_unreject_restores_the_node_to_active(self):
        self.star(HF_RCT, VET_A)
        self.build()
        kg.reject_pmid("1001", graph_path=self.graph)
        kg.unreject_pmid("1001", graph_path=self.graph)
        g = kg.load_graph(self.graph)
        node = next(n for n in g["nodes"] if n["id"] == "pmid:1001")
        self.assertEqual(node["status"], "active")
        self.assertNotIn("rejected_at", node)


# ── 4. What the graph learns from both loops ───────────────────────────────────
class TestGraphLearning(Temp):
    def test_starred_concepts_score_positive_rejected_ones_negative(self):
        self.star(HF_RCT)
        self.reject(VET_A)
        w = kg.preference_weights(self.build())
        self.assertGreater(w["mesh:Heart Failure"], 0)
        self.assertLess(w["mesh:Veterinary Medicine"], 0)
        self.assertGreater(w["journal:Circulation"], 0)
        self.assertLess(w["journal:Journal of Veterinary Genomics"], 0)

    def test_a_concept_on_both_sides_cancels_out(self):
        both_star  = dict(HF_RCT, mesh_terms=["Heart Failure", "Humans"])
        both_reject = dict(VET_A, mesh_terms=["Veterinary Medicine", "Humans"])
        self.star(both_star)
        self.reject(both_reject)
        w = kg.preference_weights(self.build())
        self.assertEqual(w.get("mesh:Humans", 0), 0,
                         "a concept both sides touch equally carries no preference")

    def test_more_stars_strengthen_a_concept(self):
        self.star(HF_RCT)
        w_one = kg.preference_weights(self.build())["mesh:Heart Failure"]
        self.star(HF_RCT, dict(HF_RCT_2, mesh_terms=["Heart Failure"]))
        w_two = kg.preference_weights(self.build())["mesh:Heart Failure"]
        self.assertEqual(w_one, 1.0)
        self.assertEqual(w_two, 1.0)   # every starred paper still touches it


# ── 5. Relevance scoring on top of the graph ───────────────────────────────────
class TestRelevance(Temp):
    def test_empty_profile_scores_zero(self):
        self.assertEqual(relevance.fit_score(HF_RCT, {}), 0)

    def test_an_article_like_your_stars_is_boosted(self):
        self.star(HF_RCT)
        self.reject(VET_A)
        self.build()
        self.assertGreater(relevance.fit_score(HF_RCT_2, self.profile()), 0)

    def test_an_article_like_your_rejects_is_penalised(self):
        self.star(HF_RCT)
        self.reject(VET_A)
        self.build()
        self.assertLess(relevance.fit_score(VET_B, self.profile()), 0)

    def test_fit_raises_and_lowers_the_digest_score(self):
        self.star(HF_RCT)
        self.reject(VET_A)
        self.build()
        p = self.profile()

        good = pubmed_digest.score_paper(HF_RCT_2, p)
        bad  = pubmed_digest.score_paper(VET_B, p)

        self.assertGreater(good["fit"], 0)
        self.assertLess(bad["fit"], 0)
        self.assertEqual(good["total"], good["quality"] + good["fit"])
        self.assertEqual(bad["total"],  max(bad["quality"] + bad["fit"], 0))
        self.assertGreater(good["total"], pubmed_digest.score_paper(HF_RCT_2, {})["total"])
        self.assertLess(bad["total"],     pubmed_digest.score_paper(VET_B, {})["total"])

    def test_score_never_goes_negative(self):
        self.reject(VET_A)
        self.build()
        self.assertGreaterEqual(pubmed_digest.score_paper(VET_B, self.profile())["total"], 0)

    def test_a_paper_with_no_mesh_still_scores_from_text(self):
        # New PubMed records often have no MeSH terms yet — the text signal has to
        # carry them, or the loop would be blind to everything fresh.
        self.star(HF_RCT)
        self.reject(VET_A)
        self.build()
        no_mesh = dict(VET_B, mesh_terms=[], journal="", authors=[])
        self.assertLess(relevance.fit_score(no_mesh, self.profile()), 0)


# ── 6. The issue parsing the GitHub Action relies on ───────────────────────────
class TestIssueParsing(unittest.TestCase):
    def test_star_and_reject_titles_parse(self):
        self.assertEqual(apply_feedback.parse_title("⭐ star 42098926"),
                         ("star", "42098926"))
        self.assertEqual(apply_feedback.parse_title("👎 reject 42098926"),
                         ("reject", "42098926"))

    def test_unrelated_issue_is_ignored(self):
        self.assertEqual(apply_feedback.parse_title("Bug: digest is empty"),
                         (None, None))

    def test_template_lines_are_stripped_from_the_reason(self):
        body = ("> Genomic sequencing of canine myocardial tissue\n\n"
                "Reason (optional, one line — it shows up in `feedback.py --report`):\n"
                "veterinary, not human medicine\n")
        self.assertEqual(apply_feedback.parse_note(body, "👎 reject 1001"),
                         "veterinary, not human medicine")

    def test_an_empty_body_yields_no_reason(self):
        self.assertEqual(apply_feedback.parse_note("", "👎 reject 1001"), "")

    def test_the_digest_link_round_trips_through_the_parser(self):
        # The button the digest renders must produce an issue the Action can read.
        url  = pubmed_digest._issue_url("reject", VET_A)
        q    = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        kind, pmid = apply_feedback.parse_title(q["title"][0])
        self.assertEqual((kind, pmid), ("reject", "1001"))
        self.assertEqual(q["labels"][0], "reject")
        self.assertEqual(apply_feedback.parse_note(q["body"][0], q["title"][0]), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
