# Verovo

<img src="assets/verovo-logo-card.png" alt="Verovo" width="300">

**让 AI 服务的每一笔钱，都花得明白**

Verovo 把供应商规则、账单、用量和交付记录放进同一次买方核验。业务、工程、采购和财务团队可以看清买了什么、交付了什么，以及下一步该验收、补救还是续约。

[官网](https://roy-tong.github.io/Verovo/) · [English](README.md) · [核验指南](docs/buyer-review.md) · [账期重放](https://roy-tong.github.io/Verovo/examples/replayable-period.html)

[![CI](https://github.com/roy-tong/verovo/actions/workflows/conformance.yml/badge.svg)](https://github.com/roy-tong/verovo/actions/workflows/conformance.yml)
[![PyPI](https://img.shields.io/pypi/v/verovo)](https://pypi.org/project/verovo/)
[![License](https://img.shields.io/badge/License-MIT-blue)](LICENSE)

## 一张总账单，回答不了要不要续约

财务看总额，业务看任务，工程看调用和重试。Verovo 把三种视角连起来，帮助团队回答三个业务问题：

- **费用对不对？** 按供应商适用规则复算支持的账单，并与票据记录比较。
- **交付到位了吗？** 用买方自己的验收判据检查实际交付。
- **这笔钱花得值吗？** 把任务成本与已验证结果连接起来，看每次成功交付的成本和 ROI。

## 从一个账期，到清楚的下一步

1. **带上一个账期**：整理一家供应商的规则、账单、用量和交付记录。
2. **看清三类答案**：费用、交付和证据覆盖并排呈现。
3. **讨论具体差异**：业务、财务和工程围绕同一组发现和待办推进。
4. **重放并决定**：复现报告，保留签核修订，用于验收或续约。

[查看买方核验报告](https://roy-tong.github.io/Verovo/examples/buyer-review.html) · [重放一个账期](https://roy-tong.github.io/Verovo/examples/replayable-period.html)

## 运行第一次核验

```bash
pipx install verovo
verovo review --sample --html buyer-review.html --json buyer-review.json
```

合成示例把费用核验、交付核验和各自依据放进一份可检查的报告。打开 HTML 就能追溯判据与观察记录，再按[核验指南](docs/buyer-review.md)准备自己的任务。

## 看清每个任务的经济性

```bash
git clone https://github.com/roy-tong/Verovo.git && cd Verovo
python3 healthcheck/verovo economics \
  --case examples/task-economics.json \
  --json economics.json --markdown economics.md
```

Verovo 连接任务、Agent 运行、用量、票据与已验证结果。你可以比较直接与分摊成本、每次成功交付成本，以及不同供应商或工作流选择的经济性。

已有账期材料包：

```bash
python3 healthcheck/verovo intake --bundle examples/period-bundle --out ./period-out
python3 healthcheck/verovo drivers --case ./period-out/case.json
```

[任务经济性](docs/task-economics.md) · [账期摄入](docs/period-intake.md) · [成本驱动](docs/cost-optimization.md)

## 从公开测量工作中长出来

Verovo 始于一次约 110 个 AI 用量与成本工具的审计。项目公开了 65 条以上可复核发现，覆盖五类反复出现的计费问题，并持续记录复现与上游修正。

- [The Token-Accounting Bug Report](campaigns/audit-report-2026-09.md)
- [带日期的案例卡](docs/launch/README.md)
- [公开证据与复现](https://roy-tong.github.io/Verovo/claims.html)

这项工作延续在开源规则、SDK、符合性向量和买方工具中。可以使用 CLI、通过 MCP 接入，或带一个具体供应商账期到[买方任务页](https://roy-tong.github.io/Verovo/first-look.html#contact)。

## 继续探索

- [买方核验指南](docs/buyer-review.md)
- [支持的账单复算](examples/showcase/README.md)
- [Buyer Ledger](buyer-ledger/README.md)
- [Provider SDK](sdk/README.md)
- [路线图](ROADMAP.md) · [贡献指南](CONTRIBUTING.md) · [AGENTS.md](AGENTS.md)

Verovo 以 MIT 开源。从一家供应商的一个账期开始，为下一次验收或续约建立清楚的依据。
