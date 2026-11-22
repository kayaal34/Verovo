# 可重放合成账期

两份事件和一份规则映射均为合成数据，不是客户账单，也不证明真实供应商行为。

在 `buyer-ledger/` 下运行：

```bash
python3 -m buyer_ledger reproduce build \
  --input examples/replayable-period/events.jsonl \
  --period 2026-10 \
  --rule-map examples/replayable-period/rule-map.json \
  --out /tmp/verovo-private-period \
  --data-classification synthetic

python3 -m buyer_ledger reproduce verify /tmp/verovo-private-period
python3 -m buyer_ledger reproduce publish /tmp/verovo-private-period \
  --out /tmp/verovo-public-period --by synthetic-fixture-reviewer
python3 -m buyer_ledger reproduce verify /tmp/verovo-public-period
```

四个可见阶段是 Intake → Recompute → Review → Decide。重放 `PASS` 只表示包内计费重算一致；Delivery 保持 `UNKNOWN`，Decide 保持人工所有。公开目录含合成输入、重放结果、四步 `index.html`、`template.json` 和发布记录。模板创建的新任务默认 `confidential`，不会继承本次验收结论。
