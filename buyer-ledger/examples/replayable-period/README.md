# 可重放合成账期

两份事件和一份规则映射均为合成数据，不是客户账单，也不证明真实供应商行为。

在 `buyer-ledger/` 下运行：

```bash
period_dir="$(mktemp -d)"
python3 -m buyer_ledger reproduce build \
  --input examples/replayable-period/events.jsonl \
  --period 2026-10 \
  --rule-map examples/replayable-period/rule-map.json \
  --out "$period_dir/private" \
  --data-classification synthetic

python3 -m buyer_ledger reproduce verify "$period_dir/private"
python3 -m buyer_ledger reproduce publish "$period_dir/private" \
  --out "$period_dir/public" --by synthetic-fixture-reviewer
python3 -m buyer_ledger reproduce verify "$period_dir/public"
python3 -m buyer_ledger reproduce proof "$period_dir/private" \
  --out "$period_dir/proof" --by synthetic-fixture-reviewer
```

四个可见阶段是 Intake → Recompute → Review → Decide。重放 `PASS` 只表示包内计费重算一致；Delivery 保持 `UNKNOWN`，Decide 保持人工所有。公开目录含合成输入、重放结果、四步 `index.html`、`template.json` 和发布记录。模板创建的新任务默认 `confidential`，不会继承本次验收结论。

`reproduce proof` 以白名单输出 manifest 与聚合重放字段，不复制私密源输入、原始来源路径或逐行主体。可检查产物完整性与源重放结论，但独立复跑为 `UNAVAILABLE`，外部结果为 `UNCHECKED`。Proof 导出成功不是买方验收，也不是整包 `PASS`。
