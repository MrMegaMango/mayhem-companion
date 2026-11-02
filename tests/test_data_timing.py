import copy
import unittest

from mayhem.data_timing import data_timing


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


if __name__ == "__main__":
    unittest.main()
