# -*- encoding: utf-8 -*-
"""打分闸门的回归：把「要不要接话」算成一道题，别算错。

为什么值得钉死：这个分数直接决定她**主动说话的数量**。
分数算错一次不会报错，只会让她要么变哑巴、要么变话痨，而且很难查。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault('GEYI_DATA_DIR', tempfile.mkdtemp(prefix='geyi_lik_test_'))

from newGeYi.routing.likelihood import (  # noqa: E402
    FALLBACK_THRESHOLD, Features, contributions, explain, passes,
    resolve_threshold, score, target_ratio, weights_from_config,
)


class ScoreTest(unittest.TestCase):
    def test_mention_beats_nothing(self):
        quiet = Features()
        mentioned = Features(mention_bot=True)
        self.assertGreater(score(mentioned), score(quiet))

    def test_at_other_lowers_score(self):
        base = Features(question=True, recent_count_5m=10)
        at_other = Features(question=True, recent_count_5m=10, at_other=True)
        self.assertLess(score(at_other), score(base))

    def test_self_ratio_and_count(self):
        """她正聊得热乎 → 更容易接着接；群里消息越多 → 越不该硬插。"""
        busy = Features(recent_count_5m=40)
        calm = Features(recent_count_5m=5)
        self.assertLess(score(busy), score(calm))
        engaged = Features(self_ratio_5m=0.5)
        detached = Features(self_ratio_5m=0.0)
        self.assertGreater(score(engaged), score(detached))

    def test_media_only_lowers(self):
        self.assertLess(score(Features(media_only=True)), score(Features()))

    def test_score_in_range(self):
        for features in (Features(), Features(mention_bot=True, question=True),
                         Features(at_other=True, media_only=True, recent_count_5m=999)):
            value = score(features)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_weights_override(self):
        cfg = {'gate_weights': {'mention_bot': 0.0}}
        custom = weights_from_config(cfg)
        self.assertEqual(custom['mention_bot'], 0.0)
        self.assertEqual(score(Features(mention_bot=True), custom),
                         score(Features(mention_bot=False), custom))

    def test_contributions_add_up(self):
        features = Features(mention_bot=True, question=True, recent_count_5m=5)
        total = sum(contributions(features).values())
        self.assertAlmostEqual(1 / (1 + 2.718281828 ** (-total)), score(features), places=6)

    def test_explain_is_readable(self):
        text = explain(Features(mention_bot=True))
        self.assertIn('mention_bot', text)


class ThresholdTest(unittest.TestCase):
    def test_target_ratio(self):
        self.assertAlmostEqual(target_ratio(6, 60), 0.1)
        self.assertAlmostEqual(target_ratio(0, 60), 0.0)
        self.assertAlmostEqual(target_ratio(100, 60), 1.0)
        # 批次为 0 时不能除零
        self.assertEqual(target_ratio(3, 0), 1.0)

    def test_fallback_when_few_samples(self):
        self.assertEqual(resolve_threshold([0.9, 0.8], 0.5), FALLBACK_THRESHOLD)

    def test_quantile_from_history(self):
        scores = [i / 100.0 for i in range(1, 101)]     # 0.01 ~ 1.00
        threshold = resolve_threshold(scores, 0.2)
        self.assertGreater(threshold, 0.7)
        self.assertLess(threshold, 0.9)

    def test_extremes(self):
        scores = [0.5] * 30
        self.assertGreater(resolve_threshold(scores, 0.0), 1.0)   # 谁都不许过
        self.assertEqual(resolve_threshold(scores, 1.0), 0.0)     # 全都放行

    def test_passes(self):
        self.assertTrue(passes(0.5, 0.5))
        self.assertFalse(passes(0.49, 0.5))


if __name__ == '__main__':
    unittest.main()
