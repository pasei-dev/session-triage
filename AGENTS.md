# session-triage — agent notes

Only what cannot be derived from the code.

## Versioning

`plugin.yaml` and the newest `CHANGELOG.md` heading carry the same version. A docs-only change bumps nothing:
it waits under `[Unreleased]`. How a version moves — the digit that counts, and what is counted against — is
`pasei-delegation:pasei-orchestration`; this file names the files.

## Python

**3.10 or newer, and it must also run on the newest release.** The agent half is imported by whatever
interpreter runs Hermes, so 3.10 is the floor; the Commands below run the suite on **3.10 and 3.14**, the pair
that keeps both ends honest. Do not declare a version in `plugin.yaml` — the manifest has no field for it and
an unknown key is a warning, not a gate.

## Commands

```sh
PYTHONPATH="$PWD" python3.10 -m unittest discover -s tests -t . -p 'test_*.py'
PYTHONPATH="$PWD" python3.14 -m unittest discover -s tests -t . -p 'test_*.py'
hermes plugins validate .
```

`tests/_fixture.py` builds a throwaway `state.db` with only the tables the stages read, so the whole
suite runs with no live gateway and no profile. The `triage/` package is plain Python and testable on its
own; the agent half of a running session is not re-read.

## Host surfaces it relies on

`ctx.inject_message` and its `allow_gateway_injection` gate (`hermes_cli/plugins.py`), the `on_session_end` /
`agent_loop_stopped` hook payloads (`hermes_cli/hooks.py`), and the `session_turn_leases` /
`session_admissions` tables (`state.db`). A capability the host lacks is reported, never added there — that
rule is `hermes-plugin-authoring`, and it is why these symbols are cited where they are used.

## Boundaries

- **No deployment's facts, and no other project's names**, in a file, an example or a commit message — the class rule is `pasei-delegation:pasei-orchestration`.
- **The engine never writes session state.** It reads `state.db` `mode=ro` and writes only its own
  ledger; a resume is an injected turn, never a mutation of the transcript.
- **Fail safe.** Every gate defaults to *do not resume*; a failure anywhere is a logged defer, never a
  raise into the host.
