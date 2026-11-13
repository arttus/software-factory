import tempfile
import unittest
from pathlib import Path

from factory.core.contracts import (
    ContractError,
    Event,
    InvocationRequest,
    InvocationResult,
    new_invocation_id,
    normalize_usage,
    utc_now,
)


class InvocationRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def request(self, **overrides):
        data = dict(
            invocation_id=new_invocation_id(),
            job_id="001-demo",
            stage="build",
            round=1,
            attempt=1,
            role="builder",
            controller_root=str(self.root),
            job_dir=str(self.root / "jobs" / "001-demo"),
            worktree=str(self.root / "worktrees" / "001-demo"),
            instruction_path="factory/roles/builder.md",
            instruction_digest="a" * 64,
            outputs=["build.md"],
            profile="fake-builder",
            adapter="fake",
            model="fake-model",
            required_capabilities=["shell_control"],
            permission_profile="builder",
            created_at=utc_now(),
        )
        data.update(overrides)
        return InvocationRequest(**data)

    def test_valid_request(self):
        self.request().validate()

    def test_relative_job_dir_is_rejected(self):
        with self.assertRaises(ContractError):
            self.request(job_dir="jobs/001-demo").validate()

    def test_absolute_instruction_path_is_rejected(self):
        with self.assertRaises(ContractError):
            self.request(instruction_path=str(self.root / "role.md")).validate()

    def test_attempt_must_start_at_one(self):
        with self.assertRaises(ContractError):
            self.request(attempt=0).validate()

    def test_invocation_id_must_be_uuid4_hex(self):
        with self.assertRaises(ContractError):
            self.request(invocation_id="not-hex").validate()

    def test_round_trip(self):
        request = self.request()
        clone = InvocationRequest.from_dict(request.to_dict())
        self.assertEqual(clone.to_dict(), request.to_dict())


class InvocationResultTests(unittest.TestCase):
    def test_completed_requires_zero_exit(self):
        result = InvocationResult(invocation_id="x", status="completed", exit_code=1,
                                  adapter="fake", started_at=utc_now(), ended_at=utc_now())
        with self.assertRaises(ContractError):
            result.validate()

    def test_cancelled_may_have_no_exit_code(self):
        result = InvocationResult(invocation_id="x", status="cancelled", exit_code=None,
                                  adapter="fake", started_at=utc_now(), ended_at=utc_now())
        result.validate()

    def test_unknown_status_is_rejected(self):
        result = InvocationResult(invocation_id="x", status="ok", adapter="fake",
                                  started_at=utc_now(), ended_at=utc_now())
        with self.assertRaises(ContractError):
            result.validate()

    def test_error_needs_a_message(self):
        result = InvocationResult(invocation_id="x", status="failed", adapter="fake",
                                  started_at=utc_now(), ended_at=utc_now(), error={})
        with self.assertRaises(ContractError):
            result.validate()

    def test_usage_unknown_fields_stay_null(self):
        usage = normalize_usage({"input_tokens": 10})
        self.assertEqual(usage["input_tokens"], 10)
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["cost_usd"])

    def test_unknown_usage_keys_are_rejected(self):
        result = InvocationResult(invocation_id="x", status="failed", adapter="fake",
                                  started_at=utc_now(), ended_at=utc_now(),
                                  usage={"bogus": 1})
        with self.assertRaises(ContractError):
            result.validate()


class EventTests(unittest.TestCase):
    def test_event_types_are_normalized(self):
        Event(invocation_id="x", type="progress", timestamp=utc_now(), data={}).validate()
        with self.assertRaises(ContractError):
            Event(invocation_id="x", type="stdout", timestamp=utc_now(), data={}).validate()


if __name__ == "__main__":
    unittest.main()
