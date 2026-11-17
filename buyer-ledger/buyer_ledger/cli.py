"""buyer-ledger CLI：全部本地运行。

  python3 -m buyer_ledger init --db ledger.db
  python3 -m buyer_ledger import bill.csv --provider example --tier bill_fact --period 2026-10
  python3 -m buyer_ledger parse --document <id> --agent
  python3 -m buyer_ledger policy policy.json
  python3 -m buyer_ledger price --provider example --period 2026-10
  python3 -m buyer_ledger recipe validate recipe.json / dryrun recipe.json
  python3 -m buyer_ledger session login --recipe recipe.json
  python3 -m buyer_ledger fetch --recipe recipe.json --period 2026-10
  python3 -m buyer_ledger catalog summary
  python3 -m buyer_ledger catalog list --region cn --role buyer
  python3 -m buyer_ledger session cdp --endpoint http://127.0.0.1:9222
  python3 -m buyer_ledger fetch --recipe recipe.json --period 2026-10 --cdp ...
  python3 -m buyer_ledger reconcile --period 2026-10
  python3 -m buyer_ledger report --period 2026-10 --perspective seller --out report.md
  python3 -m buyer_ledger show <row-id> / status / audit verify <trail.jsonl>
"""

import argparse
import json
import sys

from . import browser, catalog as catalog_mod, close as close_mod, ingest as ingest_mod, reconcile as recon_mod, report as report_mod
from .llm import LLMClient, LLMError
from .model import LedgerError, PricingPolicy
from .store import LedgerStore

DEFAULT_DB = "buyer-ledger.db"


def _store(args):
    return LedgerStore(args.db or DEFAULT_DB)


def cmd_init(args):
    store = _store(args)
    print("ledger ready: %s" % (args.db or DEFAULT_DB))
    store.close()


def cmd_import(args):
    store = _store(args)
    llm = LLMClient() if args.agent else None
    result = ingest_mod.import_file(
        store, args.file, provider=args.provider, tier=args.tier,
        period=args.period, doc_type=args.doc_type, account_ref=args.account,
        use_agent_if_new=args.agent, llm_client=llm)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    store.close()


def cmd_parse(args):
    store = _store(args)
    doc = store.get_document(args.document)
    if not doc:
        print("document not found: %s" % args.document)
        sys.exit(1)
    if doc["tier"] not in ("bill_fact", "platform_usage"):
        print("reference-tier document; nothing to parse")
        store.close()
        return
    text, current_hash = ingest_mod.read_and_hash(doc["file_path"])
    if current_hash != doc["content_hash"]:
        store.close()
        raise ValueError("source file changed after registration; import it as a new document")
    fingerprint = ingest_mod.parsing.format_fingerprint(doc["file_name"], text)
    scope = {"provider": doc["provider"], "doc_type": doc["doc_type"], "tier": doc["tier"]}
    rule = store.get_parse_rule(fingerprint, **scope)
    if rule is None:
        if not args.agent:
            store.close()
            print("no approved rule for fingerprint %s; rerun with --agent" % fingerprint)
            sys.exit(1)
        spec, golden = ingest_mod.parsing.agent_generate_spec(
            doc["file_name"], text, doc["provider"], doc["doc_type"], LLMClient())
        store.put_parse_rule(fingerprint, spec, created_via="agent",
                             golden=golden, approved=False, **scope)
        print(json.dumps({"fingerprint": fingerprint, "spec_source": "agent_draft",
                          "review_required": True, "spec": spec,
                          "next": "rule approve %s --provider %s --doc-type %s --tier %s" %
                                  (fingerprint, doc["provider"], doc["doc_type"], doc["tier"])},
                         ensure_ascii=False, indent=2))
        store.close()
        return
    if not rule["approved"]:
        print(json.dumps({"review_required": True, "fingerprint": fingerprint,
                          "spec": rule["spec"],
                          "next": "run rule approve, then rerun parse"},
                         ensure_ascii=False, indent=2))
        store.close()
        return
    from .model import SourceDocument
    doc_obj = SourceDocument(
        content_hash=doc["content_hash"], provider=doc["provider"], tier=doc["tier"],
        account_ref=doc["account_ref"], doc_type=doc["doc_type"], period=doc["period"],
        file_path=doc["file_path"], file_name=doc["file_name"])
    result = ingest_mod.parsing.apply_spec(
        doc["file_name"], text, rule["spec"], doc_obj, doc["provider"],
        period=doc["period"], review_status="verified")
    n_lines = n_events = 0
    try:
        for cl in result["charge_lines"]:
            _, inserted = store.put_charge_line(cl, commit=False)
            n_lines += inserted
        for ue in result["usage_events"]:
            _, inserted = store.put_usage_event(ue, commit=False)
            n_events += inserted
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise
    print(json.dumps({"fingerprint": fingerprint, "spec_via": rule.get("created_via"),
                      "charge_lines": n_lines, "usage_events": n_events,
                      "notes": result["notes"], "constraint": result["constraint"]},
                     ensure_ascii=False, indent=2))
    store.close()


def cmd_rule(args):
    store = _store(args)
    if args.action == "list":
        rows = [{k: r[k] for k in ("fingerprint", "provider", "doc_type", "tier",
                                   "created_via", "approved")}
                for r in store.list_parse_rules()]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        scope = {"provider": args.provider, "doc_type": args.doc_type, "tier": args.tier}
        if args.action == "show":
            rule = store.get_parse_rule(args.fingerprint, **scope)
            if not rule:
                store.close()
                raise ValueError("parse rule not found")
            print(json.dumps(rule, ensure_ascii=False, indent=2))
        else:
            rule = store.get_parse_rule(args.fingerprint, **scope)
            if not rule:
                store.close()
                raise ValueError("parse rule not found")
            errors = ingest_mod.parsing.validate_spec(rule["spec"])
            if errors:
                store.close()
                raise ValueError("refusing invalid rule: %s" % "; ".join(errors))
            store.approve_parse_rule(args.fingerprint, **scope)
            print("rule approved; rerun `parse --document <doc-id>` to apply it")
    store.close()


def cmd_preset(args):
    from . import presets as presets_mod
    if args.action == "list":
        for name, p in presets_mod.PRESETS.items():
            print("%-22s %-12s %-8s %s" % (name, p["doc_type"], p["tier"], p["status"]))
        return
    if args.action == "show":
        if args.name not in presets_mod.PRESETS:
            raise ValueError("preset not found: %s" % args.name)
        p = presets_mod.PRESETS[args.name]
        print(json.dumps({"name": args.name, "provider": p["provider"],
                          "doc_type": p["doc_type"], "tier": p["tier"],
                          "status": p["status"],
                          "expected_header": p["header"], "spec": p["spec"]},
                         ensure_ascii=False, indent=2))
        return
    # apply：登记为已批准规则；真实文件表头一致时 import 直接重放
    store = _store(args)
    try:
        fingerprint = presets_mod.apply_preset(store, args.name)
    except KeyError:
        store.close()
        raise ValueError("preset not found: %s" % args.name)
    p = presets_mod.PRESETS[args.name]
    print(json.dumps({"applied": args.name, "provider": p["provider"],
                      "doc_type": p["doc_type"], "tier": p["tier"],
                      "fingerprint": fingerprint, "created_via": "preset"},
                     ensure_ascii=False, indent=2))
    store.close()


def cmd_catalog(args):
    catalog = catalog_mod.load_catalog()
    if args.action == "summary":
        print(json.dumps(catalog_mod.support_summary(catalog), ensure_ascii=False, indent=2))
        return
    if args.action == "show":
        try:
            provider = catalog_mod.get_provider(args.provider, catalog)
        except KeyError as exc:
            raise ValueError(str(exc))
        print(json.dumps(provider, ensure_ascii=False, indent=2))
        return
    if args.action == "coverage":
        store = _store(args)
        period = args.period or None
        providers = {r["provider"] for r in store.effective_charge_lines(period=period)} | {
            r["provider"] for r in store.effective_usage_events(period=period)}
        store.close()
        print(json.dumps(catalog_mod.coverage(providers, catalog),
                         ensure_ascii=False, indent=2))
        return
    providers = catalog_mod.list_providers(
        region=args.region, role=args.role, category=args.category,
        query=args.query, catalog=catalog)
    if args.json:
        print(json.dumps(providers, ensure_ascii=False, indent=2))
        return
    print("%-28s %-6s %-18s %-11s %-16s %s" %
          ("ID", "REGION", "CATEGORY", "ROLES", "READER", "NAME"))
    for p in providers:
        print("%-28s %-6s %-18s %-11s %-16s %s" % (
            p["id"], p["region"], p["category"], ",".join(p["roles"]),
            p["reader_status"], p["name"]))
    print("\n%d tools; cataloged != live-validated" % len(providers))


def cmd_policy(args):
    store = _store(args)
    with open(args.file, "r", encoding="utf-8") as f:
        data = json.load(f)
    policies = data if isinstance(data, list) else [data]
    for p in policies:
        store.put_policy(PricingPolicy(**p))
        print("policy saved: %s@%s (%s/%s)" % (p["policy_id"], p["version"],
                                               p["provider"], p.get("capability", "")))
    store.close()


def cmd_price(args):
    store = _store(args)
    print(json.dumps(recon_mod.apply_pricing(store, args.provider, args.period),
                     ensure_ascii=False))
    store.close()


def cmd_reconcile(args):
    store = _store(args)
    results = recon_mod.reconcile(store, args.period)
    for r in results:
        def show(value):
            return "—" if value in (None, "") else value
        print("%-16s account=%-12s currency=%-4s bill=%-12s recount=%-12s "
              "variance=%-12s %s" % (
                  r["provider"], r["account_ref"] or "—", r["currency"] or "—",
                  show(r["bill_total"]), show(r["recount_total"]),
                  show(r["variance"]), r["variance_class"]))
    store.close()


def cmd_report(args):
    store = _store(args)
    results = store.list_reconciliations(args.period)
    if not results:
        print("no reconciliation for %s; run reconcile first" % args.period)
        sys.exit(1)
    docs = store.list_documents(period=args.period)
    rendered = []
    for row in results:
        detail = json.loads(row["detail_json"])
        rendered.append({"period": row["period"], "provider": row["provider"],
                         "account_ref": row["account_ref"], "currency": row["currency"],
                         "bill_total": row["bill_total"],
                         "recount_total": row["recount_total"],
                         "recount_complete": detail.get("recount_complete", True),
                         "variance": row["variance"],
                         "variance_class": row["variance_class"],
                         "detail": detail})
    catalog_coverage = catalog_mod.coverage(
        [row["provider"] for row in results])
    md = report_mod.render_report(
        args.period, rendered, documents_count=len(docs),
        ledger_path=args.db or DEFAULT_DB,
        catalog_coverage=catalog_coverage,
        perspective=getattr(args, "perspective", "buyer"))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print("report written: %s" % args.out)
    else:
        print(md)
    store.close()


def cmd_run(args):
    """日常轻量入口：为账期内全部 provider 复算，然后对账并生成报告。"""
    store = _store(args)
    providers = sorted({e["provider"] for e in
                        store.effective_usage_events(period=args.period)})
    pricing = {provider: recon_mod.apply_pricing(store, provider, args.period)
               for provider in providers}
    store.close()
    print(json.dumps({"pricing": pricing}, ensure_ascii=False))
    cmd_reconcile(args)
    cmd_report(args)


def cmd_recipe(args):
    recipe = browser.load_recipe(args.file)
    if args.cmd == "validate":
        errors = browser.validate_recipe(recipe)
        if errors:
            print("INVALID:\n" + "\n".join("- " + e for e in errors))
            sys.exit(1)
        print("recipe OK: %s (%d steps, readonly)" % (recipe["recipe_id"],
                                                      len(recipe["steps"])))
    else:
        print(json.dumps(browser.dry_run(recipe), ensure_ascii=False, indent=2))


def cmd_session(args):
    if args.action == "cdp":
        if not args.endpoint:
            raise ValueError("--endpoint is required for session cdp")
        print(json.dumps(browser.cdp_health(args.endpoint), ensure_ascii=False, indent=2))
        return
    if not args.recipe:
        raise ValueError("--recipe is required for session login/status")
    recipe = browser.load_recipe(args.recipe)
    if args.action == "status":
        status = browser.session_health(recipe, account_ref=args.account)
        print(json.dumps({"status": status, "account": args.account}, ensure_ascii=False))
        sys.exit(0 if status == "ok" else 2)
    ok = browser.open_login_session(recipe, account_ref=args.account)
    sys.exit(0 if ok else 1)


def cmd_fetch(args):
    recipe = browser.load_recipe(args.recipe)
    store = _store(args)
    doc_ids = []

    def on_document(path, name, provenance):
        result = ingest_mod.import_file(
            store, path, provider=recipe["provider"],
            tier=recipe.get("tier", "bill_fact"), period=args.period,
            doc_type=recipe.get("doc_type", "bill"), account_ref=args.account,
            fetched_via="browser_recipe:" + recipe["recipe_id"],
            fetched_at=provenance["fetched_at"], extra=provenance)
        doc_ids.append(result)
        print("ingested: %s" % json.dumps(result, ensure_ascii=False))
        return result

    try:
        summary = browser.run_recipe(recipe, period=args.period,
                                     headless=not args.headed,
                                     on_document=on_document, account_ref=args.account,
                                     cdp_endpoint=args.cdp)
        summary["ingestions"] = doc_ids
        summary["needs_parse"] = any(
            not d.get("duplicate") and not (d.get("charge_lines") or d.get("usage_events"))
            for d in doc_ids)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary["needs_parse"]:
            print("采集完成但尚未形成账本行：请完成解析规则审核后运行 parse。")
            sys.exit(2)
    except browser.Handoff as h:
        print("NEEDS MANUAL HANDOFF: %s" % h)
        print("audit: %s" % getattr(h, "audit_path", "—"))
        print("请手动导出后运行：python3 -m buyer_ledger --db %s import <file> "
              "--provider %s --tier %s --period %s --account %s" %
              (args.db or DEFAULT_DB, recipe["provider"], recipe.get("tier", "bill_fact"),
               args.period, args.account))
        sys.exit(2)
    finally:
        store.close()
    if args.analyze:
        cmd_run(args)


def cmd_show(args):
    store = _store(args)
    for table in ("charge_line", "usage_event"):
        row = store.conn.execute(
            "SELECT * FROM %s WHERE id=?" % table, (args.id,)).fetchone()
        if row:
            d = dict(row)
            d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
            print(json.dumps(d, ensure_ascii=False, indent=2))
            store.close()
            return
    print("not found: %s" % args.id)
    store.close()


def cmd_status(args):
    store = _store(args)
    docs = store.list_documents()
    print("documents: %d" % len(docs))
    for tier in ("bill_fact", "platform_usage", "reference"):
        n = len([d for d in docs if d["tier"] == tier])
        print("  %-16s %d" % (tier, n))
    n_charge = store.conn.execute("SELECT COUNT(*) c FROM charge_line").fetchone()["c"]
    n_usage = store.conn.execute("SELECT COUNT(*) c FROM usage_event").fetchone()["c"]
    print("charge lines: %d (effective %d)" % (
        n_charge, len(store.effective_charge_lines())))
    print("usage events: %d (effective %d)" % (
        n_usage, len(store.effective_usage_events())))
    store.close()


def cmd_audit(args):
    entries = []
    with open(args.file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    ok, reason = browser.verify_chain(entries)
    print("chain %s (%d entries) %s" % ("VERIFIED" if ok else "BROKEN",
                                        len(entries), reason))
    sys.exit(0 if ok else 1)


def cmd_close_init(args):
    result = close_mod.init_case(
        args.case, job_path=args.from_job or None, job_id=args.job_id,
        buyer=args.buyer, vendor=args.vendor, account=args.account,
        currency=args.currency, period=args.period, timezone_=args.timezone,
        task_economics_requested=args.task_economics, rule_map=args.rule_map or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_close_ingest(args):
    result = close_mod.ingest(args.case, bundle=args.bundle or None)
    draft = result["mapping_draft"]
    intake = args.case.rstrip("/") + "/intake"
    print("scan: %s/scan.json" % intake)
    print("mapping-draft.json: %s/mapping-draft.json" % intake)
    print("state: %s%s" % (draft["state"],
                           " (idempotent re-ingest)" if result["idempotent"] else ""))
    print("sources: %d; docs: %d; open items: %d; blocked: %s"
          % (sum(len(v) if isinstance(v, list) else 1
                 for v in draft["sources"].values()),
             len(result["documents"]), len(draft["open_items"]),
             draft["validation"]["blocked_code"] or "no"))
    if draft["validation"]["blocked"]:
        print("blocked: %s" % draft["validation"]["blocked_reason"])


def _parse_control_domains(values):
    domains = {}
    for raw in values or []:
        role, sep, domain = raw.partition("=")
        if not sep or not role.strip() or not domain.strip():
            raise close_mod.CloseError(
                "INPUT_REQUIRED",
                "--control-domain must be role=vendor|buyer (got %r)" % raw)
        domains[role.strip()] = domain.strip()
    return domains


def cmd_close_mapping(args):
    if not args.confirm:
        path = args.case.rstrip("/") + "/intake/mapping-draft.json"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                draft = json.load(handle)
        except OSError as exc:
            raise close_mod.CloseError(
                "INPUT_REQUIRED", "no mapping draft found; run close ingest first: %s" % exc)
        print("mapping draft: %s" % path)
        print("state: %s; review required before canonical generation" % draft.get("state"))
        for role, entry in (draft.get("sources") or {}).items():
            entries = entry if isinstance(entry, list) else [entry]
            for item in entries:
                print("  %s: %s -> %s" % (role, item.get("path"),
                                          item.get("columns") or {}))
        print("open items: %d" % len(draft.get("open_items") or []))
        print("confirm with: close mapping %s --confirm --review-by <reviewer>"
              % args.case)
        return
    confirmation = close_mod.confirm_mapping(
        args.case, reviewer=args.review_by,
        control_domains=_parse_control_domains(args.control_domain),
        note=args.note)
    print("mapping confirmed by %s (%s)"
          % (confirmation["confirmed_by"], confirmation["attestation"]))
    print("canonical: %s/canonical" % args.case)


def _close_status_markdown(payload):
    identity = payload.get("identity") or {}
    account_ref = identity.get("buyer_account_ref") or {}
    period = identity.get("period_range") or {}
    c01 = payload.get("c01_status") or {}
    facts = payload.get("computed_facts") or {}
    review = payload.get("review_flow") or {}
    lines = [
        "# 月结状态 · %s" % (payload.get("close_case_id") or "—"),
        "",
        "- 买方：%s" % (account_ref.get("buyer") or "—"),
        "- 供应商：%s" % (identity.get("vendor_ref") or "—"),
        "- 账户：%s" % (account_ref.get("account") or "—"),
        "- 账期：%s → %s（%s）" % (
            period.get("from") or "—", period.get("to") or "—",
            period.get("timezone") or "—"),
        "- 币种：%s" % (identity.get("currency") or "—"),
        "",
        "## 状态",
        "",
        "- 任务状态：%s｜next_actor：%s" % (
            c01.get("task_state") or "—", c01.get("next_actor") or "—"),
        "- 阻断码：%s" % (c01.get("blocked_reason_code") or "无"),
        "- 完成轴：%s" % json.dumps(facts.get("completion_axes") or {},
                                    ensure_ascii=False),
        "- 复核：%s｜签核版本：%s" % (
            review.get("assigned_reviewer") or "未指派",
            review.get("signoff_revision") or "未签核"),
        "- 未闭合项：%d" % len(review.get("review_queue") or []),
        "",
        "## 缺失输入",
        "",
    ]
    missing = payload.get("missing_inputs") or []
    if missing:
        for item in missing:
            lines.append("- %s：%s" % (item.get("item") or "—",
                                        item.get("why") or ""))
    else:
        lines.append("- 无")
    lines += ["", "## 边界", "", "- %s" % (payload.get("boundary") or "—"), ""]
    return "\n".join(lines)


def cmd_close_status(args):
    from . import close_revision
    recovery = close_revision.ensure_recovered(args.case)
    target = close_revision.resolve_case_dir(args.case, args.revision)
    payload = close_mod.build_case(target)
    payload["recovery"] = recovery
    fmt = getattr(args, "format", "auto")
    if fmt == "auto":
        fmt = "md" if sys.stdout.isatty() else "json"
    if fmt == "md":
        print(_close_status_markdown(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_close_recover(args):
    from . import close_revision
    result = close_revision.recover(
        args.case, apply=args.apply, rollback=args.rollback)
    if args.apply:
        if result["applied"]:
            print("applied %d transaction(s): %s"
                  % (len(result["applied"]),
                     ", ".join(item.get("revision_id") or "?"
                               for item in result["applied"])))
        else:
            print("nothing to recover; index and revisions are consistent")
        return
    if args.rollback:
        if result["rolled_back"]:
            print("rolled back %d transaction(s): %s"
                  % (len(result["rolled_back"]),
                     ", ".join(item.get("revision_id") or "?"
                               for item in result["rolled_back"])))
        else:
            print("nothing to roll back; index and revisions are consistent")
        return
    issues = result["issues"]
    if not issues:
        print("nothing to recover; index and revisions are consistent")
        return
    print("recovery plan (dry-run): %d transaction(s)" % len(issues))
    for issue in issues:
        print("  %s %s: %s" % (issue.get("revision_id") or "?",
                               issue.get("journal_kind") or issue.get("kind"),
                               issue.get("state")))
        for step in issue.get("steps") or []:
            suffix = ""
            if step.get("problems"):
                suffix = " [%s]" % ", ".join(
                    problem.get("problem", "?")
                    for problem in step["problems"])
            print("    %s: %s%s" % (step.get("name"), step.get("state"),
                                    suffix))
        if issue.get("message"):
            print("    %s" % issue["message"])
    print("apply:    close recover %s --apply" % args.case)
    print("rollback: close recover %s --rollback" % args.case)


def _recover_before_write(args):
    from . import close_revision
    recovery = close_revision.ensure_recovered(args.case, strict=True)
    if recovery["state"] == "recovered":
        print("recovery: %s" % recovery["message"])
    return recovery


def cmd_close_calculate(args):
    from . import close_report, close_revision
    _recover_before_write(args)
    if close_revision.list_revisions(args.case):
        raise close_mod.CloseError(
            "REVIEW_REQUIRED",
            "the root is revision r1 and must stay immutable; create a new "
            "revision with close revise instead of recalculating it")
    target = args.case
    calculation = close_report.calculate(target)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(calculation, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print("calculation: %s/calculation.json" % target)
    print("schema: %s" % calculation["schema"])
    print("canonical fingerprint: %s" % calculation["canonical_snapshot"]["fingerprint"])
    print("axes: %s" % json.dumps(calculation["statuses"], ensure_ascii=False))
    print("calculation fingerprint: %s" % calculation["calculation_fingerprint"])


def cmd_close_report(args):
    from . import close_report, close_revision
    _recover_before_write(args)
    if close_revision.list_revisions(args.case):
        raise close_mod.CloseError(
            "REVIEW_REQUIRED",
            "the root is revision r1 and must stay immutable; create a new "
            "revision with close revise instead of rebuilding its report")
    target = args.case
    report = close_report.build_report(target)
    print("report dir: %s/report" % target)
    for name in ("report.md", "report.json", "line-reconciliation.csv",
                 "review-queue.csv", "evidence-index.json", "manifest.json"):
        print("  %s" % name)
    print("finance=%s verification=%s delivery=%s economics=%s"
          % (report["finance_status"], report["verification_status"],
             report["delivery_status"], report["economics_status"]))
    print("open difference: %s %s" % (
        report["amounts"]["open_difference"] or "—",
        report["amounts"]["currency"]))
    print("review: %s" % report["review"]["state"])


def cmd_close_review(args):
    from . import close_report, close_revision
    _recover_before_write(args)
    if close_revision.list_revisions(args.case):
        raise close_mod.CloseError(
            "REVIEW_REQUIRED",
            "the root is revision r1 and must stay immutable; record a "
            "reference review before revisions or sign off a revision")
    target = args.case
    review = close_report.record_review(
        target, reviewer=args.by, approved_scope=args.scope,
        reservations=args.reservation, next_review=args.next_review)
    print("reviewed by %s (%s)" % (review["reviewer"], review["attestation"]))
    print("scope: %s" % (review["approved_scope"] or "—"))
    print("signoff state: %s" % review["signoff"]["state"])
    print("unresolved items: %d (never closed by review)"
          % len(review["unresolved_items"]))
    print("payment authorization: %s" % review["payment_authorization"])


def cmd_close_revise(args):
    from . import close_revision
    _recover_before_write(args)
    result = close_revision.revise(
        args.case, bundle=args.bundle or None, source=args.source or None,
        role=args.role, reason=args.reason, reviewer=args.review_by,
        control_domains=_parse_control_domains(args.control_domain))
    revision = result["revision"]
    print("revision: %s (parent %s)" % (
        revision["revision_id"], revision["parent_revision_id"]))
    print("idempotent: %s" % result["idempotent"])
    print("status: %s" % revision["status"])
    print("sources: added=%s removed=%s changed=%s" % (
        revision["source_added"], revision["source_removed"],
        revision["source_changed"]))
    if result.get("delta"):
        amounts = result["delta"]["amounts"]
        print("delta invoice_total: %s -> %s (%s)" % (
            amounts["invoice_total"]["before"], amounts["invoice_total"]["after"],
            amounts["invoice_total"]["delta"]))
        print("delta axes: %s" % (result["delta"]["changed_axes"] or "none"))
        credits = result["delta"]["credits"]
        print("credits applied delta=%s pending delta=%s realized delta=%s"
              % (credits["applied"]["delta"], credits["pending"]["delta"],
                 credits["realized"]["delta"]))
    if result.get("path"):
        print("path: %s" % result["path"])


def cmd_close_signoff(args):
    from . import close_revision
    _recover_before_write(args)
    record = close_revision.signoff(
        args.case, reviewer=args.by, approved_scope=args.scope,
        reservations=args.reservation, note=args.note,
        revision_id=args.revision)
    print("signed revision %s by %s (%s)"
          % (record["revision_id"], record["reviewer"], record["state"]))
    print("unresolved items: %d (not closed by signoff)"
          % len(record["unresolved_items"]))
    print("payment authorization: %s" % record["payment_authorization"])
    print("scope hash: %s" % record["scope_hash"])


def build_parser():
    p = argparse.ArgumentParser(prog="buyer-ledger",
                                description="买方侧统一费用账本与对账审查")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite 账本路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化账本").set_defaults(func=cmd_init)

    sp = sub.add_parser("import", help="导入材料（L2 / 浏览器下载物）")
    sp.add_argument("file")
    sp.add_argument("--provider", required=True)
    sp.add_argument("--tier", required=True,
                    choices=["bill_fact", "platform_usage", "reference"])
    sp.add_argument("--period", default="")
    sp.add_argument("--doc-type", default="bill")
    sp.add_argument("--account", default="")
    sp.add_argument("--agent", action="store_true",
                    help="指纹未命中时用 LLM 生成解析规格")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("parse", help="解析已登记文档（确定性重放或 --agent）")
    sp.add_argument("--document", required=True)
    sp.add_argument("--agent", action="store_true")
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("rule", help="查看/批准 LLM 生成的解析规则草案")
    sp.add_argument("action", choices=["list", "show", "approve"])
    sp.add_argument("fingerprint", nargs="?", default="")
    sp.add_argument("--provider", default="")
    sp.add_argument("--doc-type", default="")
    sp.add_argument("--tier", default="")
    sp.set_defaults(func=cmd_rule)

    sp = sub.add_parser("preset", help="内置导出格式预设（登记已知表头的解析规则）")
    sp.add_argument("action", choices=["list", "show", "apply"])
    sp.add_argument("name", nargs="?", default="")
    sp.set_defaults(func=cmd_preset)

    sp = sub.add_parser("catalog", help="国内外 Agent 工具账单/计费/审计能力目录")
    sp.add_argument("action", choices=["list", "show", "coverage", "summary"])
    sp.add_argument("provider", nargs="?", default="")
    sp.add_argument("--region", choices=["cn", "global"])
    sp.add_argument("--role", choices=["buyer", "seller"])
    sp.add_argument("--category", default="")
    sp.add_argument("--query", default="")
    sp.add_argument("--period", default="")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_catalog)

    sp = sub.add_parser("policy", help="登记价格政策（同版本不可变）")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_policy)

    sp = sub.add_parser("price", help="把价格政策应用到用量（复算估算层）")
    sp.add_argument("--provider", required=True)
    sp.add_argument("--period", required=True)
    sp.set_defaults(func=cmd_price)

    sp = sub.add_parser("reconcile", help="对账一个账期")
    sp.add_argument("--period", required=True)
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser("report", help="生成对账报告")
    sp.add_argument("--period", required=True)
    sp.add_argument("--out", default="")
    sp.add_argument("--perspective", choices=["buyer", "seller"], default="buyer",
                    help="买家对账或卖家计量完整性视角")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("run", help="一键复算、对账并生成报告")
    sp.add_argument("--period", required=True)
    sp.add_argument("--out", default="")
    sp.add_argument("--perspective", choices=["buyer", "seller"], default="buyer")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("recipe", help="配方校验/演练")
    sp.add_argument("action", choices=["validate", "dryrun"])
    sp.add_argument("file")
    sp.set_defaults(func=cmd_recipe)

    sp = sub.add_parser("session", help="会话管理")
    sp.add_argument("action", choices=["login", "status", "cdp"])
    sp.add_argument("--recipe", default="")
    sp.add_argument("--account", default="")
    sp.add_argument("--endpoint", default="",
                    help="本地 CDP 地址，如 http://127.0.0.1:9222")
    sp.set_defaults(func=cmd_session)

    sp = sub.add_parser("fetch", help="L1 浏览器采集（配方重放）")
    sp.add_argument("--recipe", required=True)
    sp.add_argument("--period", required=True)
    sp.add_argument("--account", default="")
    sp.add_argument("--cdp", default="",
                    help="附着本地 Chrome CDP，如 http://127.0.0.1:9222")
    sp.add_argument("--analyze", action="store_true",
                    help="解析成功后自动复算、对账并生成报告")
    sp.add_argument("--out", default="")
    sp.add_argument("--perspective", choices=["buyer", "seller"], default="buyer")
    sp.add_argument("--headed", action="store_true", help="有头模式（调试）")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("show", help="查看任意行的完整字段与证据")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_show)

    sub.add_parser("status", help="账本概况").set_defaults(func=cmd_status)

    sp = sub.add_parser("audit", help="审计链校验")
    sp.add_argument("action", choices=["verify"])
    sp.add_argument("file")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("close",
                        help="账期包导入、人工字段映射与 BuyerCloseCase（V-02）")
    close_sub = sp.add_subparsers(dest="close_command", required=True)

    sp = close_sub.add_parser("init", help="创建账期案例（--from-job 或显式参数）")
    sp.add_argument("case", help="案例目录，如 ./close-2026-10")
    sp.add_argument("--from-job", default="", help="verovo.period-job/1 JSON 路径")
    sp.add_argument("--job-id", default="")
    sp.add_argument("--buyer", default="")
    sp.add_argument("--vendor", default="")
    sp.add_argument("--account", default="")
    sp.add_argument("--period", default="", help="YYYY-MM；写入半开区间 [from, to)")
    sp.add_argument("--currency", default="")
    sp.add_argument("--timezone", default="UTC", help="IANA 时区，与账单展示口径一致")
    sp.add_argument("--rule-map", default="")
    sp.add_argument("--task-economics", action="store_true")
    sp.set_defaults(func=cmd_close_init)

    sp = close_sub.add_parser("ingest", help="扫描账期包并生成 mapping-draft.json")
    sp.add_argument("case")
    sp.add_argument("--bundle", default="", help="账期包目录（默认同 case）")
    sp.set_defaults(func=cmd_close_ingest)

    sp = close_sub.add_parser("mapping", help="查看或人工确认字段映射")
    sp.add_argument("case")
    sp.add_argument("--confirm", action="store_true",
                    help="记录人工确认；确认后才生成 canonical")
    sp.add_argument("--review-by", default="", help="确认人（attested_by_declaration）")
    sp.add_argument("--control-domain", action="append", default=[],
                    metavar="ROLE=VENDOR|BUYER",
                    help="解决 pending 控制域，可重复")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_close_mapping)

    for name, help_text in (
            ("status", "只读 BuyerCloseCase 投影（四完成轴与待办）"),
            ("show", "同 status：只读 BuyerCloseCase 投影")):
        sp = close_sub.add_parser(name, help=help_text)
        sp.add_argument("case")
        sp.add_argument("--revision", default="", help="指定 revision；默认最新")
        sp.add_argument("--format", choices=("auto", "json", "md"), default="auto",
                        help="输出格式；auto=终端用 md、管道用 json（默认 auto）")
        sp.set_defaults(func=cmd_close_status)

    sp = close_sub.add_parser("calculate", help="对 canonical bundle 运行既有重算链（V-03）")
    sp.add_argument("case")
    sp.add_argument("--json", default="", help="另存一份 calculation JSON")
    sp.set_defaults(func=cmd_close_calculate)

    sp = close_sub.add_parser("report", help="生成六份可签核月结报告（V-03）")
    sp.add_argument("case")
    sp.add_argument("--format", choices=["all", "md", "json"], default="all",
                    help="输出投影提示；V-03 始终写全六份报告对象")
    sp.set_defaults(func=cmd_close_report)

    sp = close_sub.add_parser("review", help="记录署名复核；不关闭未决项（V-03/V-04 门禁）")
    sp.add_argument("case")
    sp.add_argument("--by", default="",
                    help="复核人（自报声明，非强身份；缺失即 INPUT_REQUIRED）")
    sp.add_argument("--scope", default="", help="批准/复核范围")
    sp.add_argument("--reservation", action="append", default=[],
                    help="保留意见，可重复")
    sp.add_argument("--next-review", default="", help="下次复核日期 YYYY-MM-DD")
    sp.set_defaults(func=cmd_close_review)

    sp = close_sub.add_parser("revise", help="从新来源生成独立 revision（V-04）")
    sp.add_argument("case")
    sp.add_argument("--bundle", default="", help="完整的新账期包目录")
    sp.add_argument("--source", default="", help="替换单个来源文件")
    sp.add_argument("--role", default="", help="--source 对应的 role")
    sp.add_argument("--reason", default="", help="修订原因")
    sp.add_argument("--review-by", default="", help="确认新映射的人；缺失即拒绝")
    sp.add_argument("--control-domain", action="append", default=[],
                    metavar="ROLE=VENDOR|BUYER")
    sp.set_defaults(func=cmd_close_revise)

    sp = close_sub.add_parser("signoff", help="对指定 revision 记录署名签核（V-04）")
    sp.add_argument("case")
    sp.add_argument("--revision", default="", help="指定 revision；默认最新")
    sp.add_argument("--by", default="", help="签核人；缺失即 INPUT_REQUIRED")
    sp.add_argument("--scope", default="", help="签核范围")
    sp.add_argument("--reservation", action="append", default=[],
                    help="保留意见，可重复")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_close_signoff)

    sp = close_sub.add_parser(
        "recover", help="检查/完成/回滚中断的 revision 或 signoff 事务（V-06）")
    sp.add_argument("case")
    group = sp.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true",
                       help="完成 journal 记录的事务（默认 dry-run）")
    group.add_argument("--rollback", action="store_true",
                       help="回滚到事务前的一致状态")
    sp.set_defaults(func=cmd_close_recover)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (LedgerError, LLMError, browser.RecipeError, ValueError, OSError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(2)
    return 0


if __name__ == "__main__":
    main()
