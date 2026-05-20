from __future__ import annotations

import unittest

from snap_dashboard.testing.baselines import ScreenshotAsset, pair_screenshots


class PairScreenshotsTests(unittest.TestCase):
    def test_pairs_matching_names_first(self) -> None:
        baseline = [
            ScreenshotAsset("home.png", "baseline-home", "YQ=="),
            ScreenshotAsset("prefs.png", "baseline-prefs", "Yg=="),
        ]
        new = [
            ScreenshotAsset("prefs.png", "new-prefs", "Yw=="),
            ScreenshotAsset("home.png", "new-home", "ZA=="),
        ]

        pairs = pair_screenshots(baseline, new)

        self.assertEqual(
            [(left.image_name, right.image_name) for left, right in pairs],
            [("home.png", "home.png"), ("prefs.png", "prefs.png")],
        )

    def test_falls_back_to_first_pair_when_names_do_not_match(self) -> None:
        baseline = [ScreenshotAsset("baseline.png", "baseline", "YQ==")]
        new = [ScreenshotAsset("new.png", "new", "Yg==")]

        pairs = pair_screenshots(baseline, new)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0].image_name, "baseline.png")
        self.assertEqual(pairs[0][1].image_name, "new.png")


if __name__ == "__main__":
    unittest.main()
