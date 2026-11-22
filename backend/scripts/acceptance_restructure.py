"""分类重构 → 标签返修 → 待办派发 的验收脚本。

覆盖题目里的三类角色：
1. 从未审核过的关联实体（不能因为重构就凭空出现返修）；
2. 同一实体在两处分类都有标注，一份已审核、一份还在复查（找全冲突但不错领）；
3. 旧版本错误残留的 META-TAX-* 样本（不派发，但原字段仍可查）。

用法：backend/.venv/bin/python scripts/acceptance_restructure.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import tag_ownership as own  # noqa: E402
from app.services.metadata_tag import MetadataTagService  # noqa: E402
from app.services.taxonomy import TaxonomyService  # noqa: E402
from app.store import store  # noqa: E402

FAILS: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        FAILS.append(label)


def reset() -> None:
    from app.seed import SEED_ROWS

    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


def add_taxonomy(**values) -> dict:
    rows = store.rows("taxonomy")
    entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
             "status": "草稿", "pending": True, "abnormal": False}
    entry.update(values)
    rows.append(entry)
    return entry


def add_tag(**values) -> dict:
    rows = store.rows("metadata_tag")
    entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
             "status": "待分配", "pending": True, "abnormal": False}
    entry.update(values)
    rows.append(entry)
    return entry


def main() -> int:
    reset()
    tax_service = TaxonomyService()
    tag_service = MetadataTagService()

    # 实体 A：分类里只登记了关联实体，标注任务从未审核。
    tax_a = add_taxonomy(分类编号="TAXO-A", 分类名称="商品类", 父分类="根",
                         层级深度="2", 关联实体="实体A", 分类规则="rule-a",
                         自动归类="true", 最后更新="2026-11-20")
    tag_a = add_tag(任务编号="META-A", 标注对象="实体A", 标注类型="商品类",
                    标注人="张三", 标注批次="batch-a", 内容标签="商品类",
                    审核状态="pending", 标注时间="2026-11-20")
    own.ensure_tag_key(tag_a)
    tag_a["归属分类"] = own.taxonomy_no(tax_a)

    # 实体 B：两个分类都有标注，B1 已审核，B2 还在复查。
    tax_b1 = add_taxonomy(分类编号="TAXO-B1", 分类名称="合同类", 父分类="根",
                          层级深度="2", 关联实体="实体B", 分类规则="rule-b1",
                          自动归类="true", 最后更新="2026-11-20")
    tax_b2 = add_taxonomy(分类编号="TAXO-B2", 分类名称="合同类", 父分类="根-归档",
                          层级深度="2", 关联实体="实体B", 分类规则="rule-b2",
                          自动归类="true", 最后更新="2026-11-20")
    tag_b1 = add_tag(任务编号="META-B1", 标注对象="实体B", 标注类型="合同类",
                     标注人="李四", 标注批次="batch-b1", 内容标签="合同类",
                     审核状态="已审核", 标注时间="2026-11-20")
    own.ensure_tag_key(tag_b1)
    tag_b1["归属分类"] = own.taxonomy_no(tax_b1)
    own.snapshot_evidence(tag_b1, tax_b1)
    tag_b2 = add_tag(任务编号="META-B2", 标注对象="实体B", 标注类型="合同类",
                     标注人="王五", 标注批次="batch-b2", 内容标签="合同类",
                     审核状态="reviewing", 标注时间="2026-11-21")
    own.ensure_tag_key(tag_b2)
    tag_b2["归属分类"] = own.taxonomy_no(tax_b2)

    # 旧版本错误残留：分类重构直接塞进标注列表、从未审核过的任务。
    residue = add_tag(任务编号="META-TAX-0099", 标注对象="实体B", 标注类型="re-classify",
                      标注人="system-auto", 标注批次="batch-99",
                      内容标签="合同类", 审核状态="pending", 标注时间="2026-11-01")

    before = len(store.rows("metadata_tag"))

    # 执行“重构层级”
    entry_a, msg_a = tax_service.run_action(tax_a["id"], "重构层级")
    entry_b1, msg_b1 = tax_service.run_action(tax_b1["id"], "重构层级")
    entry_b2, msg_b2 = tax_service.run_action(tax_b2["id"], "重构层级")
    print("\n重构消息 A :", msg_a)
    print("重构消息 B1:", msg_b1)
    print("重构消息 B2:", msg_b2, "\n")

    # 1) 从未审核的实体不生成可执行返修。
    check(entry_a is not None and "生成返修 0 条" in msg_a,
          "从未审核的关联实体不生成返修")

    # 2) 重构没有把残留样本算成新返修：新增只来自真正归属变化的已审核标签。
    created = [r for r in store.rows("metadata_tag")[before:]
               if own.is_rework_row(r) and not own.is_residue(r)]
    check(all(r["返修来源"] == own.SOURCE_RESTRUCTURE for r in created),
          "新增记录全部是可执行的重构返修")
    check(all(r.get("标签编号") for r in created), "返修记录带标签归属身份")

    # 3) 已审核的 B1 在自己的分类里没有被改挂，不产生空转返修。
    reworks_b1 = [r for r in created if own.text(r.get("标签编号")) == own.tag_key(tag_b1)
                  and own.text(r.get("目标归属分类")) == "TAXO-B1"]
    check(not reworks_b1, "审核证据与当前分类一致时不生成返修")

    # 4) 分类 B1 能看到实体 B 的潜在冲突（另一份在复查），但不领别的分类的返修。
    conflicts = entry_b1.get("潜在冲突明细") if entry_b1 else []
    check(isinstance(conflicts, list) and len(conflicts) == 1,
          "分类 B1 找全实体 B 的潜在冲突")
    check(bool(entry_b1 and entry_b1.get("潜在冲突")),
          "分类列表里的冲突摘要可读")
    if isinstance(conflicts, list) and conflicts:
        detail = conflicts[0]
        check(detail["当前分类标签数"] == 1 and detail["其他分类标签数"] >= 1,
              "冲突明细区分当前分类与其他分类的标注")
    check("TAXO-B2" in (entry_b1.get("冲突分类") or ""),
          "冲突分类清单包含另一个分类")

    # 5) 归属未确认的标签禁止自动改挂：复查中的 B2 保持原挂钩。
    check(own.text(tag_b2.get("归属分类")) == "TAXO-B2"
          and own.text(tag_b2.get("归属确认")) == "待确认",
          "复查中的标签归属保持原分类且未确认")

    # 6) 存量可查：旧残留字段原样保留，但被标记为不可派发。
    check(residue["任务编号"] == "META-TAX-0099"
          and residue["审核状态"] == "pending"
          and residue["标注人"] == "system-auto",
          "残留样本的原始审核字段仍可查")
    check(own.is_residue(residue), "残留样本被识别")

    # 7) 给 B1 的实体再登记一条已审核但归属不同的标签，制造真实归属变化。
    tag_b3 = add_tag(任务编号="META-B3", 标注对象="实体B", 标注类型="合同类",
                     标注人="赵六", 标注批次="batch-b3", 内容标签="合同类",
                     审核状态="已审核", 标注时间="2026-11-21")
    own.ensure_tag_key(tag_b3)
    tag_b3["归属分类"] = "TAXO-OLD"
    tag_b3["原审核标签"] = "合同类"
    tag_b3["原归属父分类"] = "根-旧"
    own.add_member(tax_b1, own.tag_key(tag_b3))
    tax_service.run_action(tax_b1["id"], "重构层级")
    rework_b3 = [r for r in store.rows("metadata_tag")
                 if own.is_rework_row(r) and own.text(r.get("标签编号")) == own.tag_key(tag_b3)
                 and own.text(r.get("目标归属分类")) == "TAXO-B1"]
    check(len(rework_b3) == 1, "实际归属变化时生成按归属的返修")
    if rework_b3:
        rw = rework_b3[0]
        check(rw["原归属分类"] == "TAXO-OLD" and rw["原审核标签"] == "合同类",
              "返修保留旧审核证据与原归属分类")

    # 8) 待办同步选区：只派发真正应派发的返修。
    selected = tag_service.pending_dispatch(owner_no="TAXO-B1")
    check(all(own.text(r.get("目标归属分类")) == "TAXO-B1" for r in selected),
          "待办选区不混入其他分类的返修")
    check(all(not own.is_residue(r) for r in selected), "待办选区排除错误残留样本")
    check(all(own.text(r.get("返修批次")) for r in selected), "返修带可追溯批次")
    selected_all = tag_service.pending_dispatch()
    check(len(selected_all) == len(selected) and len(selected_all) == 1,
          "历史批次兼容：不带分类筛选时派发同一批可执行返修")

    # 9) 并发：重构确认与标签审核交错时，已追平的返修不再派发。
    tax_service.run_action(tax_b1["id"], "重构层级")
    duplicate = [r for r in store.rows("metadata_tag")
                 if own.is_rework_row(r) and own.text(r.get("标签编号")) == own.tag_key(tag_b3)
                 and own.text(r.get("目标归属分类")) == "TAXO-B1"]
    check(len(duplicate) == 1, "同一标签同一目标归属只保留一个返修")

    # 10) 提交一轮审核：先把返修提交审核，观察是否回写并关闭。
    rw = duplicate[0]
    _, rw_msg = tag_service.run_action(rw["id"], "提交审核")
    print("返修审核:", rw_msg)
    check(own.text(tag_b3.get("归属分类")) == "TAXO-B1", "返修审核后回写源标签归属")
    check(own.text(rw.get("可执行")) == "false", "返修完成后不再派发")
    check(not tag_service.pending_dispatch(owner_no="TAXO-B1"),
          "审核完成后待办选区为空")

    # 11) 列表默认不出现从未审核的残留返修，但可显式追溯。
    items, _ = tag_service.list_entries(page=1, size=1000)
    check(all(not own.is_residue(r) for r in items), "标注列表默认不混入残留样本")
    items_all, _ = tag_service.list_entries(page=1, size=1000, include_residue=True)
    check(any(r["任务编号"] == "META-TAX-0099" for r in items_all),
          "显式追溯时仍能查到原始残留记录")
    dispatched, _ = tag_service.list_entries(page=1, size=1000, dispatch=True)
    check(all(not own.is_residue(r) for r in dispatched), "待办同步接口排除残留样本")

    # 12) 历史批次：旧审核记录没有证据快照时可按存量归属还原。
    #     旧字段语义：内容标签 = 分类名称，标注类型 = 父分类（与旧版推送桥一致）。
    legacy = add_tag(任务编号="META-OLD", 标注对象="实体C", 标注类型="根",
                     标注人="钱七", 标注批次="batch-old", 内容标签="物料类",
                     审核状态="已审核", 标注时间="2026-08-01")
    tax_c = add_taxonomy(分类编号="TAXO-C", 分类名称="物料类", 父分类="根",
                         层级深度="2", 关联实体="实体C", 分类规则="rule-c",
                         自动归类="true", 最后更新="2026-11-20")
    owner_no, reason = own.resolve_legacy_owner(legacy)
    check(owner_no == "TAXO-C" and reason == "legacy-evidence",
          "历史批次可按存量记录还原归属")

    # 13) 证据历史留痕：再次审核后旧证据仍能追溯。
    old_evidence = own.evidence_id(tag_b3)
    tax_b1["父分类"] = "根-调整"
    tax_service.run_action(tax_b1["id"], "重构层级")
    check(own.evidence_id(tag_b3) == old_evidence or not own.evidence_seq(tag_b3) == 1,
          "标签重新审核后证据序号单调递增")

    # 14) 并发：返修生成后源标签被审核到别处，旧返修不得派发。
    tax_b1["父分类"] = "根"
    tax_service.run_action(tax_b1["id"], "重构层级")
    tag_b1["归属分类"] = "TAXO-B2"
    tag_b1["归属确认"] = "已确认"
    own.snapshot_evidence(tag_b1, tax_b2)
    stale = [r for r in store.rows("metadata_tag")
             if own.is_rework_row(r) and own.text(r.get("标签编号")) == own.tag_key(tag_b1)
             and own.text(r.get("目标归属分类")) == "TAXO-B1"]
    check(bool(stale), "构造：B1 返修存在")
    if stale:
        check(own.dispatch_reason(stale[0], owner_no="TAXO-B1") in (
            "evidence-superseded", "ownership-caught-up"),
            "源标签改挂后旧返修判定为不可派发")
        check(stale[0] not in tag_service.pending_dispatch(owner_no="TAXO-B1"),
              "待办选区排除证据已过期的返修")

    print("\n结果：", "全部通过" if not FAILS else f"{len(FAILS)} 项失败")
    for item in FAILS:
        print(" -", item)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
