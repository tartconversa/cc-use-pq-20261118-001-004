import unittest
from tests.common import ServiceCase
from meridian.demo import decision, observation, query
from meridian.errors import MeridianError


def both_regions():
    return query() | {"regions": ["North", "South"]}


class CoverageTests(ServiceCase):
    def accept(self, external_id, source_id, stance="positive", story_key=None):
        self.add(external_id, source_id, story_key)
        self.code(external_id, stance, source_id=source_id)

    def frames(self, north_expected=0, south_expected=0, north_batch=None):
        self.service.frames.open("north-frame", "north-a", query()["start"], query()["end"],
                                 north_expected, batch_id=north_batch)
        if south_expected is not None:
            self.service.frames.open("south-frame", "south-a", query()["start"], query()["end"],
                                     south_expected)

    def test_closed_window_and_open_window_are_not_compared_as_equal(self):
        self.accept("n1", "north-a", "positive")
        self.accept("s1", "south-a", "negative")
        self.frames(north_expected=2, south_expected=1)
        self.service.frames.close("south-frame")
        comparison = self.service.comparisons.regions(both_regions(), "North", "South")
        self.assertTrue(comparison["tentative"])
        self.assertEqual(comparison["trace"]["left"]["coverage"]["pending_reports"], 1)
        self.assertEqual(comparison["trace"]["right"]["coverage"]["pending_reports"], 0)
        self.assertEqual(comparison["coverage"]["status"], "incomplete")
        # 已收到的一侧照常显示观察比例，但缺口侧给出区间而不是冒充完整。
        self.assertEqual(comparison["stances"]["positive"]["right_share"], 0.0)
        self.assertEqual(comparison["stances"]["positive"]["left_share"], 1.0)
        self.assertEqual(comparison["stances"]["positive"]["left_interval"],
                         {"lower": 0.5, "upper": 1.0, "denominator": 1, "pending": 1})
        positive = next(claim for claim in comparison["sensitivity"]["claims"]
                        if claim["claim"] == "positive_share")
        self.assertEqual(positive["status"], "stable")
        self.assertFalse(positive["could_flip"])

    def test_gap_flip_is_reported_as_fragile_claim(self):
        for external_id, stance in (("n1", "positive"), ("n2", "negative"), ("n3", "negative")):
            self.accept(external_id, "north-a", stance)
        for external_id in ("s1", "s2"):
            self.accept(external_id, "south-a", "positive")
        for external_id in ("s3", "s4"):
            self.accept(external_id, "south-a", "negative")
        self.frames(north_expected=6, south_expected=4)
        self.service.frames.close("south-frame")
        comparison = self.service.comparisons.regions(both_regions(), "North", "South")
        sensitivity = comparison["sensitivity"]
        self.assertTrue(sensitivity["bounded"])
        self.assertIn("positive_share", sensitivity["fragile_claims"])
        positive = next(claim for claim in sensitivity["claims"]
                        if claim["claim"] == "positive_share")
        self.assertEqual(positive["direction"], "undetermined")
        self.assertEqual(positive["possible_difference"], [-0.166667, 0.333333])
        self.assertTrue(comparison["tentative"])

    def test_arrival_of_promised_reports_resolves_provisional_status(self):
        self.accept("n1", "north-a", "positive")
        self.accept("s1", "south-a", "positive")
        self.frames(north_expected=2, south_expected=1)
        self.service.frames.close("south-frame")
        before = self.service.comparisons.regions(both_regions(), "North", "South")
        self.assertTrue(before["tentative"])
        self.accept("n2", "north-a", "positive")
        self.service.frames.close("north-frame")
        after = self.service.comparisons.regions(both_regions(), "North", "South")
        self.assertFalse(after["tentative"])
        self.assertEqual(after["coverage"]["status"], "complete")

    def test_unreceived_reports_are_never_assigned_a_stance(self):
        self.accept("n1", "north-a", "positive")
        self.accept("s1", "south-a", "negative")
        self.frames(north_expected=3, south_expected=1)
        self.service.frames.close("south-frame")
        report = self.service.analytics.report(both_regions())
        north = next(row for row in report["regions"] if row["region"] == "North")
        self.assertEqual(north["stance_counts"], {"positive": 1})
        self.assertEqual(north["story_count"], 1)
        bounds = north["stance_bounds"]
        self.assertEqual(bounds["stances"]["positive"], {"lower": 0.333333, "upper": 1.0})
        self.assertEqual(report["coverage"]["rules"],
                         {"pending_stance_assigned": False,
                          "cross_source_substitution": False,
                          "repost_fills_gap": False})

    def test_other_outlet_cannot_fill_a_gap(self):
        self.accept("n1", "north-a", "positive")
        self.accept("n2", "north-b", "negative", story_key="n1")
        self.service.frames.open("north-frame", "north-a", query()["start"], query()["end"], 2)
        coverage = self.service.diagnostics.coverage(both_regions())
        north = next(row for row in coverage["regions"] if row["region"] == "North")
        by_source = {row["source_id"]: row for row in north["sources"]}
        self.assertEqual(by_source["north-a"]["pending_reports"], 1)
        self.assertEqual(by_source["north-b"]["status"], "unregistered")
        self.assertEqual(north["denominator"]["stories"], 1)
        self.assertEqual(north["denominator"]["observations"], 2)

    def test_retraction_after_close_reopens_shortfall_and_blocks_release(self):
        self.accept("n1", "north-a", "positive")
        self.frames(north_expected=1, south_expected=0)
        self.service.frames.close("north-frame")
        self.service.frames.close("south-frame")
        self.service.observations.retract("local-fixture", "n1", "corrected", expected_revision=1)
        frame = self.service.frames.describe("north-frame")
        self.assertEqual(frame["drift_kind"], "shortfall")
        self.assertEqual(frame["status"], "shortfall")
        self.assertEqual(frame["pending_reports"], 1)
        self.assertFalse(self.service.frames.coverage_report(query())["regions"][0]["status"]
                         == "complete")
        with self.assertRaises(MeridianError) as caught:
            self.service.frames.reseal("north-frame")
        self.assertEqual(caught.exception.code, "frame_incomplete")

    def test_correction_can_be_resealed_with_history(self):
        self.accept("n1", "north-a", "positive")
        self.frames(north_expected=1, south_expected=0)
        self.service.frames.close("north-frame")
        changed = observation("n1", "north-a", "n1")
        changed["headline"] = "Corrected headline"
        self.service.observations.ingest(changed, expected_revision=1)
        self.assertEqual(self.service.frames.describe("north-frame")["drift_kind"], "revised")
        resealed = self.service.frames.reseal("north-frame", "source corrected headline")
        self.assertEqual(resealed["status"], "complete")
        self.assertEqual([row["seal_no"] for row in self.service.frames.seals("north-frame")],
                         [1, 2])
        self.assertTrue(self.service.audit.run()["ok"])

    def test_partial_import_cannot_share_a_window(self):
        rows = [observation("n1", "north-a", "n1"), observation("n2", "north-a", "n2")]
        self.service.batches.step("batch", rows, limit=1)
        self.service.frames.open("north-frame", "north-a", query()["start"], query()["end"],
                                 2, batch_id="batch")
        frame = self.service.frames.describe("north-frame")
        self.assertEqual(frame["status"], "import_pending")
        self.assertEqual(frame["import_pending_rows"], 1)
        self.assertEqual(frame["pending_reports"], 1)
        with self.assertRaises(MeridianError) as caught:
            self.service.frames.close("north-frame")
        self.assertEqual(caught.exception.code, "frame_batch_incomplete")
        self.service.batches.step("batch", rows)
        closed = self.service.frames.close("north-frame")
        self.assertEqual(closed["status"], "complete")
        self.assertEqual(closed["batch"]["pending_rows"], 0)

    def test_batch_rejection_blocks_frame_close(self):
        rows = [observation("n1", "north-a", "n1"), observation("bad", "missing", "bad")]
        result = self.service.batches.step("batch", rows)
        self.assertEqual(result["rejected"], 1)
        self.service.frames.open("north-frame", "north-a", query()["start"], query()["end"],
                                 1, batch_id="batch")
        with self.assertRaises(MeridianError) as caught:
            self.service.frames.close("north-frame")
        self.assertEqual(caught.exception.code, "frame_batch_rejected")

    def test_source_without_registered_window_is_uncovered(self):
        self.accept("n1", "north-a", "positive")
        self.accept("n2", "north-b", "positive", story_key="n2")
        self.frames(north_expected=1, south_expected=0)
        self.service.frames.close("north-frame")
        self.service.frames.close("south-frame")
        coverage = self.service.frames.coverage_report(query())
        north = coverage["regions"][0]
        self.assertEqual(north["status"], "partial")
        self.assertEqual(north["uncovered_sources"], ["north-b"])
        self.assertTrue(north["provisional"])

    def test_unregistered_coverage_is_not_reported_as_complete(self):
        self.accept("n1", "north-a", "positive")
        report = self.service.analytics.report(query())
        self.assertEqual(report["coverage_status"], "no_commitments")
        self.assertFalse(report["provisional"])
        # 没有窗口承诺时只能显示已观察比例，不能给出覆盖区间。
        self.assertIsNone(report["regions"][0]["stance_bounds"])
        self.assertEqual(report["regions"][0]["stance_shares"], {"positive": 1.0})
        preview = self.service.releases.preview(query())
        self.assertNotIn("collection_gap", preview["quality"]["reasons"])
        self.assertNotIn("collection_coverage", preview["quality"]["reasons"])
        readiness = self.service.diagnostics.readiness(query())
        self.assertIn("coverage_unregistered", readiness["warnings"])
        self.assertFalse(readiness["ready"])

    def test_window_without_open_commitment_blocks_release(self):
        self.accept("n1", "north-a", "positive")
        self.frames(north_expected=1, south_expected=0)
        self.service.frames.close("south-frame")
        preview = self.service.releases.preview(query())
        self.assertFalse(preview["quality"]["passed"])
        self.assertIn("collection_coverage", preview["quality"]["reasons"])
        self.assertTrue(preview["provisional"])
        with self.assertRaises(MeridianError) as caught:
            self.service.releases.publish("release", query(), preview["token"])
        self.assertEqual(caught.exception.code, "quality_failed")

    def test_frozen_release_keeps_its_coverage_interpretation(self):
        self.accept("n1", "north-a", "positive")
        self.frames(north_expected=1, south_expected=0)
        self.service.frames.close("north-frame")
        self.service.frames.close("south-frame")
        preview = self.service.releases.preview(query())
        self.assertTrue(preview["quality"]["passed"])
        self.service.releases.publish("release", query(), preview["token"])
        self.service.observations.retract("local-fixture", "n1", "corrected", expected_revision=1)
        frozen = self.service.releases.describe("release")["report"]
        self.assertEqual(frozen["coverage"]["status"], "complete")
        comparison = self.service.releases.compare("release")
        self.assertTrue(comparison["coverage_changed"])
        self.assertEqual(comparison["current_coverage"]["regions"][0]["status"], "incomplete")
        changes = self.service.provenance.release_changes("release")
        self.assertEqual(changes["coverage_changes"][0]["region"], "North")
        self.assertEqual(changes["frozen_coverage"]["status"], "complete")
        self.assertEqual(changes["current_coverage"]["regions"][0]["status"], "incomplete")
        bundle = self.service.exports.frozen("release")
        self.assertEqual(bundle["coverage"]["status"], "complete")
        self.assertTrue(self.service.exports.verify(bundle)["valid"])

    def test_coverage_trace_reaches_sources_frames_and_story_denominators(self):
        self.accept("n1", "north-a", "positive")
        self.frames(north_expected=2, south_expected=0)
        self.service.frames.close("south-frame")
        trace = self.service.diagnostics.coverage(query())
        north = trace["regions"][0]
        self.assertEqual(north["denominator"], {"stories": 1, "observations": 1})
        source = north["sources"][0]
        self.assertEqual(source["source_id"], "north-a")
        self.assertEqual(source["frames"][0]["expected_count"], 2)
        self.assertEqual(source["frames"][0]["observed_count"], 1)
        self.assertEqual(source["frames"][0]["pending_reports"], 1)
        self.assertEqual(source["stories"][0]["story_key"], "n1")

    def test_window_not_covering_query_cannot_be_scored(self):
        self.add("n1", "north-a", "n1")
        self.service.observations.ingest(observation("n2", "north-a", "n2"))
        wide_end = "2027-01-01T00:00:00+00:00"
        self.service.frames.open("wide", "north-a", query()["start"], wide_end, 2)
        self.service.frames.close("wide")
        coverage = self.service.frames.coverage_report(query())
        self.assertEqual(coverage["regions"][0]["status"], "partial")
        # 窗口跨出查询范围时缺口未知，不能记成零缺口。
        self.assertIsNone(coverage["regions"][0]["pending_reports"])
        self.assertFalse(coverage["regions"][0]["bounded"])
        self.assertIsNone(coverage["regions"][0]["sources"][0]["frames"][0]["pending_reports"])

    def test_reopening_drifted_frame_allows_reimport(self):
        self.accept("n1", "north-a", "positive")
        self.frames(north_expected=1, south_expected=0)
        self.service.frames.close("north-frame")
        self.service.observations.retract("local-fixture", "n1", "withdrawn", expected_revision=1)
        self.service.frames.withdraw("north-frame")
        reopened = self.service.frames.describe("north-frame")
        self.assertEqual(reopened["state"], "open")
        self.assertFalse(reopened["drifted"])
        self.add("n2", "north-a", "n2")
        self.code("n2", "positive")
        self.assertEqual(self.service.frames.close("north-frame")["status"], "complete")


if __name__ == "__main__":
    unittest.main()
