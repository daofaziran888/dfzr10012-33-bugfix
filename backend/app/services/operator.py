"""流处理算子业务规则：状态流转、字段校验与函数引用消费都收在这里。

算子发布时会同步读取「函数引用数据结果」，按 ``记录编号`` 关联消费：
只采用每个被引用函数最新且已确认的那条结果，命中已停用的函数直接拒绝发布，
避免已停用函数被静默带入新的执行链路。消费过的记录编号会留在算子上，
历史引用只追加不覆盖，便于追溯。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.udf import (
    CONFIRMED,
    REF_MODULE,
    STATUS_DELETED,
    STATUS_DISABLED,
    UdfService,
    parse_references,
)
from app.store import TransactionAbort, store

MODULE = "operator"
REQUIRED_FIELDS = ["算子编号", "算子名称", "输入流"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已下线"]
STATUS_DRAFT, STATUS_RUNNING, STATUS_PAUSED, STATUS_OFFLINE = STATUS_ORDER
ACTION_RULES = {"启动算子": "草稿", "暂停算子": "运行中", "下线算子": "已暂停"}
PUBLISH_ACTION = "发布算子"
NEGATIVE_ACTIONS = ["下线算子"]
PUBLISHED, UNPUBLISHED = "已发布", "未发布"


class OperatorService:
    def __init__(self) -> None:
        self.udf = UdfService()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("算子编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["引用函数"] = parse_references(values.get("引用函数"))
        entry["引用版本"] = 0
        entry["引用记录"] = []
        entry["引用历史"] = []
        entry["发布状态"] = UNPUBLISHED
        rows.append(entry)
        return entry, []

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        if action == PUBLISH_ACTION:
            return self._publish(entry_id, values)
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流处理算子可执行范围"
        try:
            with store.transaction(MODULE, REF_MODULE, "udf"):
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    raise TransactionAbort(f"算子定义 {entry_id} 不存在或已归档")
                target = ACTION_RULES[action]
                if target not in STATUS_ORDER:
                    raise TransactionAbort(f"目标状态「{target}」不在允许的状态序列里")
                if action == "启动算子" and parse_references(entry.get("引用函数")):
                    if entry.get("发布状态") != PUBLISHED:
                        raise TransactionAbort(
                            f"算子 {entry.get('算子编号')} 引用了自定义函数，需先执行发布算子"
                            "完成引用校验后才能启动"
                        )
                    # 已发布算子再次启动时复核引用结果，停用后的函数不能继续被消费。
                    _, _, error = self._check_published_references(entry)
                    if error:
                        raise TransactionAbort(error)
                entry["status"] = target
                entry["算子状态"] = target
                entry["pending"] = target != STATUS_OFFLINE
                entry["abnormal"] = action in NEGATIVE_ACTIONS
                return entry, f"算子定义已{action}"
        except TransactionAbort as abort:
            return None, abort.message

    def preview_publish(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """只读预览发布校验：展示将要消费的引用结果记录编号与结论。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"算子定义 {entry_id} 不存在或已归档"
        refs = parse_references(entry.get("引用函数"))
        checks, consumed, error = self._check_references(refs)
        if error:
            return None, error
        return {
            "算子编号": entry.get("算子编号"),
            "引用函数": refs,
            "消费记录编号": consumed,
            "引用校验明细": checks,
            "发布校验结果": self._publish_result(checks),
        }, "发布校验预览完成"

    def _publish(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            with store.transaction(MODULE, REF_MODULE, "udf"):
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    raise TransactionAbort(f"算子定义 {entry_id} 不存在或已归档")
                if entry.get("status") != STATUS_DRAFT:
                    raise TransactionAbort(
                        f"算子当前状态为「{entry.get('status')}」，只有草稿状态的算子可以发布"
                    )
                if entry.get("发布状态") == PUBLISHED:
                    raise TransactionAbort(
                        f"算子 {entry.get('算子编号')} 已发布，重复发布会覆盖既有引用结果"
                    )
                expected, error = self._expected_version(entry, values)
                if error:
                    raise TransactionAbort(error)
                # 版本核对通过后、改任何字段之前，先做引用结果版本冲突复核：
                # 若函数引用结果在本次发布前刚被别的入口改写过，这里直接中止。
                refs = parse_references(values.get("引用函数")) or parse_references(
                    entry.get("引用函数")
                )
                checks, consumed, error = self._check_references(refs)
                if error:
                    raise TransactionAbort(error)
                version = expected + 1
                entry["引用函数"] = refs
                # 发布只消费引用结果，不新造记录：算子与结果之间靠记录编号关联。
                for record_number in consumed:
                    result = store.find_by(REF_MODULE, "记录编号", record_number)
                    if result is not None:
                        adopters = result.setdefault("采用算子", [])
                        if entry.get("算子编号") not in adopters:
                            adopters.append(entry.get("算子编号"))
                entry["引用版本"] = version
                entry["引用记录"] = consumed
                entry["发布状态"] = PUBLISHED
                entry["发布校验结果"] = self._publish_result(checks)
                entry.setdefault("引用历史", []).append({
                    "版本": version,
                    "操作": PUBLISH_ACTION,
                    "引用函数": list(refs),
                    "消费记录编号": list(consumed),
                    "发布校验结果": entry["发布校验结果"],
                    "时间": date.today().isoformat(),
                })
                return entry, (
                    f"算子已发布，引用校验通过，消费引用结果 {len(consumed)} 条"
                    + (f"：{'、'.join(consumed)}" if consumed else "")
                )
        except TransactionAbort as abort:
            return None, abort.message

    def _check_references(
        self, refs: list[str]
    ) -> tuple[list[dict[str, Any]], list[str], str | None]:
        """按记录编号核对每个函数的引用结果；只接受最新已确认的版本。"""
        if not refs:
            return [], [], None
        udf_rows = store.rows("udf")
        checks: list[dict[str, Any]] = []
        consumed: list[str] = []
        for function_number in refs:
            function = next(
                (row for row in udf_rows if row.get("函数编号") == function_number), None
            )
            if function is None:
                return [], [], f"引用的函数 {function_number} 不存在或已归档"
            latest = self.udf.latest_reference(function_number)
            if latest is None:
                return [], [], (
                    f"函数 {function_number} 缺少已确认的引用数据结果，"
                    "请先在自定义函数库执行校验引用"
                )
            if latest.get("确认状态") != CONFIRMED:
                return [], [], (
                    f"函数 {function_number} 最新的引用结果 {latest.get('记录编号')} 尚未确认"
                )
            if str(latest.get("停用状态")) in {STATUS_DISABLED, STATUS_DELETED}:
                return [], [], (
                    f"函数 {function_number} 已停用（引用结果 {latest.get('记录编号')}），"
                    "不能带入新的执行链路"
                )
            if str(function.get("status")) in {STATUS_DISABLED, STATUS_DELETED}:
                return [], [], (
                    f"函数 {function_number} 当前状态为「{function.get('status')}」，"
                    "已有的引用数据结果已过期，不能用于发布"
                )
            if int(latest.get("版本", 0)) != int(function.get("引用版本", 0)):
                return [], [], (
                    f"函数 {function_number} 的引用数据结果版本 {latest.get('版本')} "
                    f"落后于当前版本 {function.get('引用版本')}，请重新校验引用"
                )
            consumed.append(str(latest.get("记录编号")))
            checks.append({
                "函数编号": function_number,
                "返回类型": function.get("返回类型"),
                "测试用例": function.get("测试用例"),
                "记录编号": latest.get("记录编号"),
                "版本": latest.get("版本"),
                "停用状态": latest.get("停用状态"),
                "匹配算子": list(latest.get("匹配算子") or []),
            })
        return checks, consumed, None

    def _check_published_references(
        self, entry: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str], str | None]:
        """复核已发布算子当时消费的引用结果，确保函数没有被停用。"""
        consumed = [str(item) for item in entry.get("引用记录") or []]
        if not consumed:
            return [], [], None
        checks: list[dict[str, Any]] = []
        for record_number in consumed:
            result = store.find_by(REF_MODULE, "记录编号", record_number)
            if result is None:
                return [], [], (
                    f"算子 {entry.get('算子编号')} 消费的引用结果 {record_number} 已不存在"
                )
            function = store.find_by("udf", "函数编号", result.get("函数编号"))
            if function is None:
                return [], [], (
                    f"算子 {entry.get('算子编号')} 引用的函数 {result.get('函数编号')} 已归档"
                )
            if str(function.get("status")) in {STATUS_DISABLED, STATUS_DELETED}:
                return [], [], (
                    f"函数 {function.get('函数编号')} 已停用，算子"
                    f"{entry.get('算子编号')} 不能继续执行，请重新校验引用后再发布"
                )
            checks.append({
                "函数编号": function.get("函数编号"),
                "记录编号": record_number,
                "返回类型": result.get("返回类型"),
                "测试用例": result.get("测试用例"),
                "匹配算子": list(result.get("匹配算子") or []),
                "停用状态": result.get("停用状态"),
            })
        return checks, consumed, None

    @staticmethod
    def _publish_result(checks: list[dict[str, Any]]) -> str:
        if not checks:
            return "无函数引用"
        return "发布校验通过"

    @staticmethod
    def _expected_version(
        entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[int, str | None]:
        current = int(entry.get("引用版本", 0))
        raw = values.get("期望版本")
        if raw is None or str(raw).strip() == "":
            return current, None
        try:
            expected = int(raw)
        except (TypeError, ValueError):
            return current, "期望版本必须是整数"
        if expected != current:
            return current, (
                f"算子 {entry.get('算子编号')} 的引用版本已变为 {current}"
                f"（期望 {expected}），并发写入未被接受，请按最新版本重试"
            )
        return current, None
