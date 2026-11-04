# Telemetry tests

Run them offline from this directory's project root:

```sh
uv run --frozen pytest -q tests/telemetry
```

The real-Alloy tests (`test_alloy.py`, part of `test_runner.py`) need an
`alloy` binary on PATH, or `ALLOY_BIN`, and are skipped without one. They plant
a private marker in these places:
- bodies;
- tool arguments and output;
- errors;
- unknown and nested attributes;
- scope metadata;
- allowed keys with malicious values;
- span names, status, events and links;
- metric descriptions.

They check that the marker never reaches an exporter or Alloy's diagnostics.

## The Codex probe

`probe-codex.py` is opt-in and makes two small real Codex calls. Run it as the
runner's authenticated user.

Each call runs with:
- `analytics.enabled` set to true for one call and false for the other;
- a temporary `CODEX_HOME` that links to the existing auth;
- `--ignore-user-config`, `--ignore-rules`, `--strict-config` and `--ephemeral`;
- a read-only sandbox, a temporary working directory, and an HTTP sink on
  loopback.

It never changes user configuration or the scheduler.

```sh
python3 tests/telemetry/probe-codex.py > codex-probe.json            # loopback sink only
ALLOY_BIN=/usr/bin/grafana-alloy python3 tests/telemetry/probe-codex.py --alloy
python3 tests/telemetry/probe-codex.py --endpoint http://127.0.0.1:4318   # through the running collector
```

Its output is a derived inventory. Raw payloads, prompts, tool arguments,
outputs, errors and account identifiers stay in memory. The inventory keeps:
- field names and types;
- recognised event and model identifiers;
- numeric measurements.

It is not an OTLP replay fixture or a production filter. Review its output from
a new CLI version before sharing it.

What it found on `codex-cli` 0.163.0:

| Capability | Observed |
| --- | --- |
| OTLP transport | All three exporters sent HTTP JSON to `/v1/logs`, `/v1/metrics` and `/v1/traces`. |
| Trace parent | A passed W3C trace ID and parent span ID appeared in native spans. |
| Custom resource attributes | They appeared in telemetry. |
| Token metric | `codex.turn.token_usage`, an OTLP DELTA histogram with `token_type`: input, cached_input, cache_write_input, output, reasoning_output and total. Its sums equal the JSONL `turn.completed` usage. |
| Content | With prompt logging off, synthetic content still appeared in `arguments` and `output`, so the collector must remove it. |
| Analytics toggle | Custom OTLP metrics arrived with both true and false. |

Token-bearing SSE events include an extra zero-output observation, so summing
every token-bearing event overcounts. Use the histogram sum or the completed
run's usage.

## Runner smoke test

`smoke-runner.sh` runs the Codex review adapter once against a temporary Git
repository. It uses its own counters and state and calls no scheduler and no
GitHub. First copy the candidate `bin/` to a private directory and create
`bin/telemetry-enabled` there:

```sh
bash tests/telemetry/smoke-runner.sh /path/to/candidate/baton/bin
```

It prints bounded metadata and leaves its state directory in place, for
checking the collector and backends.
