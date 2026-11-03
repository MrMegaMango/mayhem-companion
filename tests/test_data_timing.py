import copy
import unittest

from mayhem.data_timing import data_freshness, data_period, data_period_details, data_timing


class DataTimingTests(unittest.TestCase):
    def test_explicit_match_dates_and_source_freshness_are_separate(self):
        self.assertEqual(data_timing({"sample_start": "2026-10-21", "sample_end": "2026-11-01",
                                      "updated": "2026-11-01T06:23:45Z", "updated_scope": "dataset"}),
                         "Matches 2026-10-21 – 2026-11-01 · Dataset updated 2026-11-01 06:23 UTC")

    def test_offset_timestamps_normalize_to_utc_before_order_comparison(self):
        self.assertEqual(data_timing({"sample_start": "2026-11-01T01:30:00-07:00",
                                      "sample_end": "2026-11-01T01:15:00-08:00"}),
                         "Matches 2026-11-01 08:30 UTC – 2026-11-01 09:15 UTC")
        self.assertEqual(data_timing({"updated": "2026-11-01T00:10:15.123456+02:00"}),
                         "Match dates not published · Updated 2026-10-31 22:10 UTC")

    def test_patch_and_freshness_do_not_invent_a_sample_window(self):
        self.assertEqual(data_timing({"version": "26.21", "patch": "16.21", "data_date": "2026-10-31"}),
                         "Match dates not published · Updated 2026-10-31")
        self.assertEqual(data_timing({"build": "2026-10-31", "date_date": "2026-10-30"}),
                         "Match dates not published")

    def test_partial_sample_bounds_are_explicitly_unknown(self):
        self.assertEqual(data_timing({"sample_start": "2026-10-21"}), "Matches 2026-10-21 – unknown")
        self.assertEqual(data_timing({"sample_end": "2026-11-01"}), "Matches unknown – 2026-11-01")
        self.assertEqual(data_timing({"sample_start": "invalid", "sample_end": "2026-11-01"}),
                         "Matches unknown – 2026-11-01")

    def test_reversed_or_mixed_precision_bounds_are_not_presented_as_a_range(self):
        for start, end in (("2026-11-01", "2026-10-21"),
                           ("2026-11-01", "2026-11-01T20:00:00Z"),
                           ("2026-11-01T20:00:00Z", "2026-11-01"),
                           ("2026-11-01T10:00:00+01:00", "2026-11-01T08:30:00Z")):
            with self.subTest(start=start, end=end):
                self.assertEqual(data_timing({"sample_start": start, "sample_end": end}),
                                 "Match dates not published")

    def test_equal_endpoints_are_valid(self):
        self.assertEqual(data_timing({"sample_start": "2024-02-29", "sample_end": "2024-02-29"}),
                         "Matches 2024-02-29 – 2024-02-29")

    def test_invalid_dates_and_naive_datetimes_are_rejected(self):
        for value in ("2026-02-29", "2026-04-31", "2026-13-01", "2026-11-01T12:30:00",
                      "2026-11-01T12:30:00+01:60", "2026-11-01T12:30:00+24:00",
                      "2026-11-01T25:00:00Z", "2026-W44-7", "20261101", "2026-11-01 junk",
                      "2026-11-01\n", "", None, False, 1793520000, {}):
            with self.subTest(value=value):
                self.assertEqual(data_timing({"sample_start": value, "sample_end": value,
                                              "updated": value, "data_date": value, "fetched_at": value}),
                                 "Match dates not published")

    def test_source_update_precedes_retrieval(self):
        self.assertEqual(data_timing({"updated": "2026-10-31", "data_date": "2026-10-30",
                                      "fetched_at": "2026-11-01T12:00:00Z", "_fetched_at": 0}),
                         "Match dates not published · Updated 2026-10-31")
        self.assertEqual(data_timing({"updated": "invalid", "data_date": "2026-10-30",
                                      "fetched_at": "2026-11-01T12:00:00Z"}),
                         "Match dates not published · Updated 2026-10-30")

    def test_retrieval_is_labelled_and_accepts_only_the_designated_epoch_field(self):
        self.assertEqual(data_timing({"fetched_at": "2026-11-01T04:00:00-08:00", "_fetched_at": 0}),
                         "Match dates not published · Retrieved 2026-11-01 12:00 UTC")
        self.assertEqual(data_timing({"fetched_at": "invalid", "_fetched_at": 0}),
                         "Match dates not published · Retrieved 1970-01-01 00:00 UTC")
        self.assertEqual(data_timing({"_fetched_at": 90.5}),
                         "Match dates not published · Retrieved 1970-01-01 00:01 UTC")

    def test_invalid_epochs_do_not_fall_back_to_current_time(self):
        for value in (False, True, None, "0", -1, float("nan"), float("inf"),
                      -float("inf"), 10**1000, {}, []):
            with self.subTest(value=value):
                self.assertEqual(data_timing({"_fetched_at": value}), "Match dates not published")

    def test_editorial_uses_only_edit_time(self):
        self.assertEqual(data_timing({"updated": "2026-10-31T14:00:00-07:00", "updated_scope": "dataset",
                                      "sample_start": "2026-10-21", "sample_end": "2026-11-01"}, editorial=True),
                         "Edited 2026-10-31 21:00 UTC")
        self.assertEqual(data_timing({"updated": "2026-10-31"}, editorial=True), "Edited 2026-10-31")
        self.assertEqual(data_timing({"data_date": "2026-10-31", "fetched_at": "2026-11-01T12:00:00Z",
                                      "_fetched_at": 0}, editorial=True), "Update date not published")
        self.assertEqual(data_timing({"updated": "2026-10-31T12:00:00"}, editorial=True),
                         "Update date not published")

    def test_stale_flag_does_not_change_dates_or_mutate_metadata(self):
        meta = {"data_date": "2026-10-31", "fetched_at": "2026-11-01T12:00:00Z", "stale": True}
        original = copy.deepcopy(meta)
        self.assertEqual(data_timing(meta), data_timing({**meta, "stale": False}))
        self.assertEqual(meta, original)

    def test_missing_metadata_is_honestly_unknown(self):
        for meta in (None, {}, [], "2026-11-01"):
            self.assertEqual(data_timing(meta), "Match dates not published")
            self.assertEqual(data_timing(meta, editorial=True), "Update date not published")


class DataPeriodTests(unittest.TestCase):
    def test_known_lol_aliases_produce_explicit_estimate_not_match_bounds(self):
        for source in ("ARAMKit", "Lolalytics", "winrate.lol", "LOLALYTICS"):
            for patch in ("16.21", "26.21"):
                meta = {"source": source, "version": patch, "data_date": "2026-11-02"}
                with self.subTest(source=source, patch=patch):
                    self.assertEqual(data_period(meta), "Patch period (est.): Oct 21 – Nov 2, 2026")
                    self.assertIn("not a measured first/last-match interval", data_period_details(meta))
                    self.assertIn("Pacific Time", data_period_details(meta))
                    self.assertIn("not the latest included match", data_period_details(meta))
                    self.assertIn("https://support.riotgames.com/", data_period_details(meta))
                    self.assertTrue(data_timing(meta).startswith("Match dates not published"))

    def test_known_calendar_handles_prior_and_upcoming_patches(self):
        for patch, day, label in (("16.19", "2026-09-25", "Sep 23 – Sep 25"),
                                  ("26.20", "2026-10-10", "Oct 7 – Oct 10"),
                                  ("26.22", "2026-11-05", "Nov 4 – Nov 5"),
                                  ("26.23", "2026-11-20", "Nov 18 – Nov 20"),
                                  ("26.24", "2026-12-10", "Dec 9 – Dec 10")):
            with self.subTest(patch=patch):
                self.assertEqual(data_period({"game_mode": "Arena", "patch": patch, "updated": day}),
                                 f"Patch period (est.): {label}, 2026")

    def test_actual_and_partial_match_bounds_override_patch_estimates(self):
        meta = {"source": "ARAMKit", "version": "16.21", "data_date": "2026-11-02"}
        self.assertEqual(data_period({**meta, "sample_start": "2026-10-23", "sample_end": "2026-11-01"}),
                         "Matches 2026-10-23 – 2026-11-01")
        self.assertEqual(data_period({**meta, "sample_start": "2026-10-23"}), "Matches 2026-10-23 – unknown")
        for bounds in ({"sample_start": "2026-11-03", "sample_end": "2026-11-01"},
                       {"sample_start": "bad", "sample_end": None},
                       {"sample_start": "2026-11-01", "sample_end": "2026-11-02T00:00:00Z"}):
            with self.subTest(bounds=bounds):
                self.assertEqual(data_period({**meta, **bounds}), "Match dates not published")
                self.assertIn("invalid or ambiguous", data_period_details({**meta, **bounds}))

    def test_retrieval_only_has_distinct_label_and_explanation(self):
        meta = {"source": "winrate.lol", "patch": "16.21", "fetched_at": "2026-11-02T03:00:00-08:00"}
        self.assertEqual(data_period(meta), "Patch start → retrieval: Oct 21 – Nov 2, 2026")
        self.assertEqual(data_freshness(meta), "Retrieved 2026-11-02 11:00 UTC")
        self.assertIn("not a source update time", data_period_details(meta))
        self.assertIn("UTC calendar date", data_period_details(meta))
        self.assertEqual(data_period({"source": "winrate.lol", "patch": "16.21", "_fetched_at": 1793577600}),
                         "Patch start → retrieval: Oct 21 – Nov 2, 2026")

    def test_source_snapshot_wins_over_retrieval_without_current_clock_or_next_patch(self):
        meta = {"source": "ARAMKit", "version": "16.21", "data_date": "2026-10-30", "fetched_at": "2026-11-07T12:00:00Z"}
        self.assertEqual(data_period(meta), "Patch period (est.): Oct 21 – Oct 30, 2026")
        self.assertEqual(data_period({**meta, "updated": "2026-11-01T00:10:00+02:00"}),
                         "Patch period (est.): Oct 21 – Oct 31, 2026")
        self.assertEqual(data_period({"source": "ARAMKit", "version": "16.21"}), "Match dates not published")
        self.assertEqual(data_period({"source": "ARAMKit", "version": "16.21", "data_date": "2026-11-10"}),
                         "Patch period (est.): Oct 21 – Nov 10, 2026")

    def test_invalid_unknown_conflicting_or_reversed_calendar_ranges_stay_unavailable(self):
        for extra in ({"data_date": "2026-10-20"}, {"version": "16.22", "patch": "26.21"},
                      {"version": "25.21"}, {"version": "16212"}, {"version": "16.21.1"},
                      {"version": "16.21c"}, {"source": "Unknown source"},
                      {"patch_start": "invalid"}, {"data_date": "2026-11-02T12:00:00"}):
            with self.subTest(extra=extra):
                meta = {"source": "ARAMKit", "version": "16.21", "data_date": "2026-11-02", **extra}
                self.assertEqual(data_period(meta), "Match dates not published")
        for value in (True, -1, float("inf"), float("nan"), 10**1000):
            self.assertEqual(data_period({"source": "ARAMKit", "version": "16.21", "_fetched_at": value}),
                             "Match dates not published")

    def test_source_can_publish_patch_start_without_a_calendar_lookup(self):
        meta = {"patch_start": "2027-01-06", "data_date": "2027-01-10"}
        self.assertEqual(data_period(meta), "Patch period (est.): Jan 6 – Jan 10, 2027")
        self.assertIn("Source-provided patch start", data_period_details(meta))
        self.assertNotIn("Riot calendar", data_period_details(meta))
        self.assertEqual(data_period({"patch_start": "2026-12-30", "data_date": "2027-01-02"}),
                         "Patch period (est.): Dec 30, 2026 – Jan 2, 2027")
        self.assertEqual(data_period({"patch_start": "2026-11-02T12:00:00Z", "updated": "2026-11-02T11:00:00Z"}),
                         "Match dates not published")

    def test_timestamp_end_cannot_precede_scheduled_pacific_release_day(self):
        for patch, rejected, accepted in (("16.21", "2026-10-21T06:59:00Z", "2026-10-21T07:00:00Z"),
                                          ("26.22", "2026-11-04T07:59:00Z", "2026-11-04T08:00:00Z")):
            meta = {"source": "ARAMKit", "version": patch}
            with self.subTest(patch=patch):
                self.assertEqual(data_period({**meta, "updated": rejected}), "Match dates not published")
                self.assertTrue(data_period({**meta, "updated": accepted}).startswith("Patch period (est.):"))

    def test_tft_uses_its_own_calendar_and_discloses_unknown_midpatch_start(self):
        meta = {"source": "Tactics.tools", "patch": "18.5c", "updated": "2026-11-02T09:00:00Z"}
        self.assertEqual(data_period(meta), "Patch period (est.): Oct 21 – Nov 2, 2026")
        self.assertIn("base TFT patch 18.5", data_period_details(meta))
        self.assertIn("exact 18.5c mid-patch start is not established", data_period_details(meta))
        self.assertIn("/tft/events/", data_period_details(meta))
        self.assertEqual(data_period({**meta, "patch": "18.6b", "updated": "2026-11-12"}),
                         "Patch period (est.): Nov 11 – Nov 12, 2026")
        for patch in ("16212", "16.21", "26.21"):
            with self.subTest(patch=patch):
                self.assertEqual(data_period({**meta, "patch": patch}), "Match dates not published")
        self.assertEqual(data_period({"source": "ARAMKit", "game_mode": "TFT", "patch": "16.21", "data_date": "2026-11-02"}),
                         "Match dates not published")
        self.assertEqual(data_period({"source": "Tactics.tools", "_patch_id": 16212, "data_date": "2026-11-02"}),
                         "Match dates not published")

    def test_editorial_exposes_edit_date_without_inheriting_statistical_range(self):
        meta = {"source": "Tactics.tools", "patch": "18.5c", "updated": "2026-11-01T00:10:00+02:00", "sample_start": "2026-10-21", "sample_end": "2026-11-01"}
        self.assertEqual(data_period(meta, editorial=True), "Expert tiers · edited Oct 31, 2026")
        self.assertEqual(data_freshness(meta, editorial=True), "Edited 2026-10-31 22:10 UTC")
        self.assertIn("no statistical match sample", data_period_details(meta, editorial=True))
        self.assertIn("Edited 2026-10-31 22:10 UTC", data_period_details(meta, editorial=True))
        self.assertEqual(data_period({"data_date": "2026-11-01"}, editorial=True), "Expert guidance · no match sample")
        self.assertEqual(data_freshness({"data_date": "2026-11-01"}, editorial=True), "")

    def test_freshness_excludes_period_and_missing_metadata_adds_no_footer_text(self):
        self.assertEqual(data_freshness({"updated": "2026-11-02", "updated_scope": "dataset"}), "Dataset updated 2026-11-02")
        self.assertEqual(data_freshness({"data_date": "2026-11-02"}), "Updated 2026-11-02")
        for meta in (None, {}, [], "2026-11-01"):
            self.assertEqual(data_period(meta), "Match dates not published")
            self.assertEqual(data_freshness(meta), "")
            self.assertIn("does not publish", data_period_details(meta))

    def test_stale_metadata_remains_its_own_snapshot_and_is_not_mutated(self):
        meta = {"source": "ARAMKit", "version": "16.21", "data_date": "2026-10-30", "stale": True}
        original = copy.deepcopy(meta)
        for formatter in (data_period, data_period_details, data_freshness):
            self.assertEqual(formatter(meta), formatter({**meta, "stale": False}))
        self.assertEqual(meta, original)


if __name__ == "__main__":
    unittest.main()
