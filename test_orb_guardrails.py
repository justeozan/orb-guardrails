"""Tests for each guardrail of orb_guardrails (standard library unittest only)."""

import asyncio
import io
import logging
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass, replace

from orb_guardrails import (
    DAY,
    BranchGuard,
    BranchRefused,
    Budget,
    BudgetExceeded,
    CallTimeout,
    CallTooExpensive,
    CircuitBreaker,
    FakeEngine,
    Issue,
    Limits,
    Orchestrator,
    Project,
    Request,
    Result,
    main,
)

LOGGER = "orb_guardrails"
ASK = Request("implement", "shop", 1, 1)


class FakeClock:
    """A clock the tests move by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(slots=True)
class ScriptedEngine:
    """Answers with the given qualities in order (the last one repeats) and counts calls."""

    name: str
    qualities: list[float]
    price: float = 0.01
    delay: float = 0.0
    calls: int = 0

    async def run(self, request: Request) -> Result:
        quality = self.qualities[min(self.calls, len(self.qualities) - 1)]
        self.calls += 1
        await asyncio.sleep(self.delay)
        return Result(self.price, quality)


def make(limits: Limits | None = None, implement: float = 0.9, review: float = 0.9):
    """Build an orchestrator with scripted engines that always give these qualities."""
    builder = ScriptedEngine("builder", [implement])
    critic = ScriptedEngine("critic", [review])
    return Orchestrator(limits or Limits(), implementer=builder, reviewer=critic), builder, critic


class BudgetTests(unittest.TestCase):
    def test_refuses_calls_beyond_the_rolling_budget(self) -> None:
        clock = FakeClock()
        budget = Budget(limit=1.0, per_call_cap=1.0, clock=clock)
        budget.authorize(0.6)
        budget.settle(0.6, 0.6)
        with self.assertRaises(BudgetExceeded):
            budget.authorize(0.6)

    def test_budget_is_rolling_not_a_calendar_day(self) -> None:
        clock = FakeClock()
        budget = Budget(limit=1.0, per_call_cap=1.0, clock=clock)
        budget.authorize(0.9)
        budget.settle(0.9, 0.9)
        clock.advance(DAY + 1)
        self.assertEqual(budget.spent(), 0.0)
        budget.authorize(0.9)  # the old spend left the window

    def test_reservations_stop_parallel_calls_from_overspending(self) -> None:
        budget = Budget(limit=1.0, per_call_cap=1.0, clock=FakeClock())
        budget.authorize(0.6)  # still running, nothing settled yet
        with self.assertRaises(BudgetExceeded):
            budget.authorize(0.6)

    def test_per_call_cap_refuses_an_expensive_call(self) -> None:
        budget = Budget(limit=10.0, per_call_cap=0.3, clock=FakeClock())
        with self.assertRaises(CallTooExpensive):
            budget.authorize(0.5)


class BreakerTests(unittest.TestCase):
    def test_opens_after_n_consecutive_failures_and_recovers_after_cooldown(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(threshold=3, cooldown=10.0, clock=clock)
        self.assertFalse(breaker.record(False))
        self.assertFalse(breaker.record(False))
        self.assertTrue(breaker.record(False))  # third failure in a row opens it
        self.assertEqual(breaker.remaining(), 10.0)
        clock.advance(9)
        self.assertGreater(breaker.remaining(), 0)
        clock.advance(1)
        self.assertEqual(breaker.remaining(), 0.0)  # cooldown over: allowed again

    def test_a_success_resets_the_streak(self) -> None:
        breaker = CircuitBreaker(threshold=2, cooldown=10.0, clock=FakeClock())
        breaker.record(False)
        breaker.record(True)
        self.assertFalse(breaker.record(False))

    def test_half_open_probe_failure_reopens_immediately(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(threshold=3, cooldown=10.0, clock=clock)
        for _ in range(3):
            breaker.record(False)
        clock.advance(10)
        self.assertEqual(breaker.remaining(), 0.0)
        self.assertTrue(breaker.record(False))
        self.assertGreater(breaker.remaining(), 0)


class BranchGuardTests(unittest.TestCase):
    def test_only_the_prefix_is_allowed(self) -> None:
        guard = BranchGuard("orb/")
        guard.check("orb/shop/1")
        for branch in ("main", "feature/x", "orbit/x", "orb/../main", "orb//x"):
            with self.subTest(branch=branch), self.assertRaises(BranchRefused):
                guard.check(branch)


class CallTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_that_exceeds_the_timeout_is_cancelled_and_billed(self) -> None:
        slow = ScriptedEngine("builder", [0.9], delay=5.0)
        orb = Orchestrator(
            Limits(call_timeout=0.05), implementer=slow, reviewer=ScriptedEngine("critic", [0.9])
        )
        with self.assertRaises(CallTimeout), self.assertLogs(LOGGER) as logs:
            await orb.call(slow, ASK)
        self.assertIn("TIMEOUT", logs.output[0])
        self.assertAlmostEqual(orb.budget.spent(), 0.01)  # worst case is charged

    async def test_call_refused_when_the_budget_is_exhausted(self) -> None:
        orb, builder, _ = make(Limits(budget_24h=0.005))
        with self.assertRaises(BudgetExceeded), self.assertLogs(LOGGER):
            await orb.call(builder, ASK)
        self.assertEqual(builder.calls, 0)  # the engine was never reached

    async def test_call_refused_above_the_per_call_cap(self) -> None:
        orb, builder, _ = make(Limits(per_call_cap=0.05))
        with self.assertRaises(CallTooExpensive), self.assertLogs(LOGGER):
            await orb.call(builder, replace(ASK, size=9))  # quote 0.09 > 0.05
        self.assertEqual(builder.calls, 0)

    async def test_reviewer_must_be_a_different_engine(self) -> None:
        with self.assertRaises(ValueError):
            Orchestrator(Limits(), implementer=FakeEngine("same"), reviewer=FakeEngine("same"))


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_lap_cap_stops_the_inner_loop(self) -> None:
        orb, builder, critic = make(Limits(max_laps=3), implement=0.0)  # checks never pass
        with self.assertLogs(LOGGER):
            outcome = await orb.attempt(Project("shop"), Issue(1, "x"))
        self.assertEqual(outcome, "failed")
        self.assertEqual(builder.calls, 3)
        self.assertEqual(critic.calls, 0)  # review only happens once checks are green

    async def test_failed_review_sends_feedback_back_to_implement(self) -> None:
        orb, builder, critic = make(Limits(max_laps=2), review=0.0)
        with self.assertLogs(LOGGER):
            outcome = await orb.attempt(Project("shop"), Issue(1, "x"))
        self.assertEqual((outcome, builder.calls, critic.calls), ("failed", 2, 2))

    async def test_approved_patch_opens_a_pull_request_on_an_allowed_branch(self) -> None:
        orb, _, _ = make()
        issue = Issue(1, "x")
        with self.assertLogs(LOGGER):
            await orb.work_issue(Project("shop"), issue)
        self.assertEqual(issue.status, "done")
        self.assertEqual(orb.forge.pushed, ["orb/shop/1"])
        self.assertEqual(orb.forge.pull_requests, ["orb/shop/1"])

    async def test_issue_is_blocked_after_n_failed_attempts(self) -> None:
        orb, builder, _ = make(Limits(max_laps=1, max_attempts=2), implement=0.0)
        issue = Issue(1, "x")
        with self.assertLogs(LOGGER):
            await orb.work_issue(Project("shop"), issue)
        self.assertEqual((issue.status, issue.attempts), ("blocked", 2))
        self.assertEqual(builder.calls, 2)
        self.assertEqual(orb.forge.blocked, ["shop#1"])  # handed to a human

    async def test_branch_guard_blocks_an_agent_pushing_outside_the_prefix(self) -> None:
        orb, _, _ = make()
        issue = Issue(1, "x", wants_branch="main")
        with self.assertLogs(LOGGER) as logs:
            await orb.work_issue(Project("shop"), issue)
        self.assertEqual(issue.status, "blocked")
        self.assertEqual(orb.forge.pushed, [])
        self.assertTrue(any("BRANCH" in line for line in logs.output))

    async def test_exhausted_budget_defers_the_issue_without_blaming_it(self) -> None:
        orb, builder, _ = make(Limits(budget_24h=0.005))
        issue = Issue(1, "x")
        with self.assertLogs(LOGGER):
            await orb.work_issue(Project("shop"), issue)
        self.assertEqual((issue.status, issue.attempts, builder.calls), ("deferred", 0, 0))

    async def test_breaker_pauses_a_failing_project(self) -> None:
        limits = Limits(max_laps=1, max_attempts=9, breaker_threshold=2, breaker_cooldown=0.2)
        orb, builder, _ = make(limits, implement=0.0)
        issue = Issue(1, "x")
        project = Project("shop", issues=[issue])
        with self.assertLogs(LOGGER) as logs:
            task = asyncio.create_task(orb.run_all([project]))
            await asyncio.sleep(0.1)
            self.assertGreater(orb.breaker(project).remaining(), 0)  # open
            self.assertEqual(builder.calls, 2)  # no third run while paused
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(any("paused" in line for line in logs.output))

    async def test_semaphore_caps_runs_in_parallel_across_projects(self) -> None:
        builder = ScriptedEngine("builder", [0.9], delay=0.02)
        orb = Orchestrator(
            Limits(max_parallel=2), implementer=builder, reviewer=ScriptedEngine("critic", [0.9])
        )
        projects = [Project(f"p{n}", issues=[Issue(1, "x")]) for n in range(6)]
        with self.assertLogs(LOGGER):
            await orb.run_all(projects)
        self.assertEqual(orb.peak, 2)  # six projects wanted to run, never more than two did
        self.assertTrue(all(i.status == "done" for p in projects for i in p.issues))


class DryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_logs_external_writes_and_performs_none(self) -> None:
        orb, _, _ = make(Limits(dry_run=True))
        issue = Issue(1, "x")
        with self.assertLogs(LOGGER) as logs:
            await orb.work_issue(Project("shop"), issue)
        self.assertEqual(issue.status, "done")
        self.assertEqual((orb.forge.pushed, orb.forge.pull_requests), ([], []))
        self.assertTrue(any("would push branch" in line for line in logs.output))

    async def test_dry_run_still_enforces_the_branch_guard(self) -> None:
        orb, _, _ = make(Limits(dry_run=True))
        issue = Issue(1, "x", wants_branch="main")
        with self.assertLogs(LOGGER):
            await orb.work_issue(Project("shop"), issue)
        self.assertEqual(issue.status, "blocked")


class DemoTests(unittest.TestCase):
    def test_demo_runs_offline_and_exits_zero(self) -> None:
        root = logging.getLogger()
        self.addCleanup(setattr, root, "handlers", list(root.handlers))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--seed", "7", "--dry-run", "--parallel", "1"])
        self.assertEqual(code, 0)
        self.assertIn("Summary", out.getvalue())


if __name__ == "__main__":
    unittest.main()
