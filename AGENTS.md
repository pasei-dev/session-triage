# session-triage — agent notes

Only what cannot be derived from the code.

## Versioning

`plugin.yaml` and the newest `CHANGELOG.md` heading carry the same version. Bump the **last digit
only** (`0.1.0` → `0.1.1`) for every change, feature or fix; the minor never moves on its own. Count
the bump against the **base branch**, not against your own last commit. Write the entry under
`### Added` / `### Changed` / `### Fixed` in a new heading, leaving `## [Unreleased]` above it.

## Python

**3.10 or newer, and it must also run on the newest release.** The agent half is imported by whatever
interpreter runs Hermes, so 3.10 is the floor and the suite is run on **3.10 and 3.14** — the pair
that keeps both ends honest. Do not declare a version in `plugin.yaml`; the manifest has no field for
it and an unknown key is a warning, not a gate.

## Commands

```sh
PYTHONPATH="$PWD" python3.10 -m unittest discover -s tests -t . -p 'test_*.py'
PYTHONPATH="$PWD" python3.14 -m unittest discover -s tests -t . -p 'test_*.py'
hermes plugins validate .
```

`tests/_fixture.py` builds a throwaway `state.db` with only the tables the stages read, so the whole
suite runs with no live gateway and no profile.

## The one hard constraint

This plugin runs on **stock Hermes**, and is never a reason to patch it. What the host provides is
read from its source and cited where it is relied on — `ctx.inject_message` and its
`allow_gateway_injection` gate (`hermes_cli/plugins.py`), the `on_session_end` / `agent_loop_stopped`
hook payloads (`hermes_cli/hooks.py`), and the `session_turn_leases` / `session_admissions` tables
(`state.db`). A capability the host lacks is reported, never added there.

## What is live, and when

The agent half of a **running** session is not re-read: `__init__.py`, the hooks, the tool and the
worker only change for the next session. The `triage/` package is plain Python and testable on its
own.

## Boundaries

- **No deployment's facts, ever.** No account or app identity, no host path, no profile symlink, no
  naming of another plugin, no machine-only tool. A value that exists on one machine is what makes the
  package uninstallable everywhere else.
- **The engine never writes session state.** It reads `state.db` `mode=ro` and writes only its own
  ledger; a resume is an injected turn, never a mutation of the transcript.
- **Fail safe.** Every gate defaults to *do not resume*; a failure anywhere is a logged defer, never a
  raise into the host.
