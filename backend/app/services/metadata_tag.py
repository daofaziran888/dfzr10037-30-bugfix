"""元数据标注领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services import tag_ownership as own
from app.services.base import BaseModuleService
from app.store import store


class MetadataTagService(BaseModuleService):
    MODULE = "metadata_tag"
    ENTITY = "标注任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "标注对象", "标注类型", "标注人", "标注批次", "内容标签", "审核状态", "标注时间"]
    REQUIRED_FIELDS = ["任务编号", "标注对象", "标注类型"]
    STATUS_ORDER = ["待分配", "标注中", "已审核", "返修中"]
    ACTION_RULES = {"分配任务": "标注中", "提交审核": "已审核", "发起返修": "返修中"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message

        # 返修任务被审核时，先核对归属是否仍指向本任务的目标分类。
        if own.is_rework_row(entry):
            action_msg = self._advance_rework(entry, action)
            if action_msg:
                message += "；" + action_msg
                return entry, message

        # 链路 L18: metadata_tag → taxonomy（标注审核通过后更新分类体系）
        if entry.get("status") == "已审核" and not own.is_rework_row(entry):
            tax_msg = self._push_to_taxonomy(entry)
            message += "；" + tax_msg
        # 链路 L19: metadata_tag → govern_dash（标注完成更新治理指标）
        if entry.get("status") == "已审核":
            dash_msg = self._push_to_govern_dash(entry)
            message += "；" + dash_msg
        return entry, message

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        # 先判旧残留：旧版本的 META-TAX-* 记录不能被补成“可派发的新标签”。
        if own.is_residue(entry):
            own.mark_residue(entry, "旧版本分类重构直接写入，未经审核")
            return entry, []
        own.ensure_tag_key(entry)
        # 登记时按存量记录还原旧归属；唯一命中才确认，避免登记即自动改挂。
        owner_no, reason = own.resolve_legacy_owner(entry)
        if owner_no:
            entry["归属分类"] = owner_no
            entry["归属确认"] = "已确认"
            owner = own.find_taxonomy(owner_no)
            if owner is not None:
                own.add_member(owner, own.tag_key(entry))
        else:
            entry["归属确认"] = "待确认"
            entry["归属来源"] = reason
        if own.is_reviewed(entry):
            owner = own.find_taxonomy(entry.get("归属分类", ""))
            own.snapshot_evidence(entry, owner)
        return entry, []

    def _advance_rework(self, entry: dict[str, Any], action: str) -> str:
        """返修任务的审核不能凭空确认归属。"""
        if action != "提交审核":
            return ""
        target_no = own.text(entry.get("目标归属分类"))
        target_fp = own.text(entry.get("目标归属指纹"))
        owner = own.find_taxonomy(target_no)
        if owner is None:
            entry["可执行"] = "false"
            entry["返修状态"] = "blocked"
            entry["abnormal"] = True
            entry["归属确认"] = "待确认"
            entry["审核状态"] = "blocked"
            entry["close_reason"] = "目标分类不存在"
            return "返修未生效：目标归属分类不存在，保留原审核标签与原分类"
        if own.taxonomy_fingerprint(owner) != target_fp:
            entry["可执行"] = "false"
            entry["返修状态"] = "blocked"
            entry["abnormal"] = True
            entry["归属确认"] = "待确认"
            entry["审核状态"] = "blocked"
            entry["close_reason"] = "目标归属已再次变化"
            return "返修未生效：目标归属已变化，保留原审核标签与原分类"

        # 回写源标签的归属，并留下新的审核证据（序号递增，供并发判定）。
        key = own.text(entry.get("标签编号"))
        for tag in store.rows("metadata_tag"):
            if own.is_rework_row(tag) or own.tag_key(tag) != key:
                continue
            tag["归属分类"] = target_no
            tag["归属确认"] = "已确认"
            own.snapshot_evidence(tag, owner)
            own.add_member(owner, key)
            entry["原审核标签"] = own.text(entry.get("原审核标签")) or own.text(tag.get("内容标签"))
            break
        entry["归属确认"] = "已确认"
        entry["返修状态"] = "done"
        entry["可执行"] = "false"
        entry["审核状态"] = "approved"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["status"] = "已审核"
        entry["关闭原因"] = "返修已审核并回写归属"
        entry["完成时间"] = own.now_text()
        return "返修已审核并回写归属"

    def pending_dispatch(
        self,
        *,
        owner_no: str = "",
        batch: str = "",
    ) -> list[dict[str, Any]]:
        """待办同步选区：只收录真正应派发的按归属返修。

        - 排除错误残留的历史 ``META-TAX-*`` 样本；
        - 排除归属未确认 / 不可执行 / 目标分类与本次选区不符的返修；
        - 排除源标签已经追平目标归属的返修（并发审核产生的空转）；
        - ``batch`` 兼容历史批次：``legacy`` 只选存量（无批次或标为 legacy）的返修，
          传具体批次号按批次精确匹配，留空则返回全部可派发返修。
        """
        rows = store.rows("metadata_tag")
        for row in rows:
            if own.is_residue(row):
                continue
            if not own.is_rework_row(row) or own.text(row.get("可执行")) != "true":
                continue
            reason = own.dispatch_reason(row, owner_no=owner_no)
            if reason:
                row["派发状态"] = f"excluded:{reason}"
                continue
            row["派发状态"] = "ready"
        selected: list[dict[str, Any]] = []
        for row in rows:
            if not own.is_rework_row(row) or own.is_residue(row):
                continue
            if own.text(row.get("可执行")) != "true":
                continue
            if own.dispatch_reason(row, owner_no=owner_no):
                continue
            row_batch = own.text(row.get("返修批次"))
            if batch:
                if batch in ("legacy", "历史"):
                    if row_batch and row_batch != "legacy":
                        continue
                elif row_batch != batch:
                    continue
            selected.append(row)
        return selected

    def _push_to_taxonomy(self, entry: dict[str, Any]) -> str:
        """审核通过后更新分类体系：复用同一层级的分类，避免重复建桥。"""
        label = str(entry.get("内容标签", "default-tag"))
        parent = str(entry.get("标注类型", "root"))
        matches = own.taxonomy_by_identity(label, parent)
        if matches:
            tax_entry = matches[0]
            own.add_member(tax_entry, own.tag_key(entry))
            own.attach_tag(entry, tax_entry, confirmed=True)
            own.snapshot_evidence(entry, tax_entry)
            closed = own.close_superseded_reworks(
                entry, own.taxonomy_fingerprint(tax_entry))
            message = f"已更新分类体系 {own.taxonomy_no(tax_entry)}"
            if closed:
                message += f"（关闭已追平返修 {closed} 条）"
            return message

        rows = store.rows("taxonomy")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        tax_entry = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "分类编号": f"TAXO-TAG-{next_id:04d}",
            "分类名称": label,
            "父分类": parent,
            "层级深度": "2",
            "关联实体": str(entry.get("标注对象", "")),
            "分类规则": f"from_tag_{entry.get('标注批次','')}",
            "自动归类": "true",
            "最后更新": str(entry.get("标注时间", "2026-11-18")),
            "分类成员": own.tag_key(entry),
        }
        rows.append(tax_entry)
        own.attach_tag(entry, tax_entry, confirmed=True)
        own.snapshot_evidence(entry, tax_entry)
        closed = own.close_superseded_reworks(entry, own.taxonomy_fingerprint(tax_entry))
        message = f"已更新分类体系 {tax_entry['分类编号']}"
        if closed:
            message += f"（关闭已追平返修 {closed} 条）"
        return message

    def _push_to_govern_dash(self, entry: dict[str, Any]) -> str:
        rows = store.rows("govern_dash")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        gd_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "指标编号": f"GOVE-TAG-{next_id:04d}",
            "指标名称": f"标注质量-{entry.get('标注类型','')}",
            "评估对象": str(entry.get("标注对象", "")),
            "目标值": "0.95",
            "当前值": "0.92",
            "趋势方向": "stable",
            "更新频率": "on-tag-approved",
            "最后刷新": str(entry.get("标注时间", "2026-11-18")),
        }
        rows.append(gd_entry)
        return f"已推送治理看板指标 {gd_entry['指标编号']}"

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        dispatch: bool = False,
        batch: str | None = None,
        owner: str | None = None,
        include_residue: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """列表默认不混入历史残留样本，避免“从未审核的返修”污染工作台。

        需要追溯旧样本时显式传 ``include_residue``，残留行仍带
        ``返修来源=legacy-residue`` 与原始审核字段。
        """
        own.normalize_legacy_rows()
        if dispatch:
            # 待办同步选区：只给真正应派发的按归属返修。
            rows = self.pending_dispatch(owner_no=owner or "", batch=batch or "")
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = (page - 1) * size
            return rows[start:start + size], total
        rows = [
            row for row in store.rows(self.MODULE)
            if include_residue or not own.is_residue(row)
        ]
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["taxonomy", "govern_dash"]
        base["pending_dispatch"] = len(self.pending_dispatch())
        base["legacy_residue"] = sum(
            1 for row in store.rows(self.MODULE) if own.is_residue(row))
        return base
