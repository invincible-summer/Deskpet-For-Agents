# Changelog

DeskPet uses Semantic Versioning 2.0.0. See `VERSIONING.md` for the public
compatibility contract and release rules.

## v4.0.0 — Portable release contract

First formal GitHub binary release under the permanent versioning policy.

- Windows x64 portable standalone distribution with `DeskPet.exe`.
- No Python/pip/venv required for the portable package.
- Mutable data separated from program files under `%LOCALAPPDATA%\DeskPet`.
- Runtime config, imported skins, cache and runtime icon follow the LocalAppData
  boundary.
- Compiled-artifact acceptance verifies executable version dispatch, the internal
  converter entry path, bundled FFmpeg presence/configuration, and release layout.
- Formal GitHub Actions release pipeline builds the portable ZIP and SHA256 manifest.
- Current passive terminal/desktop Agent observation, presentation modes, dashboard,
  skin and reliability work from the v3 development line is included.

## Canonical historical majors

### v3.0.0 — Passive-only observation

Commit: `da20a689654093e247abf1c996011b9351d36604`

DeskPet removed the previous control/approval plane and established passive-only
observation: no input injection and no automatic approval.

### v2.0.0 — Managed approval channel

Commit: `e032640fb78fa043214214b292e76b81405e277f`

Approval handling moved away from generic key injection to the managed Codex
app-server JSON-RPC path. This was a breaking change from the v1 behavior contract.

### v1.0.0 — Initial public behavior

Commit: `c0ca087b74c2545dbbccbff5a58a380638becfd1`

Initial usable DeskPet milestone with five-state animation, status bubbles and the
original multi-Agent/approval behavior.

## 2026-09-12 one-time tag normalization

Before the first formal GitHub binary Release, older development-milestone tags were
retired so future version numbers describe compatibility rather than implementation
phases. No commits were removed or rewritten.

The retired tag-to-commit mapping is preserved here for auditability:

| Retired tag | Historical commit |
| --- | --- |
| `v3.1.0` | `dfc32b7169087318c88e7c8fdb9a538849d46ae4` |
| `v3.1.2` | `7840568e15f024f78b885a92d7cc839b59121556` |
| old `v4.0.0` | `2330c26d86d38d6e8dc82ba511ff73a63d6d2d99` |
| `v4.1.0` | `ef3eea404524e46dff6a76c84bdafe1ccf179eb1` |
| `v4.1.1` | `0572da27819698bb8f263d553044916e3db2db3b` |
| `v4.2.0` | `ace08379761d8c1e63cc025f1e1093624e535444` |
| `v4.2.1` | `939c98e3cc056ed73b036e274a478b5c12c09125` |
| `v4.2.2` | `3d0ab8e515cd79fa703ee62138789b00f3c8f474` |
| `v4.2.3` | `57eebece9d77ba58a55c3045e39789976ad2764b` |
| `v4.3.0` | `39941c2815581eadf948ab8e4e90d82eb1c52207` |
| `v4.3.1` | `9635e1bac225378fd120e32f0a6f89512da46069` |
| `4.4.0` | `1cdd18bdaf2d21c2e4011d7c012f02cf97844753` |
| `4.5.0` | `67a5d3320c413773ae373c76efe84b065dc4e6a4` |

Those commit SHAs remain valid historical references. The tag names are intentionally
not reused except for the new canonical `v4.0.0`, which begins the permanent release
policy described in `VERSIONING.md`.
