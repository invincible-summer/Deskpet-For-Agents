# AGENTS.md

## 1. Scope and project mission

This file is the repository-wide development contract for DeskPet. It applies to all future maintenance, fixes, features, refactors, tests, documentation, and release work. A nested instruction file may add stricter local rules, but must not weaken this contract.

DeskPet is a lightweight Windows desktop-pet application that passively observes AI coding agents running in Windows and WSL terminals, derives their runtime state, presents that state through desktop pets / bubbles / dashboard UI, and can safely wake the associated terminal window when the user explicitly asks.

The long-term priorities are, in order:

1. **Correctness and deterministic behavior**
2. **Low CPU / low memory / smooth UI**
3. **Safety, privacy, and conservative attribution**
4. **Maintainable architecture with minimal unnecessary complexity**
5. **Good desktop UX and public-release reliability**

Do not trade these priorities for broader feature scope.

---

## 2. Plan-driven development is mandatory

Implementation plans are authoritative development specifications, not optional design notes.

Before changing code, always:

- read this `AGENTS.md`;
- identify and read every currently active/approved plan relevant to the task;
- read the relevant current implementation and tests;
- identify the acceptance criteria, interface contracts, resource limits, safety rules, and explicit non-goals defined by those plans.

When multiple plans apply:

- later plans supersede earlier plans **only where they explicitly change a previous contract**;
- all non-conflicting requirements remain cumulative;
- do not silently discard an older acceptance criterion because a newer plan exists;
- if code, tests, documentation, and plans disagree, determine the intended contract from the approved plans before implementation.

During implementation, re-read the relevant plan sections:

- before changing an architectural interface;
- after each major implementation batch;
- whenever an implementation choice differs from the planned mechanism;
- before declaring the task complete.

Do not “implement the spirit” while violating specified semantics. Do not rewrite or weaken a plan merely to make the current implementation pass. If a plan is genuinely impossible, unsafe, contradictory, or obsolete, surface the conflict and propose an explicit plan amendment.

---

## 3. Architectural invariants

Preserve the layered architecture unless an approved plan explicitly replaces it.

### Observation and state

Keep these responsibilities separate:

- **Process discovery / liveness** answers *which Agent instances exist* and their process identity.
- **Session observers** incrementally read Agent-owned durable state and answer *what the Agent is doing*.
- **Terminal observation** passively observes terminal UI evidence and must remain independently attributable.
- **State reduction** combines evidence into a conservative user-facing state.
- **Presentation logic** decides which Agent(s) should be displayed; it must not redefine monitoring truth.
- **Desktop UI** renders presentation state and performs explicit user actions only through validated service interfaces.

Process existence, session activity, terminal attachment, terminal observation, and window wakeability are different facts. Never collapse them into one heuristic.

### Concurrency

The number of Agents or pets must not cause linear growth in permanent infrastructure.

Prefer:

- one shared monitor;
- shared process probing;
- one terminal-observation backend where applicable;
- shared animation cache and scheduler;
- bounded queues and caches;
- event/revision-driven updates instead of repeated full polling/redraw.

Do **not** introduce per-Agent or per-pet permanent threads, processes, UIA clients, polling loops, caches, or timers unless an approved plan explicitly requires them and defines a resource budget.

### UI thread

Tk/Tcl objects belong to the Tk thread.

The Tk thread must only perform short GUI work. Never perform slow filesystem traversal, subprocess waits, WSL commands, heavy conversion, network work, or other unbounded/blocking operations in a UI callback.

Prefer:

- background workers for non-Tk blocking work;
- small, bounded UI slices for work that must stay on Tk;
- dirty/revision-based rendering;
- coalesced callbacks;
- no redraw/rebuild when semantic state is unchanged.

Hidden or inactive UI should generate as little periodic work as practical.

---

## 4. Lightweight-performance rules

Low resource usage is a product requirement, not an afterthought.

For every change:

- reuse existing observations before adding new scans;
- do not increase polling frequency without measured evidence and an approved reason;
- keep I/O incremental and bounded;
- avoid rescanning large trees when a canonical root/index is available;
- keep queues, histories, text buffers, decoded media, and caches explicitly bounded;
- avoid duplicate copies of decoded assets;
- avoid repeated widget destruction/recreation when incremental update is sufficient;
- avoid importing heavy conversion/scientific dependencies into the long-lived UI process when they can remain in short-lived workers;
- preserve idle efficiency: “nothing changed” should result in almost no work.

Resource ceilings defined by active plans, benchmarks, and tests are **hard compatibility requirements**. Do not raise a ceiling simply to make a regression disappear.

Any performance optimization must preserve correctness, safety, and observability.

---

## 5. Safety, privacy, and attribution

DeskPet should remain passive and conservative by default.

Unless an approved future plan explicitly introduces a controlled capability with its own safety design:

- do not configure Agent hooks;
- do not inject into Agent processes;
- do not read or modify Agent process memory;
- do not simulate keyboard input;
- do not use clipboard injection as a control path;
- do not automatically approve Agent requests;
- do not kill, terminate, or “clean up” user Agent processes merely because DeskPet considers them detached.

Use strong runtime identities and revalidate before user-triggered actions. Runtime identifiers that can be reused or become stale must not be treated as permanent identity.

For evidence attribution:

- ambiguous evidence stays ambiguous;
- unreadable state is not evidence of absence;
- silence is not approval;
- low-confidence window/location evidence must not automatically gain high-confidence terminal-text attribution;
- prefer UNKNOWN / unbound / no-action over acting on the wrong Agent.

Terminal/session content must follow the active privacy contract. Do not persist sensitive terminal text, runtime process/window identifiers, or other ephemeral identities unless an approved plan explicitly requires it.

---

## 6. Change discipline

Prefer the smallest change that fully satisfies the approved contract.

Do not refactor stable code merely because another architecture is aesthetically preferable. Preserve already-correct behavior and tested interfaces unless the plan requires a change.

When modifying an interface:

- define ownership, lifetime, thread affinity, error behavior, and invalidation semantics;
- update all callers atomically;
- specify what happens during failure, partial availability, stale data, shutdown, and restart;
- avoid hidden side effects;
- keep runtime-only state separate from persistent configuration.

For upstream Agent/Windows behavior:

- verify current official documentation or upstream source when protocol/layout/API behavior matters;
- treat official API documentation as the strongest contract;
- treat upstream implementation as current behavior that may require feature detection;
- treat issue reports as evidence of real-world behavior, not as stable APIs;
- retain backward compatibility where inexpensive and safe.

Do not hard-code developer-machine paths, private assets, local environment assumptions, or undocumented one-machine behavior into public code.

---

## 7. Configuration and persistence

Persistent configuration is for user preferences and durable semantic selectors, not ephemeral runtime identity.

Configuration changes must:

- have explicit defaults and normalization;
- be migration-safe;
- preserve unknown forward-compatible data where appropriate;
- use reliable/atomic persistence according to the current project contract;
- report save failure instead of silently pretending success;
- avoid unnecessary writes, especially during high-frequency UI interactions.

Runtime facts such as process incarnation, exact PID, HWND, UIA RuntimeId, transient terminal bindings, current focus, and temporary liveness leases must remain runtime-only unless an approved plan explicitly changes that rule.

---

## 8. Testing and acceptance are part of implementation

A change is not complete when the code “looks correct”.

Every planned acceptance criterion must be mapped to evidence:

- automated unit/regression test where feasible;
- benchmark/resource assertion for performance contracts;
- integration test for cross-layer behavior;
- explicit real-machine/manual verification only where platform behavior cannot be faithfully automated.

For each task:

1. run focused tests while implementing;
2. run the full applicable test suite before completion;
3. run all applicable performance/resource benchmarks;
4. exercise failure and shutdown paths, not only the happy path;
5. verify ambiguity, stale-data, process-exit, restart, and configuration-migration behavior when relevant;
6. confirm no acceptance criterion was skipped.

Never weaken a test, budget, timeout, confidence threshold, or acceptance condition merely to obtain a pass. If an existing test is wrong, justify the contract change from the active plan first.

CI success is necessary but not sufficient. Platform-sensitive functionality must still satisfy the plan’s real-world acceptance requirements.

---

## 9. Required plan-review loop

Use this loop for substantial work:

**Before coding**
- Read active plans and relevant source/tests.
- List the contracts and acceptance criteria affected.
- Confirm what must not change.

**While coding**
- Implement in small coherent batches.
- After each architectural batch, compare the result against the relevant plan sections.
- Check thread count, blocking behavior, ownership, failure semantics, and resource bounds.

**Before finishing**
- Re-read the plans from the perspective of the final implementation.
- Trace every acceptance criterion to a passing test, benchmark, or documented manual check.
- Search for obsolete code paths, old semantics, duplicate implementations, stale documentation, and accidental compatibility regressions.
- Run the complete applicable validation set.

**Completion report**
- State what changed.
- State which acceptance criteria were verified and how.
- State any remaining limitation or unverified platform-specific item explicitly.
- Never describe an untested assumption as verified behavior.

---

## 10. Documentation and release consistency

Documentation is part of the product contract.

When behavior, configuration, supported upstream formats, safety semantics, startup/release behavior, or user-visible UI changes:

- update the relevant user/developer documentation;
- keep version and release metadata consistent;
- keep the project’s upstream/reference list current and concise;
- remove stale claims that no longer describe the implementation.

A public release must not depend on the original developer’s machine, private paths, private skins/assets, pre-existing caches, or undocumented setup. Where release validation is in scope, test from a clean checkout/environment according to the active release plan.

---

## 11. Definition of done

Work is done only when all of the following are true:

- the implementation follows every applicable active-plan contract;
- all applicable acceptance criteria have evidence and pass;
- relevant regression tests and benchmarks pass;
- no resource/safety/privacy invariant was weakened;
- no new unbounded background work or lifecycle leak was introduced;
- failure, shutdown, stale-state, and restart behavior are defined and tested where relevant;
- documentation matches actual behavior;
- remaining limitations are explicitly recorded rather than hidden by heuristics.

When uncertain, choose the solution that is simpler, more conservative, more measurable, and cheaper while preserving the approved semantics.

## Attention

If the test runs overtime continuously, **please check the test logic to see if there is a waiting person trigger or an infinite loop vulnerability**.

**Be careful when writing a test that the native menu is blocked interactively.**