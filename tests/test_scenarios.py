"""场景目录测试：6 个标准场景 × GLM/DeepSeek 对称性 + 崩溃边界声明。

约定：GLM 与 DeepSeek 的 6 个标准场景必须参数全等（仅 model/tokenizer 不同），
这样两个模型的对比才站得住；速率按实测崩溃边界取安全档。
"""

from __future__ import annotations

import unittest

from sglbench.runner import build_command
from sglbench.scenarios import BUILTIN_SCENARIOS, DECODE_MASTER, DEEPSEEK, GLM, PREFILL_MASTER, ROUTER

# 6 个标准场景的 (GLM key, DeepSeek key)
PAIRS = [
    ("single-standard", "single-deepseek-standard"),          # ① 常规文本单并发
    ("single-longctx", "single-longctx-deepseek"),            # ② 超长上下文单并发（200K 基线）
    ("conc-32-standard", "conc-32-deepseek"),                 # ③ 低并发（Decode 满配）
    ("conc-64-standard", "conc-64-deepseek"),                 # ④ 中并发（Prefill 满配）
    ("conc-128-safe", "conc-128-deepseek-safe"),              # ⑤ 高并发 128（安全档）
    ("conc-32-longctx", "conc-32-longctx-deepseek"),          # ⑥ 多并发超长上下文压力
]

BOUNDARY_FIELDS = ("num_prompts", "max_concurrency", "random_input", "random_output", "request_rate")


class TestMatrixSymmetry(unittest.TestCase):
    def test_all_keys_exist(self) -> None:
        for glm_key, ds_key in PAIRS:
            self.assertIn(glm_key, BUILTIN_SCENARIOS, glm_key)
            self.assertIn(ds_key, BUILTIN_SCENARIOS, ds_key)

    def test_pairs_identical_except_model_and_backend(self) -> None:
        """同场景 GLM/DS 只在 model/tokenizer/backend 上不同，其余参数必须一致。

        backend 约定：GLM=sglang-oai-chat（对齐生产调用形态）、DS=sglang。
        """
        for glm_key, ds_key in PAIRS:
            glm = BUILTIN_SCENARIOS[glm_key]
            ds = BUILTIN_SCENARIOS[ds_key]
            for field in BOUNDARY_FIELDS:
                self.assertEqual(getattr(glm, field), getattr(ds, field), f"{field} @ {glm_key}/{ds_key}")
            for field in ("base_url", "dataset_name", "dataset_path", "pd_separated", "random_range_ratio"):
                self.assertEqual(getattr(glm, field), getattr(ds, field), f"{field} @ {glm_key}/{ds_key}")
            self.assertEqual(glm.backend, "sglang-oai-chat", glm_key)
            self.assertEqual(ds.backend, "sglang", ds_key)
            self.assertEqual(glm.model, GLM)
            self.assertEqual(ds.model, DEEPSEEK)
            self.assertEqual(glm.tokenizer, glm.model)
            self.assertEqual(ds.tokenizer, ds.model)

    def test_all_full_link_scenarios_via_router(self) -> None:
        """全链路场景统一走 sglang router（backend=sglang + --pd-separated），不走 EPP。"""
        for glm_key, ds_key in PAIRS:
            for key in (glm_key, ds_key):
                sc = BUILTIN_SCENARIOS[key]
                self.assertEqual(sc.base_url, ROUTER, key)
                self.assertTrue(sc.pd_separated, key)
                cmd = build_command(sc, python="python3")
                self.assertIn("--pd-separated", cmd, key)

    def test_model_paths_are_container_relative(self) -> None:
        """所有模型/数据集路径都是容器内路径（压测在容器/pod 里执行）。"""
        for sc in BUILTIN_SCENARIOS.values():
            self.assertTrue(sc.model.startswith("/workspace/"), sc.key)
            self.assertTrue(sc.dataset_path.endswith("ShareGPT_V3_unfiltered_cleaned_split.json"), sc.key)
            self.assertFalse(sc.model.startswith("/data/"), f"{sc.key} 用了宿主路径 /data/…")


class TestBoundaries(unittest.TestCase):
    def test_128_safe_is_5_0(self) -> None:
        for key in ("conc-128-safe", "conc-128-deepseek-safe"):
            sc = BUILTIN_SCENARIOS[key]
            self.assertEqual(sc.max_concurrency, 128)
            self.assertEqual(sc.request_rate, 5.0, key)
            self.assertIn("边界", sc.description)

    def test_crash_probe_declares_boundary(self) -> None:
        probe = BUILTIN_SCENARIOS["conc-128-crash-probe"]
        self.assertEqual(probe.model, DEEPSEEK)
        self.assertEqual(probe.request_rate, 8.0)
        self.assertIn("known-failure", probe.tags)
        self.assertIn("崩溃", probe.description)

    def test_256_declares_decode_crash_boundary(self) -> None:
        for key in ("conc-256-standard", "conc-256-deepseek"):
            sc = BUILTIN_SCENARIOS[key]
            self.assertEqual(sc.request_rate, 16.0, key)
            self.assertIn("decode 重启", sc.description)

    def test_sharegpt_protocol_matches_optimization(self) -> None:
        sc = BUILTIN_SCENARIOS["conc-64-sharegpt-deepseek"]
        self.assertEqual(sc.dataset_name, "sharegpt")
        self.assertEqual((sc.random_input, sc.random_output, sc.request_rate), (512, 128, 20.0))


class TestSingleStage(unittest.TestCase):
    def test_prefill_only_targets_master(self) -> None:
        for key in ("prefill-only", "prefill-only-deepseek"):
            sc = BUILTIN_SCENARIOS[key]
            self.assertEqual(sc.host, PREFILL_MASTER[0], key)
            self.assertEqual(sc.port, PREFILL_MASTER[1], key)
            self.assertFalse(sc.pd_separated, key)
            self.assertIsNone(sc.base_url, key)

    def test_decode_only_targets_master_modelserver(self) -> None:
        for key in ("decode-only", "decode-only-deepseek"):
            sc = BUILTIN_SCENARIOS[key]
            self.assertEqual(sc.host, DECODE_MASTER[0], key)
            self.assertEqual(sc.port, DECODE_MASTER[1], key)
            self.assertFalse(sc.pd_separated, key)


if __name__ == "__main__":
    unittest.main()
