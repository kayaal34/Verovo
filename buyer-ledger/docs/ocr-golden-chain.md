# OCR 账单黄金链（V-08）：候选 → 字段复核 → 入账 → 月结

> **状态：合成 fixture 验收通过（2026-11-17）。** 本页是评审《后续优化评审-20261117》
> §4.3 Verovo 黄金链的落地说明：账单页面/截图/粘贴文本 → OCR 候选草稿 →
> 字段级校验 → 低置信/关键字段人工复核 → book 入既有 close 链（独立复算 +
> delivery review）→ 月结报告与例外处置。签核仍不等于付款。
> 验收日志：[ocr-golden-chain-acceptance-20261117.json](ocr-golden-chain-acceptance-20261117.json)
> （由 `scripts/ocr-golden-chain-acceptance.py` 重放生成）。

```bash
cd buyer-ledger
B="python3 -m buyer_ledger"

# 1) 候选入口：粘贴文本 / 截图（本地 Vision OCR）/ 注入 OCR 结果
printf 'INVOICE No: INV-2026-1023\nVendor: vendor-1\n...' | $B bill-candidate add ./bill-candidates --paste
$B bill-candidate add ./bill-candidates --image shot.png                 # 本地 Vision
$B bill-candidate add ./bill-candidates --image shot.png --ocr-json o.json
$B bill-candidate add ./bill-candidates --text bill.txt --engine paid-cloud-ocr  # → requires_procurement，退出 2

# 2) 字段级人工复核（关键字段全部要过；可纠正 OCR 值，原值保留）
$B bill-candidate show ./bill-candidates cand-0001
$B bill-candidate review ./bill-candidates cand-0001 --field amount  --value 20.00 --by finance
$B bill-candidate review ./bill-candidates cand-0001 --field period  --value 2026-10 --by finance
$B bill-candidate review ./bill-candidates cand-0001 --field currency --value USD --by finance
$B bill-candidate review ./bill-candidates cand-0001 --field vendor  --value vendor-1 --by finance
$B bill-candidate review ./bill-candidates cand-0001 --field invoice_no --value INV-2026-1023 --by finance

# 3) 显式入账：导出 bills 源 + 原始凭证副本 + verovo.period-job/1
$B bill-candidate book ./bill-candidates cand-0001 --by finance \
    --into ./bundle --account acct-1 --usage usage-export.jsonl --rule-map contract-rules.json

# 4) 既有月结链（V-02/V-03/V-04 原样）
$B close init ./close-2026-10 --from-job ./bundle/period-job.json
$B close ingest ./close-2026-10 --bundle ./bundle
$B close mapping ./close-2026-10 --confirm --review-by finance
$B close calculate ./close-2026-10 && $B close report ./close-2026-10
$B close review ./close-2026-10 --by finance --scope "ocr chain"
```

机器可读交付合同：`python3 -m buyer_ledger contract show`
（[docs/delivery-contract.json](delivery-contract.json)）。

## 1. 候选模型（`verovo.bill-candidate/1`）

```
bill-candidates/
  cand-0001/
    candidate.json   # 草稿 + 字段级证据 + 复核记录 + booking 回执
    source.png|txt   # 原始截图/文本，逐字节保留（sha256 记录）
    ocr.json         # verovo.ocr-lines/1：{text, confidence, bbox}[]
```

每个字段（`vendor/invoice_no/period/currency/amount/line_items`）携带：

| 字段 | 含义 |
| --- | --- |
| `value` | OCR/解析候选值；坏 OCR 一律为空，绝不猜测 |
| `confidence` | 行置信度（粘贴文本为 1.0，OCR 来自引擎） |
| `evidence` | `text_span`（行号+字符区间+摘录）或 `image_region`（bbox，归一化） |
| `flags` | `low_confidence`（<0.90）/ `missing_evidence`（截图路径无 bbox）/ `ambiguous_symbol`（仅 `$`/`¥`）/ `line_sum_mismatch` / `not_extracted` |
| `review` | 人工复核记录：`reviewer/value/original_value/note/at`，`attested_by_declaration` |

状态机：`unreviewed →（字段复核）→ reviewed →（book）→ booked`；坏 OCR 直接
`blocked_user_input`。终态投影（§3.1）：draft/待复核 = `BLOCKED_USER_INPUT`
（review_pending），booked = `DELIVERED`（带回执），坏 OCR =
`BLOCKED_USER_INPUT`（bad_ocr，唯一用户行动）。**UNPROVABLE 只是 close 报告
里的核验结论，不是交付终态。**

## 2. 红线（与测试一一对应）

- **OCR 草稿永不自动入账**：高置信也只进 draft；`book` 前复核队列必须清零
  （关键字段 + 低置信/缺证据字段全部人工过目），入账是带署名的人工动作。
- **关键字段必须能回到证据区域**：截图路径无 bbox、文本路径无 span →
  `missing_evidence` → 进复核队列，不猜测。
- **坏 OCR 不变假精确**：有效字符占比 <0.6 或有效字符 <12 → `bad_ocr`，
  字段全空、草稿保留、给出唯一用户行动（重拍/改粘贴）。
- **重复发票不双计**：同 vendor+invoice_no 已 booked → 第二次 `book` 拒绝
  （REVIEW_REQUIRED），差异走 close 报告的争议材料。
- **采购分支**：`--engine paid-cloud-ocr` 输出 §5 结构化 JSON
  （`requires_procurement`，`transaction_executed=false`），不购买、不联网。
- **booking 不覆盖**：目标包已有 `period-job.json` 即拒绝；修订走 V-04。

## 3. 与既有 close 链的接缝

`book` 导出三样东西进账期包：

1. `<invoice>-invoice.csv`：bills 源（表头走既有同义词映射），行级证据列
   `source_party/collector_party/control_domain/collection_method/
   confirmation_process/source_path/evidence_ref` 指回候选与字段证据行；
2. 原始截图/文本副本：以 `invoice` 角色（document）进包，canonical 里保持
   `unreviewed/unbooked`，永不成为账本行（V-11 不变量前移到入口）；
3. `period-job.json`：period 取复核后的半开区间，currency/vendor/invoice_no
   取复核值，account 必填不猜测；usage/rule-map 由操作者补充为独立复算来源。

之后就是既有链：`close ingest → mapping --confirm → calculate → report →
review → signoff`。差异项（复算差、账期越界、缺任务分摊等）以 open items
进入 review queue，每项带 `owner` 与 `next_evidence`（处置动作）；
`review` 记录署名但不关闭未决项，`signoff` 绑定指纹且明示不是付款授权。

## 4. 边界与未验收项

- 本轮验收全部为**合成 fixture**（截图路径用注入 OCR 结果）；真实 Vision
  OCR 质量、真实账单版式多样性未验收，`scripts/ocr_image.swift` 提供
  zh-Hans/en-US 精确识别 + bbox 输出，供后续 live 验收。
- 多币种、税额、汇率、PDF/XLSX 仍属排除项（见交付合同）。
- CDP 账单页面采集入口未在本轮实现（不碰浏览器是本轮纪律）。
