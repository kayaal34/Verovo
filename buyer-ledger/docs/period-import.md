# 账期包导入与人工字段映射（V-02）

> **状态：V-02 合成 fixture 验收。** 本页描述导入层：扫描本地账期包、提议
> 字段映射、由人确认后生成 Period Intake 可消费的 canonical bundle。
> `calculate` / `report` / `review` 已在 V-03 接入；`revise` / `signoff` 已在
> V-04 接入为独立 revision 与署名 sidecar，见
> [period-revisions.md](period-revisions.md)。

```bash
cd buyer-ledger
B="python3 -m buyer_ledger"

$B close init ./close-2026-10 --from-job ./bundle/period-job.json
$B close ingest ./close-2026-10 --bundle ./bundle
$B close mapping ./close-2026-10
$B close mapping ./close-2026-10 --confirm --review-by finance
$B close status ./close-2026-10
```

也可以不用 `--from-job`，直接给 `--job-id --vendor --account --period 2026-10
--currency USD [--buyer ...] [--timezone Asia/Shanghai] [--rule-map ...]`，由
CLI 生成同一份 `period-job.json`。

## 1. 输入契约：`verovo.period-job/1`

```json
{
  "schema": "verovo.period-job/1",
  "job_id": "close-2026-10-a",
  "buyer": "buyer-1",
  "vendor": "vendor-1",
  "account": "acct-1",
  "period": {
    "from": "2026-10-01",
    "to": "2026-11-01",
    "to_exclusive": true,
    "timezone": "UTC"
  },
  "currency": "USD",
  "sources": [
    {"role": "bills", "path": "bill-export.csv", "control_domain": "vendor"},
    {"role": "usage", "path": "usage-export.jsonl", "control_domain": "vendor"}
  ],
  "rule_map": "contract-rules.json",
  "task_economics_requested": false,
  "approved_by": null
}
```

- 一个 job 只允许一个 vendor / account / currency。跨币种、跨账户不是“合并
  成一个精确总额”，而是拆成多个 job，或输出带缺项说明的部分报告。
- 账期固定半开区间 `[from, to)`，`to_exclusive` 必须为 `true`；时区用 IANA
  名称，与账单展示口径一致，并记录供应商次日/延迟修订属于新版本。
- `sources[].role` 只支持 `invoice`、`bills`、`usage`、`tasks`、`runs`、
  `outcomes`。原始文件始终不被修改。
- `sources[].control_domain` 取 `vendor` / `buyer` /
  `buyer_or_vendor_pending_review`。pending 必须在确认映射时显式解决。
- `sources[].sha256` 可选；填写后导入时校验，不匹配即 `STALE_SOURCE`。
- `approved_by` 只是记录字段，不授权任何动作；人工确认来自 `close mapping
  --confirm --review-by ...`，并记为 `attested_by_declaration`（C-01 §12.3）。

## 2. 命令与产物

| 命令 | 作用 |
| --- | --- |
| `close init CASE` | 创建 `CASE/period-job.json`；已存在时拒绝覆盖（修订属于 V-04） |
| `close ingest CASE --bundle DIR` | 扫描、解析、列识别、区间/币种/账户/重复 ID 校验，写 `intake/scan.json` 与 `intake/mapping-draft.json` |
| `close mapping CASE` | 展示提议的列映射、控制域和 open items；此时还没有 canonical |
| `close mapping CASE --confirm --review-by NAME [--control-domain role=vendor\|buyer] [--note ...]` | 记录人工确认，并生成 `CASE/canonical/` |
| `close status CASE`（`close show` 同） | 只读 BuyerCloseCase 投影：四完成轴、review queue、missing inputs、C-01 状态 |
| `close calculate` / `close report` | V-03：运行既有 canonical 重算链并生成六份月结报告对象 |
| `close review` | V-03：记录署名复核；不关闭未决项、不签核、不授权付款 |
| `close revise` | V-04：追加新的独立 revision；同输入 idempotent，不覆盖旧版本 |
| `close signoff` | V-04：对指定 revision 记录署名 scope/source/rule 指纹；不是付款授权 |

目录结构：

```
CASE/
  period-job.json
  intake/
    scan.json              # 每个源文件的路径、sha256、字节数、行数、格式
    mapping-draft.json     # 列映射提议、validation、coverage_diagnostics、open_items
    mapping-confirmed.json # 人工确认记录（含 draft sha256）
  canonical/
    manifest.json
    bills.csv / usage.jsonl / ...   # 按确认后的映射生成
    rule-map.json                   # 原样复制，供 Period Intake 校验版本
    <invoice files>                 # 仅作为 unreviewed 文档保留
```

`canonical/manifest.json` 的 `sources` 只列账本来源；未审核的 invoice/OCR 文档
放在 `documents` 字段，带 `review_status=unreviewed`、
`booking_status=unbooked`。它们不会被 Period Intake 读成费用行。

## 3. 导入不变量（AC V07–V15）

- **V07 编码与坏行**：只接受 UTF-8（含 BOM）；非 UTF-8、空文件、CSV 列数
  与表头不一致、JSONL 非对象/坏 JSON 都给出 `INPUT_REQUIRED`，错误信息带
  文件名，坏行另外带行号。
- **V08 货币混合**：任何一行币种与本 job 不同即 `UNSUPPORTED_SCOPE`，映射
  草稿标记 blocked，`confirm_mapping` 拒绝生成 canonical。同一 charge 在
  bills 与 usage 中只计一次，不因两份导出重复计数。
- **V09 重复 ID**：重复 `charge_id` / `usage_id` 列入
  `validation.duplicate_charge_ids` / `duplicate_usage_ids`；只有首行可入账，
  后行不双计。
- **V10 控制域**：vendor export 即使由买方下载仍是 vendor-controlled；两份
  vendor 导出可以支持 Vendor Reconciled，但 `Buyer Verified` 始终
  `UNPROVABLE`，导入层也不会判 `Delivery Accepted`。
- **V11 OCR / invoice2data**：最多产出 unreviewed 草稿；V-02 不调用 OCR、
  不解析文本、不把提取文本变成金额。invoice 文档进入 review queue，不进入
  账本行。
- **V12 价格表有效期**：pricing rule 要带 currency、unit、version 和
  `valid_from`/`valid_to`；有效期不覆盖账期即 `UNSUPPORTED_SCOPE`，拒绝
  重新计价。未声明有效期的规则进入 open items，不能当作已批准。
- **V13 未审核映射**：`ingest` 只写提议；没有 `mapping-confirmed.json` 就
  没有 canonical，`build_case` 的 finance 轴是 `blocked`，C-01 状态是
  `needs_review` / `REVIEW_REQUIRED`。已确认后再确认会被拒绝，直到 V-04 的
  修订流程。
- **V14 路径越界**：`sources[].path` 与 `rule_map` 在 ingest 时按
  resolve 后的真实路径检查；`..`、绝对路径和指向 bundle 外的符号链接一律
  `POLICY_DENIED`。
- **V15 幂等与原始哈希**：重复 ingest 生成逐字节相同的 mapping draft；
  scan 保留原始 sha256 和字节数；源文件后来变化时 ingest 报 `STALE_SOURCE`，
  且不覆盖已记录的原始 hash。变更要走 V-04 revision，不能静默重导。
- **半开区间落到金额**：`to` 当天与 `from` 之前的行进入
  `validation.period_outliers`，canonical 不写入这些行，报告把它们列入
  `period_outlier` open items，不进入 `invoice_total`。
- **重复行**：同一 `charge_id` 只有首个可入账行进入 reconciliation rows；
  重复行在 `duplicates`/`duplicate_charge` 中留痕，不进入 V-03 报告行数或
  金额合计。
- **V-14 跨平台路径**：除 POSIX 绝对路径、`..`、符号链接越界外，Windows
  盘符（`C:\...`）、UNC 与反斜杠 `..\...` 也在所有平台拒绝。

## 4. 覆盖诊断与缺失清单

`mapping-draft.json` 固定给出四类覆盖诊断：

| key | 含义 |
| --- | --- |
| `missing_task_id` | 行没有 task_id，也没有规则表里已批准的 allocation rule |
| `shared_account` | 同一账户下出现多个 task，需要已批准的分摊 driver |
| `unapproved_allocation` | 行引用的 rate plan / allocation rule 不在规则表中 |
| `missing_real_cost` | 没有独立成本/用量记录，或账本行缺真实金额（不补 0） |

`build_case` 的 `missing_inputs` 只描述缺什么、为什么缺，不出现金额；例如
`an independent cost or usage record`、`an approved rule map ...`、
`field mapping confirmation by a human reviewer`。单张 invoice 时仍生成
`source_check`，但 `Invoice Reconciled` 保持 `UNPROVABLE`，不会因为有一张
发票就变成 PASS。

## 5. 跨账期复用

`history.next_period_reusable_rules` 只列**有效期覆盖本账期**的 pricing rule，
且每条都带 `requires_re_review_next_period: true`。合同价格、币种、模型计费
方式或账号变化时必须重新审查；上个月 PASS 不能自动证明本月正确。

## 6. 明确不做

- 不做汇率换算、税/credit/退款推断、多账户合并、PDF/XLSX 解析或 OCR；
- 不自动签核、不判定付款、不发起供应商争议；
- 不把 vendor agreement 当买方独立验证，不把数值一致当计费正确；
- V-03 的月结报告与审核门禁见 [close-report.md](close-report.md)；
  V-04 的追加式 revision、delta 与 signoff 见
  [period-revisions.md](period-revisions.md)。
