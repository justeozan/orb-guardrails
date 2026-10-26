# orb-guardrails

The guardrails that let an autonomous coding loop run unattended without burning money or
touching what it should not, extracted as one small, runnable, offline Python script.

It comes from **Orb**, a private loop that finds work in a repository, implements it in an
isolated worktree, runs the checks, gets a cross-review from another model, opens a pull
request and waits for a human to merge. Orb is part of the [Evey](https://evey.run) suite.
This repository keeps only the safety ideas, applied to simulated engines: no network, no
real agent, no dependency (Python 3.12+ standard library).

## Quick start

```bash
python3 orb_guardrails.py                          # narrated demo, about 3 seconds
python3 orb_guardrails.py --dry-run --parallel 1   # external writes are only logged
python3 orb_guardrails.py --seed 3 --budget 3.0    # other random outcomes, bigger budget
python3 -m unittest -v                             # one or more tests per guardrail
```

Options: `--seed` (simulated engines), `--budget` (24 h spend limit), `--parallel` (max runs
at once), `--dry-run`. Four projects and seven issues run concurrently; every guardrail that
fires prints one tagged line.

## Sample output

An excerpt of the default run (`python3 orb_guardrails.py`), trimmed for readability:

```text
    18ms  START     demo              seed=7 budget=1.6 parallel=2 dry_run=False
    18ms  PARALLEL  legacy-api#1      2 runs active, waiting for a slot
   409ms  REVIEW    mobile-app#1      critic requests changes
   409ms  LAP       mobile-app#1      lap 2/3
   663ms  PR        web-shop#1        opened from orb/web-shop/1, waiting for a human to merge
   909ms  TIMEOUT   mobile-app#1      implement took over 0.5s, cancelled
  1099ms  LAPS      legacy-api#1      lap cap reached (3), run failed
  1305ms  BRANCH    docs-site#1       push refused: 'main' is outside 'orb/*'
  1738ms  BLOCKED   legacy-api#1      2 failed attempts: handed to a human
  1739ms  BUDGET    mobile-app#2      implement call refused: 0.40 > cap 0.30 per call
  2230ms  BREAKER   legacy-api        open after 3 failed runs
  3231ms  BUDGET    legacy-api#2      implement call refused: 1.54 spent + 0.10 > 1.60 / 24h

Summary
  web-shop    #1 done, #2 done
  mobile-app  #1 done, #2 deferred
  legacy-api  #1 blocked, #2 deferred
  docs-site   #1 blocked
```

## The guardrails

| Guardrail | Why it matters |
|---|---|
| Rolling 24 h budget | A bug or a loop of retries cannot spend more than a fixed amount per day. Calls are refused before they start, and parallel calls reserve their worst-case cost. |
| Per-call cap | One oversized request cannot eat the whole budget; too-large work is deferred for a human to split. |
| Per-call timeout | A hung agent is cancelled and billed at its worst case instead of stalling a slot forever. |
| Circuit breaker per project | After N failed runs in a row a project is paused for a cooldown, then probed once: a broken repo stops costing money, the others keep going. |
| Max parallel runs | A semaphore shared by all projects bounds CPU, rate limits and spend rate. |
| Capped inner loop | Implement, deterministic checks, review by a different engine; failures feed back to implement for at most N laps, so the agent never grades its own work and never loops forever. |
| Max attempts per issue | After N failed runs the issue is labelled blocked and handed to a human instead of being retried forever. |
| Branch-scope guard | Only branches under a prefix (here `orb/`) can be pushed, whatever the agent asks for, including `orb/../main` tricks. Humans keep `main`. |
| Dry-run mode | Every external write (push, pull request, label) is only logged, so you can watch a new setup safely. The branch guard still applies. |

## Files

- `orb_guardrails.py`: the guardrails and the demo, one readable file.
- `test_orb_guardrails.py`: unit tests (standard library `unittest`, injectable clock).

## About Orb

Orb itself is **not open source**. This repository is a small, self-contained illustration of
its guardrail ideas, released under the MIT license so you can reuse them. The simulated
engines are toys: swap `FakeEngine` for a real agent call to try the ideas for real.
