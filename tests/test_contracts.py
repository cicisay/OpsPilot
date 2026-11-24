import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.contracts import (  # noqa: E402
    ContractError,
    build_verdict,
    normalize_experiment,
    normalize_proposal,
    normalize_task_order,
    require,
    validate_experiment,
    validate_proposal,
    validate_task_order,
    validate_verdict,
)


def task(**overrides) -> dict:
    base = {
        "task_id": "run-1:p1",
        "run_id": "run-1",
        "product_id": "p1",
        "title": "3-Person Pop-Up Beach Tent",
        "category": "tents",
        "problem": "low_order",
        "problem_detail": "Traffic is on par but order rate is 40% below the category median",
        "evidence": ["PV 4200 vs median 3900", "order rate 0.7% vs 1.9%"],
        "hypothesis": "Leading with tier pricing lifts order rate by at least 15%",
        "success_metric": "order_rate",
        "min_lift": 0.15,
        "window_days": 14,
        "price_low_cents": 1999,
        "price_high_cents": 2599,
        "price_points": [1999, 2299, 2599],
        "variant_min": 2,
        "variant_max": 4,
    }
    base.update(overrides)
    return normalize_task_order(base)


def variant(index: int = 0, **overrides) -> dict:
    base = {
        "variant_id": f"v{index}",
        "angle": f"angle-{index}",
        "title": f"Title {index}",
        "bullets": ["Bulk pricing from 100 pcs", "Sample before full order"],
        "description": "A tent for buyers who order in volume.",
        "price_index": index,
        "material_brief": ["Show the packed size", "Caption the MOQ"],
        "self_review": "Facts match the listing",
    }
    base.update(overrides)
    return base


def proposal(count: int = 3, **overrides) -> dict:
    base = {
        "task_id": "run-1:p1",
        "product_id": "p1",
        "variant_count": count,
        "variant_reason": "Wide price band and deep stock support three angles",
        "variants": [variant(index) for index in range(count)],
    }
    base.update(overrides)
    return normalize_proposal(base)


class TaskOrderTests(unittest.TestCase):
    def test_valid_task_has_no_errors(self):
        self.assertEqual(validate_task_order(task()), [])

    def test_missing_evidence_is_rejected(self):
        errors = validate_task_order(task(evidence=["only one"]))
        self.assertTrue(any("evidence" in item for item in errors))

    def test_missing_problem_detail_is_rejected(self):
        errors = validate_task_order(task(problem_detail=""))
        self.assertTrue(any("problem_detail" in item for item in errors))

    def test_window_outside_range_is_rejected(self):
        self.assertTrue(validate_task_order(task(window_days=3)))
        self.assertTrue(validate_task_order(task(window_days=90)))

    def test_reversed_price_band_is_normalised(self):
        doc = task(price_low_cents=2599, price_high_cents=1999, price_points=[1999, 2299, 2599])
        self.assertEqual(doc["price_low_cents"], 1999)
        self.assertEqual(doc["price_high_cents"], 2599)
        self.assertEqual(validate_task_order(doc), [])

    def test_price_point_outside_band_is_rejected(self):
        errors = validate_task_order(task(price_points=[1999, 9999]))
        self.assertTrue(any("outside band" in item for item in errors))

    def test_duplicate_price_points_are_rejected(self):
        errors = validate_task_order(task(price_points=[1999, 1999, 2599]))
        self.assertTrue(any("duplicates" in item for item in errors))

    def test_reversed_variant_range_is_normalised(self):
        doc = task(variant_min=4, variant_max=2)
        self.assertEqual((doc["variant_min"], doc["variant_max"]), (2, 4))
        self.assertEqual(validate_task_order(doc), [])

    def test_variant_range_outside_limits_is_rejected(self):
        self.assertTrue(validate_task_order(task(variant_min=1, variant_max=9)))

    def test_min_lift_must_be_a_fraction(self):
        self.assertTrue(validate_task_order(task(min_lift=0)))
        self.assertTrue(validate_task_order(task(min_lift=1.5)))

    def test_require_raises_with_all_errors(self):
        with self.assertRaises(ContractError) as caught:
            require("task", ["a", "b"])
        self.assertEqual(caught.exception.stage, "task")
        self.assertEqual(caught.exception.errors, ["a", "b"])


class ProposalTests(unittest.TestCase):
    def test_valid_proposal_passes(self):
        self.assertEqual(validate_proposal(proposal(3), task()), [])

    def test_count_mismatch_is_rejected(self):
        doc = proposal(3)
        doc["variant_count"] = 4
        self.assertTrue(any("does not match" in item for item in validate_proposal(doc)))

    def test_outside_task_range_is_rejected(self):
        doc = proposal(5)
        errors = validate_proposal(doc, task(variant_min=2, variant_max=3))
        self.assertTrue(any("outside task range" in item for item in errors))

    def test_duplicate_angles_are_rejected(self):
        doc = proposal(3)
        doc["variants"][1]["angle"] = doc["variants"][0]["angle"]
        errors = validate_proposal(doc)
        self.assertTrue(any("duplicate angle" in item for item in errors))

    def test_too_few_bullets_is_rejected(self):
        doc = proposal(2)
        doc["variants"][0]["bullets"] = ["only one"]
        self.assertTrue(any("bullets" in item for item in validate_proposal(doc)))

    def test_missing_price_index_is_rejected(self):
        doc = proposal(2)
        doc["variants"][0]["price_index"] = -1
        self.assertTrue(any("price_index" in item for item in validate_proposal(doc)))

    def test_empty_variants_is_rejected(self):
        doc = proposal(2)
        doc["variants"] = []
        self.assertTrue(any("no variants" in item for item in validate_proposal(doc)))


class VerdictTests(unittest.TestCase):
    def test_severity_follows_the_worst_check(self):
        doc = build_verdict(
            {
                "variant_id": "v1",
                "checks": {
                    "c1_absolute": "block",
                    "c2_ip": "pass",
                    "c3_category": "pass",
                    "c4_market": "warn",
                },
                "findings": [
                    {
                        "category": "c1_absolute",
                        "quote": "100% effective",
                        "rule": "ABS-001",
                        "suggestion": "State the test condition or drop the claim",
                    }
                ],
            }
        )
        self.assertEqual(doc["severity"], "block")
        self.assertEqual(validate_verdict(doc), [])

    def test_model_cannot_downgrade_its_own_block(self):
        doc = build_verdict(
            {
                "variant_id": "v1",
                "checks": {"c1_absolute": "block", "c2_ip": "pass", "c3_category": "pass", "c4_market": "pass"},
                "severity": "pass",
                "findings": [{"category": "c1", "quote": "q", "rule": "r", "suggestion": "s"}],
            }
        )
        self.assertEqual(doc["severity"], "block")

    def test_missing_check_is_rejected(self):
        doc = build_verdict({"variant_id": "v1", "checks": {"c1_absolute": "pass"}})
        doc["checks"].pop("c2_ip", None)
        errors = validate_verdict(doc)
        self.assertTrue(any("missing compliance checks" in item for item in errors))

    def test_unknown_check_state_is_rejected(self):
        doc = {
            "variant_id": "v1",
            "checks": {
                "c1_absolute": "maybe",
                "c2_ip": "pass",
                "c3_category": "pass",
                "c4_market": "pass",
            },
            "severity": "pass",
            "findings": [],
        }
        self.assertTrue(any("invalid state" in item for item in validate_verdict(doc)))

    def test_block_without_finding_is_rejected(self):
        doc = build_verdict(
            {
                "variant_id": "v1",
                "checks": {"c1_absolute": "block", "c2_ip": "pass", "c3_category": "pass", "c4_market": "pass"},
                "findings": [],
            }
        )
        self.assertTrue(any("cite at least one finding" in item for item in validate_verdict(doc)))

    def test_severity_contradiction_is_rejected(self):
        doc = build_verdict({"variant_id": "v1", "checks": {"c1_absolute": "pass"}})
        doc["severity"] = "block"
        doc["findings"] = [{"category": "c1", "quote": "q", "rule": "r", "suggestion": "s"}]
        self.assertTrue(any("contradicts checks" in item for item in validate_verdict(doc)))


class ExperimentTests(unittest.TestCase):
    def base(self, **overrides) -> dict:
        payload = {
            "experiment_id": "p1:v1:2026-11-03",
            "product_id": "p1",
            "variant_id": "v1",
            "started_at": "2026-11-03T10:00:00",
            "window_days": 14,
            "baseline": {"order_rate": 0.007, "pv": 4200},
            "status": "observing",
        }
        payload.update(overrides)
        return normalize_experiment(payload)

    def test_observing_experiment_passes(self):
        self.assertEqual(validate_experiment(self.base()), [])

    def test_judged_experiment_needs_a_verdict(self):
        doc = self.base(status="verdict")
        self.assertTrue(any("needs one of" in item for item in validate_experiment(doc)))

    def test_observing_experiment_cannot_carry_verdict(self):
        doc = self.base(status="observing", verdict="supported")
        self.assertTrue(any("must not carry a verdict" in item for item in validate_experiment(doc)))

    def test_missing_baseline_is_rejected(self):
        doc = self.base(baseline={})
        self.assertTrue(any("baseline" in item for item in validate_experiment(doc)))

    def test_unknown_verdict_state_is_dropped(self):
        doc = self.base(status="verdict", verdict="probably")
        self.assertEqual(doc["verdict"], "")
        self.assertTrue(validate_experiment(doc))


if __name__ == "__main__":
    unittest.main()
