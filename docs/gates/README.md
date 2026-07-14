# Phase gates

Protocol: IMPLEMENTATION_PLAN.md §5. Gates are executed by Fable (highest tier), never by
the agents who wrote the phase's code. No phase begins until the previous gate is PASS.

One report per phase: `PHASE_N_GATE.md` containing:

1. Full-suite test results (all phases so far).
2. DD §19 acceptance criteria walked one by one, with execution evidence
   (Gate 1: fixture order → mirror ≤ 90 s; Gate 3: 3-station walk + fake-JB2 write diff; …).
3. Defects found.
4. Explicit **PASS / FAIL**. On FAIL: remediation tasks `PN-Rx` appended to
   IMPLEMENTATION_PLAN.md with tier assignments; gate re-runs.
