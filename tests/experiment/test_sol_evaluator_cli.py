from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from scripts.experiment.evaluation import sol_evaluator
from scripts.experiment.llm.protocol import ROOT


class SolEvaluatorCliTests(unittest.TestCase):
    def test_relative_output_path_is_resolved_before_evaluation_and_reporting(self):
        expected = ROOT / "docs/experiment/validation/sol-e2e-v1/evaluation"
        fake_result = {"cases": [{}] * 10,
                       "manifest": {"evaluator_commit": "synthetic-commit"}}
        output = io.StringIO()
        with patch.object(sol_evaluator, "evaluate_sol_pilot", return_value=fake_result) as evaluate, \
             contextlib.redirect_stdout(output):
            code = sol_evaluator.main([
                "--protocol", "docs/experiment/validation/sol-e2e-v1/llm/protocol-sol-e2e-v1.json",
                "--run-root", "docs/experiment/validation/sol-e2e-v1/llm/run",
                "--physical-root", "docs/experiment/validation/sol-e2e-v1/physical",
                "--output", "docs/experiment/validation/sol-e2e-v1/evaluation",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(evaluate.call_args.args[3], expected)
        self.assertEqual(json.loads(output.getvalue())["output"],
                         "docs/experiment/validation/sol-e2e-v1/evaluation")


if __name__ == "__main__":
    unittest.main()
