"""分类体系领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services import tag_ownership as own
from app.services.base import BaseModuleService
from app.store import store


class TaxonomyService(BaseModuleService):
    MODULE = "taxonomy"
    ENTITY = "分类树"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["分类编号", "分类名称", "父分类", "层级深度", "关联实体", "分类规则", "自动归类", "最后更新"]
    REQUIRED_FIELDS = ["分类编号", "分类名称", "父分类"]
    STATUS_ORDER = ["草稿", "已发布", "重构中", "已合并"]
    ACTION_RULES = {"发布分类": "已发布", "重构层级": "重构中", "合并分类": "已合并"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 链路 L20: taxonomy → govern_dash（分类发布/重构后触发治理看板更新）
        if entry.get("status") in ("已发布", "重构中"):
            dash_msg = self._push_to_govern_dash(entry)
            message += "；" + dash_msg
        # 链路 L21: taxonomy → metadata_tag（仅按标签归属生成可执行返修）
        if entry.get("status") in ("重构中", "已合并"):
            tag_msg, summary = self.sync_members(entry)
            entry["分类成员"] = ",".join(summary["成员"])
            # 结构化冲突明细供核对；列表里用一行摘要展示。
            entry["潜在冲突"] = "；".join(
                f"{item['实体']}（当前分类 {item['当前分类标签数']} 条 / 共 {len(item['标签'])} 条）"
                for item in summary["冲突"]
            ) or "无"
            entry["潜在冲突明细"] = summary["冲突"]
            entry["冲突分类"] = ",".join(summary["冲突分类"])
            entry["待确认标签"] = ",".join(summary["待确认"])
            entry["未审核标签"] = ",".join(summary["未审核"])
            entry["重构返修数"] = str(len(summary["返修"]))
            entry["最近重构"] = own.now_text()
            entry["重构批次"] = summary["批次"]
            message += "；" + tag_msg
        return entry, message

    def sync_members(self, entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """把分类成员与标签审核证据对齐，并按归属生成返修。

        规则：
        1. 只有显式成员（分类成员 / 标签归属分类）才参与重构；
        2. 从未审核过的标签不生成返修，只登记为待确认；
        3. 旧审核证据与当前分类归属一致时不生成返修；
        4. 归属变化时生成按归属的返修任务，保留原审核标签与原归属分类；
        5. 同一标签同一目标归属只保留一个可执行返修。
        """
        own.ensure_tag_key(entry)
        tax_no = own.taxonomy_no(entry)
        target_fp = own.taxonomy_fingerprint(entry)
        batch = f"TAXO-REWORK-{tax_no}"
        member_tags = own.members(entry)
        summary: dict[str, Any] = {
            "批次": batch,
            "成员": [],
            "返修": [],
            "未审核": [],
            "待确认": [],
            "冲突": own.conflict_rows(entry),
            "冲突分类": own.conflict_classification_nos(entry),
            "关闭": 0,
        }

        for tag in member_tags:
            key = own.ensure_tag_key(tag)
            summary["成员"].append(key)
            own.add_member(entry, key)
            reviewed = own.is_reviewed(tag)
            # 显式成员登记即为已确认归属；但显式标注为待确认的保持待确认。
            explicit_negative = own.text(tag.get("归属确认")) in (
                "待确认", "unconfirmed", "false")
            if reviewed:
                if not explicit_negative:
                    tag["归属确认"] = "已确认"
            elif not own.text(tag.get("归属确认")):
                # 从未审核过的成员不能因为“被分类收进来”就变成已确认。
                tag["归属确认"] = "待确认"
            if not own.text(tag.get("原审核标签")) and reviewed:
                # 历史审核记录：补一份只读证据快照，不覆盖任何原始字段。
                tag["原审核标签"] = own.text(tag.get("内容标签"))
                tag["原归属父分类"] = own.text(tag.get("标注类型"))
                tag["原归属分类"] = own.text(tag.get("归属分类"))

            if not reviewed:
                summary["未审核"].append(key)
                summary["待确认"].append(key)
                continue

            if not own.ownership_changed(tag, entry):
                summary["关闭"] += own.close_superseded_reworks(
                    tag, target_fp, owner_no=tax_no)
                continue
            if own.rework_exists(key, target_fp, tax_no):
                continue

            rework = self._create_rework(entry, tag, target_fp, batch)
            summary["返修"].append(rework["任务编号"])

        # 只有实体关联、没有审核证据的标签：只登记候选，禁止自动改挂。
        pending_seen = set(summary["待确认"])
        for tag in own.pending_candidates(entry):
            key = own.ensure_tag_key(tag)
            if key in summary["成员"] or key in pending_seen:
                continue
            summary["待确认"].append(key)
            pending_seen.add(key)
            if not own.text(tag.get("归属确认")):
                tag["归属确认"] = "待确认"

        message_parts = [
            f"重构成员 {len(summary['成员'])} 个",
            f"生成返修 {len(summary['返修'])} 条",
        ]
        if summary["未审核"]:
            message_parts.append(f"未审核不派发 {len(summary['未审核'])} 条")
        if summary["待确认"]:
            message_parts.append(f"归属待确认 {len(summary['待确认'])} 条")
        if summary["冲突"]:
            message_parts.append(f"潜在冲突 {len(summary['冲突'])} 处")
        return "已同步分类成员与审核证据：" + "，".join(message_parts), summary

    def _create_rework(
        self,
        taxonomy: dict[str, Any],
        tag: dict[str, Any],
        target_fp: str,
        batch: str,
    ) -> dict[str, Any]:
        rows = store.rows("metadata_tag")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        key = own.tag_key(tag)
        old_fp = own.evidence_fingerprint(tag)
        rework: dict[str, Any] = {
            "id": next_id,
            "status": "待分配",
            "pending": True,
            "abnormal": False,
            "任务编号": f"{own.REWORK_NO_PREFIX}{next_id:04d}",
            "标注对象": own.text(tag.get("标注对象")),
            "标注类型": "re-classify",
            "标注人": "system-rework",
            "标注批次": batch,
            "内容标签": own.text(tag.get("内容标签")),
            "审核状态": "pending",
            "标注时间": own.now_text(),
            # 按归属的派发字段
            "标签编号": key,
            "返修来源": own.SOURCE_RESTRUCTURE,
            "返修批次": batch,
            "返修状态": "pending",
            "可执行": "true",
            "归属分类": own.taxonomy_no(taxonomy),
            "归属确认": "已确认",
            "目标归属分类": own.taxonomy_no(taxonomy),
            "目标归属指纹": target_fp,
            "关联分类": own.taxonomy_no(taxonomy),
            # 旧审核证据原样保留，供存量记录追溯
            "原审核证据": own.evidence_id(tag),
            "原审核标签": own.text(tag.get("原审核标签")) or own.text(tag.get("内容标签")),
            "原归属分类": own.text(tag.get("原归属分类")) or own.text(tag.get("归属分类")),
            "原归属指纹": old_fp,
            "基于证据序号": str(own.evidence_seq(tag)),
        }
        rows.append(rework)
        tag["最后返修任务"] = rework["任务编号"]
        return rework

    def _push_to_govern_dash(self, entry: dict[str, Any]) -> str:
        rows = store.rows("govern_dash")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        gd_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "指标编号": f"GOVE-TAX-{next_id:04d}",
            "指标名称": f"分类覆盖-{entry.get('分类名称','')}",
            "评估对象": str(entry.get("分类编号", "")),
            "目标值": "0.90",
            "当前值": str(entry.get("层级深度", "0")),
            "趋势方向": "stable",
            "更新频率": "on-classification-change",
            "最后刷新": str(entry.get("最后更新", "2026-11-18")),
        }
        rows.append(gd_entry)
        return f"治理指标同步: {gd_entry['指标编号']}"

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """详情附带成员、冲突和待确认标签，供“分类工作台”核对原标注归属。"""
        entry = super().get_entry(entry_id)
        if entry is None:
            return None
        entry["分类成员清单"] = [
            {
                "标签编号": own.tag_key(tag),
                "标注对象": own.text(tag.get("标注对象")),
                "归属分类": own.text(tag.get("归属分类")),
                "归属确认": own.text(tag.get("归属确认")) or (
                    "已确认" if own.is_confirmed(tag) else "待确认"),
                "审核状态": own.text(tag.get("审核状态")) or own.text(tag.get("status")),
                "已审核": own.is_reviewed(tag),
                "原审核标签": own.text(tag.get("原审核标签")),
                "原归属分类": own.text(tag.get("原归属分类")),
            }
            for tag in own.members(entry)
        ]
        entry["潜在冲突"] = "；".join(
            f"{item['实体']}（当前分类 {item['当前分类标签数']} 条 / 共 {len(item['标签'])} 条）"
            for item in own.conflict_rows(entry)
        ) or "无"
        entry["潜在冲突明细"] = own.conflict_rows(entry)
        entry["冲突分类"] = own.conflict_classification_nos(entry)
        entry["待确认标签"] = [
            own.tag_key(tag) for tag in own.pending_candidates(entry)
        ]
        return entry

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["govern_dash", "metadata_tag"]
        return base
