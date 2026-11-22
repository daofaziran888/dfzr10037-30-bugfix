"""分类归属与标签审核证据的共享领域规则。

分类体系（taxonomy）和元数据标注（metadata_tag）之间不是简单的“一条分类推一条
标注”：一个标签只有在审核证据成立、归属已经确认、且归属确实发生变化时，才允许
生成可执行的返修。本模块把这条判定收拢到一处，避免两个服务各写一套规则后互相
对不上。

关键概念：

* 标签编号（tag key）：标注任务的稳定身份。历史批次没有该字段时按 ``TAG-<id>``
  推导，保证存量记录仍可查。
* 审核证据（evidence）：标签审核通过时留下的快照，包含原审核标签、原归属分类、
  归属指纹和证据序号。证据只增不改，用来识别“旧证据 / 当前挂接”错位。
* 归属（ownership）：标签当前挂接的分类。只有显式声明（标签的 ``归属分类`` 或
  分类的 ``分类成员``）或审核证据唯一指向的分类才算“已确认”；仅靠实体名互相关联
  只能算候选，禁止自动改挂。
* 归属指纹（fingerprint）：由分类名称和父分类计算，用来判断归属是否真的变了。
  分类编号不参与计算，因此历史批次用“内容标签/标注类型”表达的旧归属，可以和
  同一层级的分类对齐。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from app.store import store

# 审核通过的各种写法：新流程写“已审核”，历史批次里也出现过英文状态。
APPROVED_STATUSES = {"已审核"}
APPROVED_REVIEW_STATES = {"approved", "已审核", "审核通过", "已通过", "reviewed", "pass"}

# 返修来源：真正由分类重构产生的返修、人工发起的返修，以及旧版本错误残留。
SOURCE_RESTRUCTURE = "classification-restructure"
SOURCE_MANUAL = "manual"
SOURCE_RESIDUE = "legacy-residue"

# 返修任务编号前缀。旧版本用 META-TAX- 直接往标注列表塞记录，必须与新返修区分。
REWORK_NO_PREFIX = "META-RWK-"
LEGACY_TAX_PREFIX = "META-TAX-"


def now_text() -> str:
    """返回统一的日期文本；测试和演示环境不依赖时区精细度。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def tag_key(tag: dict[str, Any]) -> str:
    """标签稳定身份。历史批次没有标签编号时按主键推导。"""
    key = text(tag.get("标签编号"))
    if key:
        return key
    try:
        return f"TAG-{int(tag.get('id', 0)):04d}"
    except (TypeError, ValueError):
        return f"TAG-{tag.get('id', 0)}"


def ensure_tag_key(tag: dict[str, Any]) -> str:
    key = tag_key(tag)
    tag["标签编号"] = key
    return key


def taxonomy_no(taxonomy: dict[str, Any]) -> str:
    return text(taxonomy.get("分类编号")) or f"TAXO-{int(taxonomy.get('id', 0)):04d}"


def is_reviewed(tag: dict[str, Any]) -> bool:
    return (
        text(tag.get("status")) in APPROVED_STATUSES
        or text(tag.get("审核状态")).casefold() in APPROVED_REVIEW_STATES
    )


def is_rework_row(tag: dict[str, Any]) -> bool:
    """是否是返修产物，而不是一条原始标注任务。"""
    if text(tag.get("返修来源")):
        return True
    if text(tag.get("返修批次")) or text(tag.get("目标归属指纹")):
        return True
    return text(tag.get("任务编号")).startswith(REWORK_NO_PREFIX)


def is_residue(tag: dict[str, Any]) -> bool:
    """识别旧版本“分类重构”直接写入标注列表的错误残留样本。

    旧实现给每条分类都塞一条 ``META-TAX-*`` 任务，标注人是 system-auto，没有
    审核证据、也没有归属确认。这些记录从未被人审核过，不能进入返修派发选区。

    判据只看旧版本独有的特征（任务编号前缀 + 自动标注人 + 无审核证据），
    不依赖后来补写的标签编号，否则登记流程补一个编号就会漏判。
    """
    source = text(tag.get("返修来源"))
    if source == SOURCE_RESIDUE:
        return True
    if source:
        # 明确声明了其他返修来源，就是新流程产生的真实返修。
        return False
    return (
        text(tag.get("任务编号")).startswith(LEGACY_TAX_PREFIX)
        and text(tag.get("标注人")) == "system-auto"
        and not text(tag.get("审核证据"))
    )


def mark_residue(tag: dict[str, Any], reason: str = "") -> None:
    """把错误残留样本标记出来：保留原始字段，但禁止派发。"""
    if text(tag.get("原审核标签")):
        tag.setdefault("原审核标签", text(tag.get("内容标签")))
    tag["返修来源"] = SOURCE_RESIDUE
    tag["可执行"] = "false"
    tag["abnormal"] = True
    tag["归属确认"] = "待确认"
    tag["派发状态"] = "excluded:legacy-residue"
    if reason:
        tag["残留说明"] = reason


def normalize_legacy_rows() -> int:
    """扫描存量标注记录，给旧版本残留样本打标。

    只改标记字段，不改任务编号、内容标签、标注人、标注时间和审核状态，
    因此原始审核标签与所属分类仍可按存量记录查到。
    """
    marked = 0
    for row in store.rows("metadata_tag"):
        if is_residue(row):
            mark_residue(row, "旧版本分类重构直接写入的返修记录，未经审核")
            marked += 1
    return marked


def classification_fingerprint(name: str, parent: str) -> str:
    """分类归属指纹：由分类名称 + 父分类决定，不含分类编号。"""
    raw = f"{text(name)}\x1f{text(parent)}".encode("utf-8")
    return "fp-" + hashlib.sha1(raw).hexdigest()[:16]


def taxonomy_fingerprint(taxonomy: dict[str, Any]) -> str:
    return classification_fingerprint(
        text(taxonomy.get("分类名称")), text(taxonomy.get("父分类")))


def legacy_fingerprint(tag: dict[str, Any]) -> str:
    """历史审核证据表达的旧归属：内容标签 + 标注类型。"""
    return classification_fingerprint(
        text(tag.get("内容标签")), text(tag.get("标注类型")))


def evidence_fingerprint(tag: dict[str, Any]) -> str:
    """标签最近一次审核证据指向的归属。"""
    stored = text(tag.get("归属指纹"))
    if stored:
        return stored
    # 兼容历史审核记录：没有证据快照时，用原审核标签/标注类型还原。
    label = text(tag.get("原审核标签")) or text(tag.get("内容标签"))
    parent = text(tag.get("原归属父分类")) or text(tag.get("标注类型"))
    return classification_fingerprint(label, parent)


def evidence_seq(tag: dict[str, Any]) -> int:
    try:
        return int(text(tag.get("审核证据序号")) or 0)
    except ValueError:
        return 0


def evidence_id(tag: dict[str, Any]) -> str:
    return text(tag.get("审核证据")) or f"EVID-{tag_key(tag)}-{evidence_seq(tag):03d}"


def _taxonomy_rows() -> list[dict[str, Any]]:
    return store.rows("taxonomy")


def find_taxonomy(no: str) -> dict[str, Any] | None:
    no = text(no)
    if not no:
        return None
    for row in _taxonomy_rows():
        if taxonomy_no(row) == no or text(row.get("分类编号")) == no:
            return row
    return None


def taxonomy_by_identity(name: str, parent: str) -> list[dict[str, Any]]:
    return [
        row for row in _taxonomy_rows()
        if text(row.get("分类名称")) == text(name) and text(row.get("父分类")) == text(parent)
    ]


def member_keys(taxonomy: dict[str, Any]) -> list[str]:
    """分类里显式登记的成员标签编号（分类成员，逗号/顿号分隔）。"""
    raw = text(taxonomy.get("分类成员"))
    if not raw:
        return []
    for sep in ("，", "、", ";", "；"):
        raw = raw.replace(sep, ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def add_member(taxonomy: dict[str, Any], key: str) -> bool:
    key = text(key)
    if not key:
        return False
    keys = member_keys(taxonomy)
    if key in keys:
        return False
    keys.append(key)
    taxonomy["分类成员"] = ",".join(keys)
    return True


def tag_owner_no(tag: dict[str, Any]) -> str:
    """标签当前挂接的分类编号。返修产物本身没有归属，返回空。"""
    if is_rework_row(tag):
        return text(tag.get("目标归属分类")) or text(tag.get("归属分类"))
    return text(tag.get("归属分类"))


def is_confirmed(tag: dict[str, Any]) -> bool:
    """归属是否已经确认。

    只看显式归属、分类成员登记或唯一的分类标识匹配；仅凭实体名关联
    只能算候选，不能据此自动改挂。
    """
    if text(tag.get("归属确认")) in ("已确认", "confirmed", "true"):
        return True
    if text(tag.get("归属确认")) in ("待确认", "unconfirmed", "false"):
        return False
    if tag_owner_no(tag):
        return True
    return bool(matching_taxonomies(tag))


def matching_taxonomies(tag: dict[str, Any]) -> list[dict[str, Any]]:
    """按标签内容（原审核标签 / 标注类型）匹配分类标识。

    兼容历史批次：旧审核记录没有证据快照时，仍然能用“内容标签 + 标注类型”
    对齐同一层级的分类。
    """
    label = text(tag.get("原审核标签")) or text(tag.get("内容标签"))
    parent = text(tag.get("原归属父分类")) or text(tag.get("标注类型"))
    return taxonomy_by_identity(label, parent)


def resolve_legacy_owner(tag: dict[str, Any]) -> tuple[str, str]:
    """按存量记录还原标签的旧归属。

    返回 ``(分类编号, 原因)``。只有唯一命中才算可确认；多处命中视为冲突，
    返回空编号和 ``ambiguous``，防止把标签自动改挂到猜出来的分类上。
    实体关联命中只作为候选（``association-only``），需要人工确认。
    """
    if is_rework_row(tag):
        return "", "rework"
    explicit = text(tag.get("归属分类"))
    if explicit:
        owner = find_taxonomy(explicit)
        if owner is None:
            # 指向一个不存在的分类编号：只能视为待核实的候选，不能自动改挂。
            return "", "dangling-owner"
        return taxonomy_no(owner), "explicit"
    matches = matching_taxonomies(tag)
    if len(matches) == 1:
        return taxonomy_no(matches[0]), "legacy-evidence"
    if len(matches) > 1:
        return "", "ambiguous"
    entity = text(tag.get("标注对象"))
    if entity:
        by_entity = [
            row for row in _taxonomy_rows()
            if text(row.get("关联实体")) == entity
        ]
        if len(by_entity) == 1:
            return "", "association-only"
        if len(by_entity) > 1:
            return "", "ambiguous"
    return "", "unresolved"


def is_confirmed(tag: dict[str, Any]) -> bool:
    """归属是否已经确认。

    只看显式归属（且分类真实存在）或唯一可追溯的分类标识；仅凭实体名关联
    只能算候选，多处命中视为冲突，都不能据此自动改挂。
    """
    flag = text(tag.get("归属确认"))
    if flag in ("已确认", "confirmed", "true"):
        return True
    if flag in ("待确认", "unconfirmed", "false"):
        return False
    owner = text(tag.get("归属分类"))
    if owner:
        return find_taxonomy(owner) is not None
    return len(matching_taxonomies(tag)) == 1


def is_synthetic_class(no: str) -> bool:
    """标注审核自动补建的分类桥（TAXO-TAG-*）不是人工维护的分类节点。"""
    return text(no).startswith("TAXO-TAG-")


def ownership_changed(tag: dict[str, Any], taxonomy: dict[str, Any]) -> bool:
    """判断标签相对目标分类是否真的发生了归属变化。

    - 审核证据的归属指纹与目标分类不一致：真实变化；
    - 证据指向另一个**人工维护**的分类：真实变化（分类被重建归属）；
    - 证据只指向自动补建的 TAXO-TAG-* 桥且指纹相同：视为未变化，避免空转返修。
    """
    target_fp = taxonomy_fingerprint(taxonomy)
    ev_fp = evidence_fingerprint(tag)
    if not ev_fp:
        return True
    if ev_fp != target_fp:
        return True
    ev_owner = text(tag.get("原归属分类")) or text(tag.get("归属分类"))
    tax_no = taxonomy_no(taxonomy)
    if not ev_owner or ev_owner == tax_no:
        return False
    return not is_synthetic_class(ev_owner)



def snapshot_evidence(tag: dict[str, Any], owner: dict[str, Any] | None = None) -> None:
    """审核通过时写入证据快照；序号单调递增，供并发判定使用。

    旧一代证据不会丢：先进 ``审核证据历史`` 再覆盖当前快照，保证原审核标签
    与所属分类仍能按存量记录查到。
    """
    ensure_tag_key(tag)
    previous = {
        "审核证据": text(tag.get("审核证据")),
        "原审核标签": text(tag.get("原审核标签")),
        "原归属分类": text(tag.get("原归属分类")),
        "归属指纹": text(tag.get("归属指纹")),
    }
    if previous["审核证据"]:
        history = tag.setdefault("审核证据历史", [])
        if isinstance(history, list) and previous not in history:
            history.append(previous)
    seq = evidence_seq(tag) + 1
    owner_no = taxonomy_no(owner) if owner is not None else ""
    if owner is not None:
        fp = taxonomy_fingerprint(owner)
        label = text(tag.get("内容标签"))
        parent = text(owner.get("父分类"))
    else:
        fp = evidence_fingerprint(tag)
        label = text(tag.get("原审核标签")) or text(tag.get("内容标签"))
        parent = text(tag.get("原归属父分类")) or text(tag.get("标注类型"))
    tag["审核证据序号"] = str(seq)
    tag["审核证据"] = f"EVID-{tag_key(tag)}-{seq:03d}"
    tag["审核时间"] = text(tag.get("审核时间")) or now_text()
    tag["归属指纹"] = fp
    tag["原审核标签"] = label
    tag["原归属父分类"] = parent
    if owner_no:
        tag["原归属分类"] = owner_no
    tag["审核状态"] = "已审核"


def attach_tag(tag: dict[str, Any], taxonomy: dict[str, Any], *, confirmed: bool) -> None:
    tag["归属分类"] = taxonomy_no(taxonomy)
    tag["归属确认"] = "已确认" if confirmed else "待确认"
    add_member(taxonomy, tag_key(tag))


def members(taxonomy: dict[str, Any]) -> list[dict[str, Any]]:
    """分类成员标签行：显式成员 + 标签显式指向该分类的行。

    旧返修残留和历史返修任务（``返修来源`` 非空）不作为成员。
    """
    no = taxonomy_no(taxonomy)
    keys = set(member_keys(taxonomy))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in store.rows("metadata_tag"):
        if is_rework_row(row) or is_residue(row):
            continue
        key = tag_key(row)
        owner = text(row.get("归属分类"))
        if key in keys or owner == no:
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
    return result


def pending_candidates(taxonomy: dict[str, Any]) -> list[dict[str, Any]]:
    """只有实体关联、没有审核证据或归属确认的候选标签。"""
    entity = text(taxonomy.get("关联实体"))
    no = taxonomy_no(taxonomy)
    result: list[dict[str, Any]] = []
    for row in store.rows("metadata_tag"):
        if is_rework_row(row) or is_residue(row):
            continue
        if not entity or text(row.get("标注对象")) != entity:
            continue
        owner = text(row.get("归属分类"))
        if owner == no:
            continue
        if owner and owner != no:
            # 已经明确挂在别的分类上，属于冲突而不是本分类的待确认候选。
            continue
        if not is_confirmed(row):
            result.append(row)
    return result


def conflict_rows(taxonomy: dict[str, Any]) -> list[dict[str, Any]]:
    """找全与分类成员实体相关的潜在冲突。

    同一实体在多个分类都有标注时，把所有标注都列出来（不管审核状态），
    这样处理人员能看到全量冲突；但后续返修只会归属到当前分类自己的标签。
    """
    no = taxonomy_no(taxonomy)
    entities: list[str] = []
    for tag in members(taxonomy):
        entity = text(tag.get("标注对象"))
        if entity and entity not in entities:
            entities.append(entity)
    related = text(taxonomy.get("关联实体"))
    if related and related not in entities:
        entities.append(related)

    conflicts: list[dict[str, Any]] = []
    for entity in entities:
        if not entity:
            continue
        tags = [
            row for row in store.rows("metadata_tag")
            if not is_rework_row(row) and not is_residue(row)
            and text(row.get("标注对象")) == entity
        ]
        classifications = sorted({
            text(row.get("归属分类")) for row in tags if text(row.get("归属分类"))
        })
        if len(tags) < 2 and len(classifications) < 2:
            continue
        conflicts.append({
            "实体": entity,
            "标签": [
                {
                    "标签编号": tag_key(row),
                    "归属分类": text(row.get("归属分类")),
                    "归属确认": text(row.get("归属确认")) or ("已确认" if is_confirmed(row) else "待确认"),
                    "审核状态": text(row.get("审核状态")) or text(row.get("status")),
                    "已审核": is_reviewed(row),
                    "属于当前分类": text(row.get("归属分类")) == no,
                }
                for row in tags
            ],
            "涉及分类": classifications,
            "当前分类标签数": sum(
                1 for row in tags if text(row.get("归属分类")) == no),
            "其他分类标签数": sum(
                1 for row in tags if text(row.get("归属分类")) not in ("", no)),
        })
    return conflicts


def conflict_classification_nos(taxonomy: dict[str, Any]) -> list[str]:
    nos: list[str] = []
    no = taxonomy_no(taxonomy)
    for conflict in conflict_rows(taxonomy):
        for item in conflict["标签"]:
            item_no = item["归属分类"]
            if item_no and item_no != no and item_no not in nos:
                nos.append(item_no)
    return nos


def open_reworks(tag: dict[str, Any]) -> Iterable[dict[str, Any]]:
    key = tag_key(tag)
    for row in store.rows("metadata_tag"):
        if not is_rework_row(row) or is_residue(row):
            continue
        if text(row.get("标签编号")) != key:
            continue
        if text(row.get("可执行")) != "true":
            continue
        yield row


def rework_exists(tag_key_value: str, target_fp: str, owner_no: str) -> bool:
    for row in store.rows("metadata_tag"):
        if not is_rework_row(row) or is_residue(row):
            continue
        if text(row.get("标签编号")) != tag_key_value:
            continue
        if text(row.get("目标归属指纹")) != target_fp:
            continue
        if text(row.get("目标归属分类")) not in ("", owner_no):
            continue
        if text(row.get("可执行")) == "true":
            return True
    return False


def close_superseded_reworks(
    tag: dict[str, Any],
    current_fp: str,
    *,
    owner_no: str = "",
) -> int:
    """标签重新审核后，关闭目标归属已经追平的返修。"""
    closed = 0
    for row in open_reworks(tag):
        if owner_no and text(row.get("目标归属分类")) not in ("", owner_no):
            continue
        if text(row.get("目标归属指纹")) != current_fp:
            continue
        row["可执行"] = "false"
        row["返修状态"] = "superseded"
        row["pending"] = False
        row["abnormal"] = False
        row["status"] = "已审核"
        row["关闭原因"] = "标签审核已追平当前归属"
        row["关闭时间"] = now_text()
        closed += 1
    return closed


def next_rework_no() -> str:
    rows = store.rows("metadata_tag")
    next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    return f"{REWORK_NO_PREFIX}{next_id:04d}"


def dispatch_reason(tag: dict[str, Any], *, owner_no: str = "") -> str:
    """派发选区判定：返回空字符串表示应派发，否则返回排除原因。"""
    if is_residue(tag):
        return "legacy-residue"
    if not is_rework_row(tag):
        return "not-rework"
    if text(tag.get("可执行")) != "true":
        return "not-actionable"
    if text(tag.get("归属确认")) != "已确认":
        return "ownership-unconfirmed"
    target_no = text(tag.get("目标归属分类")) or text(tag.get("归属分类"))
    if not target_no:
        return "missing-owner"
    if owner_no and target_no != owner_no:
        return "other-classification"
    target_fp = text(tag.get("目标归属指纹"))
    based_seq = text(tag.get("基于证据序号"))
    key = text(tag.get("标签编号"))
    for row in store.rows("metadata_tag"):
        if is_rework_row(row) or text(tag_key(row)) != key:
            continue
        # 源标签已按新证据追平目标归属（并发出现在标签侧），返修无需再派发。
        if target_fp and text(row.get("归属指纹")) == target_fp:
            return "ownership-caught-up"
        # 源标签在返修生成后又被审核到了别处：旧返修的证据已过期，
        # 不能再按旧归属派发，避免领错别的分类的返修。
        if based_seq and evidence_seq(row) > int(based_seq or 0) \
                and text(row.get("归属指纹")) != target_fp:
            return "evidence-superseded"
    return ""
