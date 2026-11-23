# Provider Manifest and local admission

Verovo uses a small Provider Manifest to keep external components replaceable
without losing evidence boundaries. It is inspired by REA's provider admission,
owned-process lifecycle, resource limits and evidence coverage; it does not add
REA, Node.js or a reverse-engineering engine to Verovo.

## Local command

```bash
python3 healthcheck/verovo provider check \
  --manifest examples/provider-manifest/otel-file.json \
  --json /tmp/verovo-provider-admission.json
```

The command reads one `verovo.provider-manifest/0.1` document and emits
`verovo.provider-admission/0.1`. It does not contact the engine. Every result
contains `live_engine_contacted=false`; `coverage` remains the declared input
coverage rather than becoming a production completeness claim.

## Manifest contract

| Field | Required meaning |
| --- | --- |
| `adapter_id`, `engine` | Adapter identity, engine version and `file/process/http/embedded` integration mode |
| `source` | Source party, control domain, collector and collection method |
| `readiness_checks` | Explicit pass/fail/unknown observations; every check declares whether it is required |
| `limits` | Positive input, output, nesting, CPU and timeout budgets |
| `coverage` | complete/partial/unknown plus matched, unmatched, duplicate, rejected and omitted counts |
| `cleanup` | released, retained or unknown; no success default |
| `raw_snapshot_refs` | SHA-256 references to retained raw evidence |
| `normalization_rule_version` | Version that produced normalized candidates |

Process integrations additionally require the selected executable's SHA-256.
Limits and counts are JSON integers, not booleans or numeric strings. Unsupported
fields and unsupported versions fail closed.

## Admission is not production verification

| Admission | Condition |
| --- | --- |
| `admit` | Required checks pass, optional checks pass or degrade, and cleanup is released |
| `degrade` | No required failure exists, but an optional check fails or is unknown |
| `refuse` | A required check fails or is unknown, or cleanup is retained/unknown |

`admit` means only that the local manifest satisfies these rules. It does not
prove upstream connectivity, complete collection, independent evidence, correct
billing, effective budget enforcement or a stopped charge. Those require their
own source records and replay. `otel-file.json` is a synthetic example; its
snapshot and coverage counts are not customer data.

Schema: [`schemas/provider-manifest.schema.json`](../schemas/provider-manifest.schema.json).
