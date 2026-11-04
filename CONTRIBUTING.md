# Contributing

Bug reports, feature requests and pull requests from forks are welcome.

## Issues

For a bug, include the baton revision, OS, Python and Bash versions, the enabled agent CLIs and
their versions, reproduction steps, and expected and observed behavior. For a feature, describe
the use case and the outcome you want. Remove tokens, private repository names, hostnames,
paths and prompt content from configuration and log excerpts.

Maintainers triage reports. Only the configured repository `owner` can authorize the unattended
queue by applying `ready-for-agent`; baton checks the label event's actor. Another maintainer's
label does not authorize a run. The owner must remove and reapply such a label. If the event
history cannot be read, the issue waits. The configured milestone and handoff labels still apply.
Opening an issue does not authorize a run. Agents use the issue body and trusted account feedback;
maintainers relay any needed clarifications from contributors. Vulnerabilities follow
[SECURITY.md](SECURITY.md).

## Pull requests

1. Fork the repository and create a branch in your fork.
2. Keep the change focused. Describe the problem, resulting behavior and relevant validation.
3. Run the checks below. Add regression coverage for behavior changes.
4. Open a PR against `main`. A maintainer reviews fork PRs and approves CI when required.

Fork PRs are supported as community contributions and reviewed manually. The unattended runner
only reviews eligible same-repository PRs from its configured trusted accounts; applying
`agent-review` to a fork PR does not grant it access to the runner host.

## Development checks

The production runner targets Linux with systemd, Bash, jq and GNU coreutils. macOS can run most
offline tests; GNU sed comparisons are skipped when GNU sed is unavailable. `uv` installs the
Python version pinned in `.python-version`.

```sh
uv run --frozen pytest -q
uv run --frozen python -m baton --config config.example.toml --validate
gitleaks git --log-opts=--all --redact
```

The real telemetry integration tests run when `alloy` is on PATH (or `ALLOY_BIN` names it);
otherwise they are skipped. They use ephemeral loopback listeners and need permission to start
Alloy and bind local ports. Other offline checks need no vendor subscription or live credentials.

CI runs the offline suite on Linux. Live provider compatibility and a fresh-host installation
must be checked separately; the offline suite does not call the coding-agent services.

Keep deployment-specific configuration in an ignored `config.toml`, and use
`config.example.toml` for documented placeholders. Contributions use the project's Apache-2.0
license; retain applicable third-party notices.
