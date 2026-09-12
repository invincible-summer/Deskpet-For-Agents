# DeskPet Versioning Policy

DeskPet follows [Semantic Versioning 2.0.0](https://semver.org/).

The semantic version itself is `MAJOR.MINOR.PATCH`. Git tags add a repository
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
  supported Agent, presentation capability, setting, or compatible distribution
  feature.
- **PATCH**: increment for backward-compatible correctness, reliability, security or
  performance fixes that do not add a new public capability.

Internal refactors, CI changes, test additions, documentation fixes and development
plan milestones do not receive versions by themselves. A version changes only when a
release is intentionally prepared.

Conventional Commit markers may inform the decision (`fix:` often maps to PATCH,
`feat:` often maps to MINOR, `!` / `BREAKING CHANGE:` often maps to MAJOR), but the
public compatibility contract is authoritative.

## Release tags

Stable releases use annotated tags named exactly `vX.Y.Z`, where `X.Y.Z` is a valid
stable SemVer with no leading zeroes. `pet/version.py::APP_VERSION` is the single
application-version source of truth and MUST exactly equal the tag without the `v`
prefix.

The current automated release channel publishes stable versions only. SemVer
pre-release identifiers remain reserved for a future release-channel implementation;
until that exists, development candidates stay on branches and do not consume formal
release tags.

## Immutability

Once a version has been published as a GitHub Release, its source contents, tag target
and release assets are immutable project history. Do not move, delete and reuse, or
replace a published release tag. Fixes are always published under a new version.

Release creation follows a draft-first flow: create a draft from the already-existing
version tag, attach all release assets, then publish the draft only after validation
passes. Repository release immutability should be enabled so GitHub enforces this rule.

## Historical normalization

On 2026-09-12 the repository performed a one-time cleanup before its first formal
GitHub binary Release. The canonical historical major milestones are retained as
`v1.0.0`, `v2.0.0`, and `v3.0.0`. Development-milestone tags after `v3.0.0` were
retired; their commits remain permanently in Git history and are mapped in
`CHANGELOG.md`.

The first release under this permanent policy is `v4.0.0`, which establishes the
portable Windows distribution, `%LOCALAPPDATA%\DeskPet` mutable-data boundary, and the
validated build/release contract.
