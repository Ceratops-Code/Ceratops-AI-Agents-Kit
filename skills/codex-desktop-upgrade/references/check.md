# Check Action

## Goal

Determine whether an official Codex desktop release is newer than the version
underlying the currently running managed runtime, summarize the intervening
official release notes, and recommend which patches merit retirement testing.

## Context

The running managed runtime's `.code-version.json` is authoritative for its
base version and effective patch set. Installed package metadata establishes
what is present on the computer; official Store metadata and OpenAI release
notes establish what is published when they expose an exact version.

## Constraints

### Boundaries

- This action is read-only. Do not import app code, run `AssessUpgrade`, build,
  patch, qualify, adopt, deploy, restart Codex, or edit patch decisions.
- Compare numeric versions with version-aware semantics, not lexical ordering.
- Distinguish the running base, locally installed official package, and latest
  published official version. Do not present the installed version as the
  latest published version unless official evidence establishes that identity.
- Use official OpenAI or Microsoft sources for release availability and notes.
  State when an exact Store version or complete version-to-note mapping is not
  exposed.
- Release-note similarity identifies only a retirement candidate. Recommend
  the `upgrade` action for behavior comparison before changing or removing any
  patch.

## Workflow

1. From the active patcher checkout, run
   `pwsh -NoProfile -ExecutionPolicy Bypass -File
   scripts\Test-CodexRuntimeHealth.ps1 -Component Context
   -RequireRunningCodex -FailOnBlocked` exactly once. Read
   `%LOCALAPPDATA%\CodexDesktopPatcher\launcher-config.json`, require the
   reported running path to be beneath its `packageRoot`, resolve exactly one
   App-Code `patched/*-<generation-prefix>-source-v2` tag from its `storeRoot`,
   and read only `<tag>:.code-version.json`. Do not read the full generation
   record or scan the package tree.
2. Run one bounded `Get-AppxPackage -Name 'OpenAI.Codex'` query that emits only
   identity, version, architecture, signature, status, and install location.
   Retrieve one current official Store listing or public package manifest and
   the official OpenAI release notes; record source links and retrieval dates.
   When Store metadata omits the exact version or reports `Unknown`, stop version
   discovery and report that limitation instead of probing alternate endpoints.
3. Compare the newest officially evidenced version with the running base. If
   publication metadata does not expose an exact newer version, report that
   limitation instead of inferring one from dates or prose.
4. Report a separate dated `New official features` list of user-visible Codex
   desktop features newer than the running base. Use an exact version-to-note
   mapping when available. Otherwise use the original App-Code tag commit date
   only as a labeled local-capture lower bound, state that the notes do not prove
   inclusion in a specific Store build, and exclude unrelated ChatGPT-only
   entries.
5. Map each relevant native change to the effective patch set by behavior and
   owner. Classify patches as no apparent overlap, retirement candidate, or
   insufficient evidence. For each candidate, name the behavior that the
   `upgrade` action must verify.
6. Recommend no action when the running base is current. Otherwise recommend
   either a read-only `upgrade` assessment or a user-requested upgrade phase,
   clearly naming the required authorization and target version.

## Done When

### Completion Gate

The running-base comparison is supported by current official evidence, a dated
post-base feature list is reported from official notes, every effective patch
has a retirement disposition, and all unavailable or ambiguous version
boundaries are explicit.

### Output Contract

Report the running base, installed official version, latest officially
evidenced version, whether a newer release exists, a dated linked `New official
features` list, patch-by-patch candidate analysis, and one recommended next
action. Keep version-to-note uncertainty explicit and do not claim that any
patch is retired or compatible.
