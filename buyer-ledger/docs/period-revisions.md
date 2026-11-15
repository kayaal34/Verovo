# 版本化月结与后续调整（V-04）

> **状态：V-04 合成 fixture 验收。** 每个版本独立重算，旧版本目录只读；
> `revise` 新增来源生成新 revision，`signoff` 只对指定 revision 的指纹署名。
> 签核不是付款授权，也不关闭 unresolved items。

```bash
cd buyer-ledger
B="python3 -m buyer_ledger"

# 初版：V-02 导入 + V-03 报告
$B close init ./close-2026-10 --from-job ./bundle/period-job.json
$B close ingest ./close-2026-10 --bundle ./bundle
$B close mapping ./close-2026-10 --confirm --review-by finance
$B close calculate ./close-2026-10
$B close report ./close-2026-10
$B close review ./close-2026-10 --by finance-reviewer --scope "read report"

# V-04：供应商修订 / 新来源 / credit note
$B close revise ./close-2026-10 --bundle ./bundle-v2 \
    --review-by finance --reason "vendor revision"
$B close revise ./close-2026-10 --source ./credit-note.csv --role bills \
    --review-by finance --reason "credit note issued"

# 签核指定版本（不覆盖旧版本）
$B close signoff ./close-2026-10 --revision r2 \
    --by buyer-controller --scope "accept v2 with open credit" \
    --reservation "credit not applied"

$B close status ./close-2026-10              # 默认最新 revision
$B close status ./close-2026-10 --revision r1
```

## 1. 目录与不可覆盖性

首次 `revise` 把 V-02/V-03 根目录完整快照到 `revisions/r1/`，此后每个版本拥有
自己的 `revisions/rN/`：

```
close-2026-10/
  period-job.json                # 根 = r1（V-02/V-03 兼容布局）
  intake/ canonical/ report/ review.json/ signoff.json
  revisions/
    index.json                   # revision 列表与 latest
    r1/                          # 根快照（含 bundle/、report/、signoff.json）
    r2/
      period-job.json
      bundle/                    # 本次版本自己的原始来源副本
      intake/ canonical/ calculation.json report/
      revision.json              # PeriodRevision
      delta.json                 # r1 -> r2
      signoff.json               # 可选，只属于 r2
```

- `revise` 只写新的 staging/revision 目录；根目录和已存在 revision 不被改写。
- 根已经是 r1 后，`close calculate` / `close report` / `close review` 会拒绝
  原地重算（`REVIEW_REQUIRED`），必须用 `close revise` 生成新版本。
- 根版本一旦出现 `signoff.json`，`close review` / `close calculate` /
  `close report` 一律拒绝（`REVIEW_REQUIRED`），必须 `close revise`；签核后
  的 report/signoff 字节与指纹保持不变。
- `calculation.json` 记录 period-job、mapping-confirmed、rule-map 的输入 hash
  和 calculation fingerprint；任一变化时 `close report` 报 `STALE_SOURCE`。
- `close signoff` 只新增 `signoff.json`（并更新 revision/index 的签核状态），
  不修改报告、canonical 或已签核结论。
- 原始来源文件只被复制，不被修改；同一来源的新版本在新 revision 内重新
  ingestion，不与旧版本叠加。

## 2. `PeriodRevision` 字段

`revisions/rN/revision.json`：

| 字段 | 含义 |
| --- | --- |
| `period_job_id` | V-02 period job id |
| `revision_id` | `r1`, `r2`, … |
| `parent_revision_id` | 上一版本；r1 为 null |
| `input_snapshot_hash` | job 身份（job_id/buyer/vendor/account/period/currency）+ 每个来源 role/path/sha256 的语义指纹 |
| `rule_map_hash` | 本次 canonical rule-map.json 的 sha256 |
| `source_added` / `source_removed` | 相对父版本的来源路径增删 |
| `source_changed` | 同 role/path、内容 hash 变化的来源 |
| `source_snapshot` | 本次全部来源的 role/path/sha256 |
| `reviewer` | 最近一次映射确认/签核署名（签核后为签核人） |
| `closed_at` | 签核时间；未签核为 null |
| `status` | `report_ready` / `reviewed` / `signed` / `signed_with_open_items` |
| `report_fingerprint` | V-03 报告的输出快照指纹 |
| `calculation_fingerprint` | 既有 canonical 重算指纹 |
| `signoff` | 签核记录或 null |

`input_snapshot_hash` 和 `rule_map_hash` 相同 = 同一版本输入；`revise` 直接返回
父版本并标 `idempotent: true`，不会生成 `rN+1`。period-job 的纯格式变化不会
改变该语义指纹。

## 3. 三类变化必须分开看

`revisions/rN/delta.json` 的 `classification` 明确分栏：

| 分类 | 内容 |
| --- | --- |
| `amount_changes` | invoice_total / rule_repriced / matched_postings / known_adjustments / open_difference 等金额变化 |
| `cause_changes` | 新增、清除或改变 state/confirmed_cause 的 findings |
| `evidence_added` | 新来源、以及 verdict 从 UNPROVABLE/UNKNOWN/not_applicable 变为 PASS/FAIL 的新证据 |
| `credit_applied` | 本版本实际应用的 credit/refund/discount 行 |
| `credit_issued_not_applied` | 只 issued/pending、尚未应用的 credit/refund 行 |

另有：

- `amounts`：每个金额的 before/after/delta；缺失一侧为 `null`，不补 0；
- `axes` / `changed_axes`：四完成轴的独立变化；
- `causes`：new/cleared/changed findings；
- `evidence`：new/removed/changed sources、verdict changes、newly available；
- `credits`：applied / pending / realized 的 before/after/delta，以及上述两类
  明细；
- `rules`：rule_map_hash 是否变化；
- `revision`：前后 revision 状态。

`realized_savings` 始终是 `0.00`：credit issued 不是 realized；只有实际 applied
的 credit 才减少 amount due，但仍不是“已实现节省”。这与现有
`healthcheck.recovery` 的纪律一致——realized 只算实际 credited/paid，不把
paper concession 当现金；V-04 更严格地要求 credit 明确 `applied` 到本账期。

## 4. 签核门禁

`close signoff`：

- 必须带 `--by`（缺失即 `INPUT_REQUIRED`，exit 2）；
- 只对指定（默认最新）revision 生效；
- 记录 `scope_hash = f(case_id, revision_id, approved_scope, source_hashes,
  rule_map_hash)`、reviewer、decided_at、reservations、unresolved items；
- `attestation=attested_by_declaration`，不是强身份/法律签章；
- `payment_authorization=false`；
- 不把任何 `unresolved` item 改成 `closed`；有未决项时状态为
  `signed_with_open_items`；
- 不修改被签核的报告或 canonical；签核是独立 sidecar；
- 旧 revision 的签核不延伸到新 revision（V27）。

## 5. AC 对照

| AC | 落实 |
| --- | --- |
| V24 | `revise` 对同一 input_snapshot_hash + rule_map_hash 返回父 revision，`idempotent=true`，不生成新目录；重复 calculate/report 的指纹稳定（V-03 已测） |
| V25 | r1 与 r2 都保留；delta 显示 invoice_total 20.00 → 25.00（+5.00）；旧报告 bytes 不变 |
| V26 | issued credit：invoice_total 不变、credits_pending -4、applied/realized delta 0；applied credit：invoice_total -4、applied delta -4、pending +4、realized 仍 0 |
| V27 | 签核 r1 后新增来源生成 r2；r1 signoff/report bytes 不变，r2 无 signoff，r1/r2 结论各自保留 |
| V28 | 同一 role/path 的新内容生成 `source_changed`，新 revision 只按新内容归集，不叠加旧版本 |
| V29 | 无 `--by` 不产生 signoff；有署名也只写 signed/signed_with_open_items，unresolved 保持未闭合 |

## 6. 明确不做 / 边界

- 不做真实客户数据；V-05 才做真实账期试点。
- 不做电子签章、强身份认证或付款授权；`attested_by_declaration` 只是自报声明。
- 不做 revision 删除/压缩；append-only 目录保留全部版本。
- 不做跨 workspace 授权（V37）；本地路径隔离先满足单机场景。
- 不自动把 signed 当 PASS，不把 credit issued 当 realized，也不把签核当供应商
  正确或多收费结论。
- recovery/settlement 的 dispute/结算格式不直接套用到 revision；复用的是
  “issued/confirmed 不等于 realized、未应用不计数”的语义纪律。

## V-06 recovery and signoff freshness

Every write that touches more than one revision file goes through a journal at
`revisions/.journal-<rN>.json`, written and fsynced *before* the first visible
change. The journal records the revision id, its parent, the input snapshot
hash and each expected file hash, so recovery can tell "not started" from
"started and interrupted".

- `revise` builds the whole revision in `revisions/.staging-<rN>/`, then renames
  it to `revisions/<rN>/`, then commits the index. A crash between the rename and
  the index commit leaves a journal behind.
- `signoff` writes `signoff.json`, `revision.json` and `index.json` under the same
  journal, so the three can never disagree after recovery.
- `close status`, `close show`, `close report`, `close revise`, `close signoff`
  call `ensure_recovered()` first: a recoverable journal is completed and
  reported as `recovered`; an unrecoverable one is reported with the pending and
  finished steps, and the command refuses to pretend everything is fine.
- `close recover` is dry-run by default and prints what it would do; `--apply`
  completes the transaction, `--rollback` returns the case to the previous
  consistent state and removes orphan directories.
- An `rN/` directory that exists without an index entry is adopted into the index
  instead of dead-ending the next `revise`. A corrupted promoted revision or a
  corrupted journal is reported, never silently dropped.
- Before signing, the report fingerprints (`calculation_fingerprint`,
  `output_snapshot_fingerprint`), the canonical snapshot and every source file
  hash are re-verified. If anything moved, signing fails with `STALE_SOURCE` and
  names the step to re-run. Signing still does not authorize payment, and open
  items stay open.
