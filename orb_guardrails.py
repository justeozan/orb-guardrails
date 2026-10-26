"""Guardrails for an unattended autonomous coding loop, as a tiny offline demo.

An agent that writes code while you sleep needs hard limits. This script implements the ones
that matter and runs them against simulated engines (no network, standard library only):

* a rolling 24 h spend budget and a per-call cost cap;
* a timeout on every agent call;
* a circuit breaker per project;
* a global cap on runs in parallel;
* a capped inner loop: implement, deterministic checks, review by a different engine;
* a maximum number of failed attempts per issue, then a human takes over;
* a branch-scope guard: only branches under a prefix can be pushed;
* a dry-run mode: external writes are logged, never performed.

Run ``python orb_guardrails.py --help`` for the demo options.
"""

import argparse
import asyncio
import logging
import posixpath
import random
import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

log = logging.getLogger("orb_guardrails")

Clock = Callable[[], float]
Outcome = Literal["pr", "failed", "refused", "blocked"]

DAY = 24 * 3600.0
CHECKS_MIN = 0.40  # a patch below this quality fails the deterministic checks
REVIEW_MIN = 0.65  # a patch below this quality is not approved by the reviewer


def say(tag: str, scope: str, message: str) -> None:
    """Emit one narrated, column-aligned log line."""
    log.info("%-9s %-17s %s", tag, scope, message)


@dataclass(frozen=True, slots=True)
class Limits:
    """Every knob of the guardrails in one place."""

    budget_24h: float = 1.6
    per_call_cap: float = 0.30
    call_timeout: float = 0.5
    breaker_threshold: int = 3
    breaker_cooldown: float = 1.0
    max_parallel: int = 2
    max_laps: int = 3
    max_attempts: int = 2
    branch_prefix: str = "orb/"
    dry_run: bool = False


class BudgetExceeded(Exception):
    """The rolling spend budget would be exceeded: the call is refused."""


class CallTooExpensive(BudgetExceeded):
    """A single call would cost more than the per-call cap."""


class CallTimeout(Exception):
    """An agent call did not answer within the per-call timeout."""


class BranchRefused(Exception):
    """A push targeted a branch outside the allowed prefix."""


class Budget:
    """Rolling 24 h spend budget with a per-call cap.

    A call is authorised against its worst-case quote *before* it starts and that quote is
    reserved, so parallel calls cannot overspend together. When the call ends, the reservation
    is replaced by the actual cost.
    """

    def __init__(self, limit: float, per_call_cap: float, clock: Clock = time.monotonic) -> None:
        self.limit = limit
        self.per_call_cap = per_call_cap
        self._clock = clock
        self._entries: deque[tuple[float, float]] = deque()  # (time, cost), oldest first
        self._reserved = 0.0

    def spent(self) -> float:
        """Return what was spent during the last 24 hours."""
        while self._entries and self._entries[0][0] <= self._clock() - DAY:
            self._entries.popleft()
        return sum(cost for _, cost in self._entries)

    def authorize(self, quote: float) -> None:
        """Reserve ``quote``, or raise if the per-call cap or the 24 h budget forbids it."""
        if quote > self.per_call_cap:
            raise CallTooExpensive(f"{quote:.2f} > cap {self.per_call_cap:.2f} per call")
        if self.spent() + self._reserved + quote > self.limit:
            raise BudgetExceeded(f"{self.spent():.2f} spent + {quote:.2f} > {self.limit:.2f} / 24h")
        self._reserved += quote

    def settle(self, quote: float, actual: float) -> None:
        """Release a reservation and record what the call really cost."""
        self._reserved = max(0.0, self._reserved - quote)
        self._entries.append((self._clock(), actual))


class CircuitBreaker:
    """Pause a project after N consecutive failed runs, then probe again.

    After the cooldown the breaker is half-open: one more failure reopens it immediately,
    one success closes it for good.
    """

    def __init__(self, threshold: int, cooldown: float, clock: Clock = time.monotonic) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    def remaining(self) -> float:
        """Return the seconds left in the pause (0 when runs are allowed)."""
        if self._opened_at is None:
            return 0.0
        left = self._opened_at + self.cooldown - self._clock()
        if left > 0:
            return left
        self._opened_at = None
        self._failures = self.threshold - 1  # half-open: a single failure reopens
        return 0.0

    def record(self, success: bool) -> bool:
        """Record a run result; return True when this failure opened the breaker."""
        self._failures = 0 if success else self._failures + 1
        if success or self._failures < self.threshold or self._opened_at is not None:
            return False
        self._opened_at = self._clock()
        return True


class BranchGuard:
    """Refuse any branch outside a prefix, whatever the agent asks for."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def check(self, branch: str) -> None:
        """Raise :class:`BranchRefused` unless ``branch`` is under the prefix."""
        if posixpath.normpath(branch) != branch or not branch.startswith(self.prefix):
            raise BranchRefused(f"'{branch}' is outside '{self.prefix}*'")  # also 'orb/../main'


class Forge:
    """A fake code host: the only place where the loop writes to the outside world."""

    def __init__(self, guard: BranchGuard, dry_run: bool = False) -> None:
        self.guard = guard
        self.dry_run = dry_run
        self.pushed: list[str] = []
        self.pull_requests: list[str] = []
        self.blocked: list[str] = []

    def _write(self, action: str, ref: str, sink: list[str]) -> None:
        if self.dry_run:
            say("DRY-RUN", ref, f"would {action}")
        else:
            sink.append(ref)

    def push(self, branch: str) -> None:
        """Push a branch, after the scope guard approved it."""
        self.guard.check(branch)
        self._write("push branch", branch, self.pushed)

    def open_pr(self, branch: str) -> None:
        """Open a pull request; a human merges it."""
        self.guard.check(branch)
        self._write("open a pull request", branch, self.pull_requests)

    def mark_blocked(self, ref: str) -> None:
        """Label an issue as blocked so a human picks it up."""
        self._write("label the issue 'blocked'", ref, self.blocked)


@dataclass(frozen=True, slots=True)
class Request:
    """What the loop asks an engine to do."""

    step: str  # "implement" or "review"
    project: str
    issue: int
    lap: int
    size: int = 1
    difficulty: float = 0.0
    patch_quality: float = 0.0


@dataclass(frozen=True, slots=True)
class Result:
    """What an engine answers: what it cost and a quality score."""

    cost: float
    quality: float


class Engine(Protocol):
    """Anything that can implement a patch or review one."""

    name: str
    price: float

    async def run(self, request: Request) -> Result:
        """Perform the call."""
        ...


def quote(engine: Engine, request: Request) -> float:
    """Return the worst-case cost of a call: implementing scales with the issue size."""
    return engine.price * (request.size if request.step == "implement" else 1)


@dataclass(slots=True)
class FakeEngine:
    """Simulated engine: seeded cost, quality and latency, and it sometimes hangs."""

    name: str
    seed: int = 0
    price: float = 0.10
    skill: float = 1.0
    hang_rate: float = 0.0
    latency: tuple[float, float] = (0.05, 0.25)

    async def run(self, request: Request) -> Result:
        """Draw the outcome from a per-call seed, then sleep to simulate the latency."""
        key = f"{self.seed}|{self.name}|{request.project}|{request.issue}|{request.step}"
        rng = random.Random(f"{key}|{request.lap}")
        delay = 30.0 if rng.random() < self.hang_rate else rng.uniform(*self.latency)
        cost = quote(self, request) * rng.uniform(0.5, 1.0)
        if request.step == "implement":  # each lap gets a bit better thanks to the feedback
            quality = rng.uniform(0.2, 1.0) * self.skill - request.difficulty
            quality += 0.2 * (request.lap - 1)
        else:
            quality = request.patch_quality + rng.uniform(-0.1, 0.1)
        await asyncio.sleep(delay)
        return Result(cost, quality)


@dataclass(slots=True)
class Issue:
    """A unit of work; ``wants_branch`` lets a demo agent misbehave."""

    id: int
    title: str
    size: int = 1
    wants_branch: str | None = None
    attempts: int = 0
    status: Literal["open", "done", "blocked", "deferred"] = "open"


@dataclass(slots=True)
class Project:
    """A repository the loop works on, with its queue of issues."""

    name: str
    difficulty: float = 0.0
    issues: list[Issue] = field(default_factory=list)


def run_checks(quality: float) -> str | None:
    """Stand-in for deterministic checks (tests, lint, types): None means green."""
    return None if quality >= CHECKS_MIN else "tests failing"


class Orchestrator:
    """Runs issues through the loop with every guardrail switched on."""

    def __init__(
        self,
        limits: Limits,
        seed: int = 0,
        clock: Clock = time.monotonic,
        implementer: Engine | None = None,
        reviewer: Engine | None = None,
    ) -> None:
        self.limits = limits
        self.implementer = implementer or FakeEngine("builder", seed, 0.10, hang_rate=0.12)
        self.reviewer = reviewer or FakeEngine("critic", seed, 0.06, skill=0.9)
        if self.implementer.name == self.reviewer.name:
            raise ValueError("the reviewer must be a different engine than the implementer")
        self.budget = Budget(limits.budget_24h, limits.per_call_cap, clock)
        self.forge = Forge(BranchGuard(limits.branch_prefix), limits.dry_run)
        self.breakers: dict[str, CircuitBreaker] = {}
        self._clock = clock
        self._slots = asyncio.Semaphore(limits.max_parallel)
        self.active = 0
        self.peak = 0

    async def call(self, engine: Engine, request: Request) -> Result:
        """Make one engine call under the budget and timeout guardrails."""
        scope = f"{request.project}#{request.issue}"
        price = quote(engine, request)
        try:
            self.budget.authorize(price)
        except BudgetExceeded as exc:
            say("BUDGET", scope, f"{request.step} call refused: {exc}")
            raise
        actual = price  # worst case, unless the call proves otherwise
        try:
            result = await asyncio.wait_for(engine.run(request), self.limits.call_timeout)
            actual = result.cost
            return result
        except TimeoutError:
            say(
                "TIMEOUT", scope, f"{request.step} took over {self.limits.call_timeout}s, cancelled"
            )
            raise CallTimeout(request.step) from None
        finally:
            self.budget.settle(price, actual)

    async def lap(self, project: Project, issue: Issue, number: int) -> str | None:
        """Implement, check, review once; return the feedback, or None when approved."""
        scope = f"{project.name}#{issue.id}"
        ask = Request("implement", project.name, issue.id, number, issue.size, project.difficulty)
        try:
            patch = await self.call(self.implementer, ask)
            problem = run_checks(patch.quality)
            if problem:
                say("CHECKS", scope, f"red ({problem}), feedback goes back to implement")
                return f"checks: {problem}"
            say("CHECKS", scope, "green, patch goes to the reviewer")
            verdict = await self.call(
                self.reviewer, replace(ask, step="review", patch_quality=patch.quality)
            )
        except CallTimeout:
            return "the engine call timed out"
        if verdict.quality < REVIEW_MIN:
            say("REVIEW", scope, f"{self.reviewer.name} requests changes")
            return "review: changes requested"
        say("REVIEW", scope, f"{self.reviewer.name} approves")
        return None

    async def attempt(self, project: Project, issue: Issue) -> Outcome:
        """Run the capped inner loop once, then publish if it was approved."""
        scope = f"{project.name}#{issue.id}"
        try:
            for number in range(1, self.limits.max_laps + 1):
                say("LAP", scope, f"lap {number}/{self.limits.max_laps}")
                if await self.lap(project, issue, number) is None:
                    return self.publish(project, issue)
        except BudgetExceeded:
            return "refused"
        say("LAPS", scope, f"lap cap reached ({self.limits.max_laps}), run failed")
        return "failed"

    def publish(self, project: Project, issue: Issue) -> Outcome:
        """Push the branch and open a PR; the branch guard has the last word."""
        scope = f"{project.name}#{issue.id}"
        branch = issue.wants_branch or f"{self.limits.branch_prefix}{project.name}/{issue.id}"
        try:
            self.forge.push(branch)
            self.forge.open_pr(branch)
        except BranchRefused as exc:
            say("BRANCH", scope, f"push refused: {exc}")
            return "blocked"
        say("PR", scope, f"opened from {branch}, waiting for a human to merge")
        return "pr"

    def breaker(self, project: Project) -> CircuitBreaker:
        """Return the project's circuit breaker, creating it on first use."""
        limits = self.limits
        new = CircuitBreaker(limits.breaker_threshold, limits.breaker_cooldown, self._clock)
        return self.breakers.setdefault(project.name, new)

    def block(self, project: Project, issue: Issue, reason: str) -> None:
        """Stop working on an issue and hand it to a human."""
        issue.status = "blocked"
        self.forge.mark_blocked(f"{project.name}#{issue.id}")
        say("BLOCKED", f"{project.name}#{issue.id}", f"{reason}: handed to a human")

    def record(self, project: Project, issue: Issue, outcome: Outcome) -> None:
        """Apply a run outcome to the issue and to the project's breaker."""
        if outcome == "pr":
            issue.status = "done"
            self.breaker(project).record(True)
        elif outcome == "refused":
            issue.status = "deferred"  # budget exhausted: not the issue's fault, try later
        elif outcome == "blocked":
            self.block(project, issue, "unsafe branch")
        else:
            issue.attempts += 1
            if self.breaker(project).record(False):
                say(
                    "BREAKER",
                    project.name,
                    f"open after {self.limits.breaker_threshold} failed runs",
                )
            if issue.attempts >= self.limits.max_attempts:
                self.block(project, issue, f"{issue.attempts} failed attempts")

    async def wait_for_breaker(self, project: Project) -> None:
        """Sleep while the project's breaker is open."""
        left = self.breaker(project).remaining()
        if left > 0:
            say("BREAKER", project.name, f"paused, no new run for {left:.1f}s")
        while left > 0:
            await asyncio.sleep(left)
            left = self.breaker(project).remaining()
            if left <= 0:
                say("BREAKER", project.name, "cooldown over, next run is a probe")

    async def work_issue(self, project: Project, issue: Issue) -> None:
        """Retry an issue until it is done, blocked or deferred."""
        scope = f"{project.name}#{issue.id}"
        while issue.status == "open":
            await self.wait_for_breaker(project)
            if self._slots.locked():
                say(
                    "PARALLEL", scope, f"{self.limits.max_parallel} runs active, waiting for a slot"
                )
            async with self._slots:
                self.active += 1
                self.peak = max(self.peak, self.active)
                say("RUN", scope, f"started ({self.active}/{self.limits.max_parallel} slots busy)")
                try:
                    outcome = await self.attempt(project, issue)
                finally:
                    self.active -= 1
            self.record(project, issue, outcome)

    async def run_all(self, projects: Sequence[Project]) -> None:
        """Work on every project concurrently (a project's issues go one by one)."""

        async def work(project: Project) -> None:
            for issue in project.issues:
                await self.work_issue(project, issue)

        await asyncio.gather(*(work(project) for project in projects))


def demo_projects() -> list[Project]:
    """Build the projects of the narrated demo."""
    return [
        Project("web-shop", 0.0, [Issue(1, "Fix cart rounding"), Issue(2, "Add coupon field")]),
        Project("mobile-app", 0.1, [Issue(1, "Crash on rotate"), Issue(2, "Rewrite sync", size=4)]),
        Project("legacy-api", 0.9, [Issue(1, "Patch endpoint"), Issue(2, "Patch auth")]),
        Project("docs-site", 0.0, [Issue(1, "Update install page", wants_branch="main")]),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the narrated demo and return the exit code."""
    defaults = Limits()
    parser = argparse.ArgumentParser(description="Narrated demo of autonomous-loop guardrails.")
    parser.add_argument("--seed", type=int, default=7, help="seed of the simulated engines")
    parser.add_argument(
        "--budget", type=float, default=defaults.budget_24h, help="24 h spend budget"
    )
    parser.add_argument(
        "--parallel", type=int, default=defaults.max_parallel, help="max parallel runs"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="log external writes, do not do them"
    )
    args = parser.parse_args(argv)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(relativeCreated)6.0fms  %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    limits = replace(
        defaults, budget_24h=args.budget, max_parallel=args.parallel, dry_run=args.dry_run
    )
    orb, projects = Orchestrator(limits, seed=args.seed), demo_projects()
    say(
        "START",
        "demo",
        f"seed={args.seed} budget={args.budget} parallel={args.parallel} dry_run={args.dry_run}",
    )
    asyncio.run(orb.run_all(projects))

    print("\nSummary")
    for project in projects:
        print(f"  {project.name:<11}", ", ".join(f"#{i.id} {i.status}" for i in project.issues))
    print(
        f"  spent {orb.budget.spent():.2f} of {limits.budget_24h:.2f}, peak parallel runs {orb.peak}"
    )
    print(f"  pushed {orb.forge.pushed}, blocked {orb.forge.blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
