# DeskPet Versioning Policy

DeskPet follows [Semantic Versioning 2.0.0](https://semver.org/).

The semantic version itself is `MAJOR.MINOR.PATCH`. Git version tags add the repository
convention prefix and therefore use `vMAJOR.MINOR.PATCH`.

## Public compatibility contract

DeskPet is an application rather than a library, so its public API is the user-facing
compatibility contract below:

1. **Distribution and startup** — the supported Windows x64 portable package,
   `DeskPet.exe`, documented source-start commands, and documented public CLI such as
   `--version`.
2. **User data and configuration** — config schema compatibility and migrations,
   imported skins, and the documented `%LOCALAPPDATA%\DeskPet` data boundary.
3. **Core user behavior** — passive monitoring, supported Agent families, documented
   presentation modes, and documented safety/privacy guarantees.
4. **Release contract** — documented release asset names and upgrade behavior.

Internal Python modules, classes and functions are not a public API. The
`--deskpet-internal-converter` entry point is an implementation detail and is not a
public compatibility promise.

## Version increments

- **MAJOR**: increment for a backward-incompatible change to the public compatibility
  contract, such as removing an established capability, an incompatible public CLI
  change, or a user-data/config change that cannot be migrated compatibly.
- **MINOR**: increment for backward-compatible user-facing functionality, such as a new
  supported Agent, execution topology, presentation capability, setting, or compatible
  distribution feature.
- **PATCH**: increment for backward-compatible correctness, reliability, security or
  performance fixes that do not add a new public capability.

Internal refactors, CI changes, test additions and documentation-only edits do not
receive versions by themselves. `pet/version.py::APP_VERSION` is the single
source-version truth.

The ZCode Desktop -> WSL Remote Development capability is therefore a **MINOR** change:
it adds a new supported user-facing execution topology while preserving the passive
monitoring contract. Its canonical source milestone is `4.1.0`, not `4.0.1`.

Conventional Commit markers may inform the decision (`fix:` often maps to PATCH,
`feat:` often maps to MINOR, `!` / `BREAKING CHANGE:` often maps to MAJOR), but the
public compatibility contract is authoritative.

## Version tags and GitHub Releases

A **version tag** and a **GitHub Release** are two different records:

- Accepted stable source milestones use **annotated Git tags** named exactly `vX.Y.Z`,
  where `X.Y.Z` is stable SemVer without leading zeroes.
- The annotated tag message should identify the product/version, summarize the defining
  user-facing changes, record important safety/reliability characteristics, and state
  whether the tag is source-only or intended for a GitHub Release.
- A GitHub Release is an explicit distribution event created from an already-existing
  immutable version tag. Merely pushing a version tag MUST NOT publish a Release.
- If a GitHub Release is published, its tag version MUST match
  `pet/version.py::APP_VERSION` at that tagged commit.

The repository's `.github/workflows/release.yml` is therefore **manual-dispatch only**.
It accepts an existing annotated `vX.Y.Z` tag, checks stable SemVer and source-version
agreement, runs compile/tests/benchmarks, builds the Portable package, creates a draft
Release, and only then publishes it. Tag pushes do not invoke this workflow.

This separation allows an accepted source version such as `v4.1.0` to exist without a
GitHub Release or new Portable binary until the project intentionally chooses to
publish one.

## Standard application release order

For normal development, use this order:

1. Implement and review the change on a branch; keep the safety/resource contracts.
2. Choose the SemVer from user-visible compatibility impact and update
   `APP_VERSION`, README, CHANGELOG, VERSIONING and affected tests/docs.
3. Run the blocking Windows compile, full unit suite and resource/architecture
   benchmarks; merge the accepted tree to `main`.
4. Create an annotated `vX.Y.Z` tag on that accepted `main` commit with a descriptive
   tag message.
5. If that version is a distribution milestone, explicitly run the release workflow
   for the existing tag. If it is source-only, stop after the tag.
6. Never move a published Release tag. Fixes after a published version receive a new
   SemVer.

This gives the repository an auditable sequence: **code -> acceptance -> main ->
annotated tag -> optional explicit GitHub Release**.

## Release cadence

Source/tag cadence and binary-Release cadence are intentionally separate. DeskPet does
**not** publish a Portable Release for every source patch or tag.

A formal GitHub Release is normally cut when a meaningful milestone has accumulated
important user-facing functionality and/or substantial fixes that are worth
redistributing as a new Portable build. An urgent security, data-safety, or severe
reliability fix may justify an earlier patch Release.

The canonical `v4.1.0` tag records the accepted ZCode WSL Remote Development source
milestone, but intentionally has no GitHub Release. The latest Portable Release remains
`v4.0.0` until another distribution milestone is explicitly published.

## Immutability

Once a version has been published as a GitHub Release, its source contents, tag target
and release assets are immutable project history. Do not move, delete and reuse, or
replace a published Release tag. Fixes are always published under a new version.

Release creation follows a draft-first flow: start from the already-existing annotated
version tag, validate and build, create a draft with its assets, then publish only after
the workflow succeeds. Repository release immutability should be enabled when
available so GitHub can enforce this rule.

## Historical releases

The canonical historical major tags are:

- `v1.0.0` -> `c0ca087b74c2545dbbccbff5a58a380638becfd1`
- `v2.0.0` -> `e032640fb78fa043214214b292e76b81405e277f`
- `v3.0.0` -> `da20a689654093e247abf1c996011b9351d36604`
- `v4.0.0` -> the first canonical Portable binary release

`v1.0.0` through `v3.0.0` predate the current Portable release contract. Their GitHub
Release pages are historical **source releases** created from the existing immutable
annotated tags, with detailed notes reconstructed from the tagged README/code and
version-to-version diffs. No later Portable/EXE assets are retroactively attached to
those releases. They are explicitly kept from replacing `v4.0.0` as the repository's
latest downloadable release.

## 2026-09 historical tag normalization

On 2026-09-12 the repository performed a one-time cleanup before its first formal
Portable GitHub Release. The canonical major tags `v1.0.0`, `v2.0.0` and `v3.0.0` were
retained. Several development-milestone tags after v3 were retired; their commits remain
permanently mapped in `CHANGELOG.md`.

One retired **development-only** tag had the spelling `v4.1.0` and pointed to an older
pre-normalization commit. It was never a published GitHub Release. On 2026-09-13 the
project corrected the current ZCode WSL feature to MINOR SemVer and intentionally
reintroduced `v4.1.0` as the canonical permanent source tag for the accepted current
milestone. `CHANGELOG.md` keeps the old pre-normalization mapping explicitly labeled so
the two historical meanings cannot be confused. This is a one-time normalization
exception; published Release tags are never reused.

The first binary release under the permanent distribution policy remains `v4.0.0`,
which establishes the Portable Windows distribution, `%LOCALAPPDATA%\DeskPet` mutable
data boundary, and validated build/release contract.
