"""搜一搜采集筛选的安全默认值。"""

import unittest

from wxsearch.config import Selectors, _DEFAULTS


class FilterDefaultTests(unittest.TestCase):
    def test_default_filter_is_latest_articles_from_one_day(self):
        self.assertEqual(_DEFAULTS["selectors"]["filter_sort"], "最新")
        self.assertEqual(_DEFAULTS["selectors"]["filter_type"], "文章")
        self.assertEqual(_DEFAULTS["selectors"]["filter_time"], "最近一天")

    def test_selector_dataclass_matches_runtime_default(self):
        self.assertEqual(Selectors().filter_time, "最近一天")


if __name__ == "__main__":
    unittest.main()
