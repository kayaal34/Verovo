# Verovo

<img src="assets/verovo-logo-card.png" alt="Verovo" width="300">

**Make every dollar spent on AI services make sense**

Verovo brings supplier rules, invoices, usage and delivery records into one buyer review. Business, engineering, procurement and finance teams can see what they bought, what was delivered and whether it is time to accept, remediate or renew.

[Website](https://roy-tong.github.io/Verovo/) · [中文](README.zh-CN.md) · [Buyer guide](docs/buyer-review.md) · [Replay example](https://roy-tong.github.io/Verovo/examples/replayable-period.html)

[![CI](https://github.com/roy-tong/verovo/actions/workflows/conformance.yml/badge.svg)](https://github.com/roy-tong/verovo/actions/workflows/conformance.yml)
[![PyPI](https://img.shields.io/pypi/v/verovo)](https://pypi.org/project/verovo/)
[![License](https://img.shields.io/badge/License-MIT-blue)](LICENSE)

## A total invoice cannot make the renewal decision for you

Finance sees the total. Operations sees the tasks. Engineering sees the calls and retries. Verovo connects those views so the team can answer three business questions:

- **Are the charges right?** Recalculate supported bills with the supplier’s applicable rules and compare the results with invoice records.
- **Was the work delivered?** Check observed delivery against the buyer’s own acceptance criteria.
- **Was it worth paying for?** Connect task costs with verified outcomes to understand cost per verified success and ROI.

## From one billing period to a clear next move

1. **Bring the period.** Gather the rules, invoice, usage and delivery records for one supplier.
2. **Review the three answers.** Charges, delivery and evidence coverage appear side by side.
3. **Discuss the differences.** Give operations, finance and engineering a shared set of findings and open questions.
4. **Replay and decide.** Reproduce the report, preserve the signed revision and use it for acceptance or renewal.

[Open a buyer review](https://roy-tong.github.io/Verovo/examples/buyer-review.html) · [Replay a billing period](https://roy-tong.github.io/Verovo/examples/replayable-period.html)

## Run your first review

```bash
pipx install verovo
verovo review --sample --html buyer-review.html --json buyer-review.json
```

The synthetic example presents a charge review, a delivery review and the evidence behind each conclusion in one inspectable report. You can open the HTML, trace the criteria and observations, and then prepare your own task with the [buyer guide](docs/buyer-review.md).

## See the economics behind each task

```bash
git clone https://github.com/roy-tong/Verovo.git && cd Verovo
python3 healthcheck/verovo economics \
  --case examples/task-economics.json \
  --json economics.json --markdown economics.md
```

Verovo connects tasks, agent runs, usage, invoices and verified outcomes. Use it to compare direct and allocated costs, cost per verified success and the economics of different supplier or workflow choices.

Already have a billing-period bundle?

```bash
python3 healthcheck/verovo intake --bundle examples/period-bundle --out ./period-out
python3 healthcheck/verovo drivers --case ./period-out/case.json
```

[Task economics](docs/task-economics.md) · [Period intake](docs/period-intake.md) · [Cost drivers](docs/cost-optimization.md)

## Built from public measurement work

Verovo grew out of an audit of roughly 110 AI usage and cost tools. The project published more than 65 evidence-backed findings across five recurring accounting problems, along with reproductions and upstream corrections.

- [The Token-Accounting Bug Report](campaigns/audit-report-2026-09.md)
- [Dated case cards](docs/launch/README.md)
- [Claims and reproductions](https://roy-tong.github.io/Verovo/claims.html)

That work continues in the open-source rules, SDK, conformance vectors and buyer tools. Use the CLI, connect through MCP, or bring a concrete supplier period to the [buyer task page](https://roy-tong.github.io/Verovo/first-look.html#contact).

## Continue

- [Buyer review guide](docs/buyer-review.md)
- [Supported recounts](examples/showcase/README.md)
- [Buyer Ledger](buyer-ledger/README.md)
- [Provider SDK](sdk/README.md)
- [Roadmap](ROADMAP.md) · [Contributing](CONTRIBUTING.md) · [AGENTS.md](AGENTS.md)

Verovo is MIT open source. Start with one supplier and one period, and give your next acceptance or renewal a clear basis.
