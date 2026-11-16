"""自定义函数库业务规则：状态流转、引用索引、停用终态与引用结果都收在这里。

「函数引用数据结果」（内部模块 ``udf_ref``）是这里产出的派生数据：
- 来源：停用函数、校验引用两个入口会读取函数定义的 ``输入参数`` / ``返回类型`` /
  ``函数代码``，按算子上的 ``引用函数`` 建立引用索引，匹配到的算子连同
  ``停用状态``、``测试用例`` 一并写入一条新的结果记录。
- 去向：流处理算子在发布时按 ``记录编号`` 消费同一份结果，只接受最新且已确认的版本，
  并把自身登记到结果的 ``采用算子``；发布本身不再新造结果记录。
- 追溯：每条结果按版本追加，从不覆盖历史记录。

并发写入采用乐观版本（函数上的 ``引用版本``）：调用方可以带 ``期望版本``，
版本不一致时整笔操作不落库，保证停用与算子发布并发时只有一方被接受。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.store import TransactionAbort, store

MODULE = "udf"
REF_MODULE = "udf_ref"
REQUIRED_FIELDS = ["函数编号", "函数名称", "输入参数"]
# 引用校验必须读到的函数字段：缺任何一项都不允许停用，也不允许产出引用结果。
REFERENCE_FIELDS = ["输入参数", "返回类型", "函数代码"]
STATUS_ORDER = ["草稿", "已注册", "已停用", "已删除"]
STATUS_DRAFT, STATUS_REGISTERED, STATUS_DISABLED, STATUS_DELETED = STATUS_ORDER
# 动作 -> (要求的当前状态, 流转后的目标状态)
ACTION_RULES: dict[str, tuple[str, str]] = {
    "注册函数": (STATUS_DRAFT, STATUS_REGISTERED),
    "停用函数": (STATUS_REGISTERED, STATUS_DISABLED),
    "删除函数": (STATUS_DISABLED, STATUS_DELETED),
}
CHECK_ACTION = "校验引用"
NEGATIVE_ACTIONS = ["停用函数", "删除函数"]
CONFIRMED = "已确认"


def parse_references(value: Any) -> list[str]:
    """把算子上的函数引用字段统一解析成函数编号列表。

    存量记录可能是逗号、顿号分隔的字符串，也可能是已经拆好的列表。
    """
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[,，、;；\s]+", str(value))
    seen: list[str] = []
    for part in parts:
        code = part.strip()
        if code and code not in seen:
            seen.append(code)
    return seen


class UdfService:
    # ---------- 查询 ----------

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
            rows = [row for row in rows if keyword in str(row.get("函数编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_references(
        self,
        *,
        function_number: str | None = None,
        record_number: str | None = None,
        disabled_state: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """读取函数引用数据结果；按记录编号、函数编号、停用状态过滤。"""
        rows = store.rows(REF_MODULE)
        if function_number:
            rows = [row for row in rows if row.get("函数编号") == function_number]
        if record_number:
            rows = [row for row in rows if row.get("记录编号") == record_number]
        if disabled_state:
            rows = [row for row in rows if row.get("停用状态") == disabled_state]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_reference(self, record_number: str) -> dict[str, Any] | None:
        return store.find_by(REF_MODULE, "记录编号", record_number)

    def list_function_references(self, function_number: str) -> list[dict[str, Any]]:
        """某个函数的全部引用结果，按版本升序返回（历史引用保持可追溯）。"""
        rows = [
            row for row in store.rows(REF_MODULE)
            if row.get("函数编号") == function_number
        ]
        return sorted(rows, key=lambda row: int(row.get("版本", 0)))

    def latest_reference(self, function_number: str) -> dict[str, Any] | None:
        """取某个函数最新的一条引用结果（版本最大），用于发布校验对表。"""
        rows = [row for row in store.rows(REF_MODULE) if row.get("函数编号") == function_number]
        if not rows:
            return None
        return max(rows, key=lambda row: int(row.get("版本", 0)))

    def build_reference_index(self) -> dict[str, list[dict[str, Any]]]:
        """函数编号 -> 引用它的算子列表，作为停用前引用匹配的唯一索引。"""
        index: dict[str, list[dict[str, Any]]] = {}
        for operator in store.rows("operator"):
            for function_number in parse_references(operator.get("引用函数")):
                index.setdefault(function_number, []).append({
                    "算子编号": operator.get("算子编号"),
                    "算子名称": operator.get("算子名称"),
                    "算子状态": operator.get("status"),
                    "发布状态": operator.get("发布状态"),
                })
        for entries in index.values():
            entries.sort(key=lambda item: str(item.get("算子编号", "")))
        return index

    def preview_reference(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """只读预览引用匹配结果，不写引用数据结果、不改函数状态。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"函数定义 {entry_id} 不存在或已归档"
        missing = self._missing_reference_fields(entry)
        if missing:
            return None, f"引用校验缺少字段：{'、'.join(missing)}"
        matches = self._match_operators(entry)
        return {
            "函数编号": entry.get("函数编号"),
            "输入参数": entry.get("输入参数"),
            "返回类型": entry.get("返回类型"),
            "函数代码": entry.get("函数代码"),
            "测试用例": entry.get("测试用例"),
            "匹配算子": [item["算子编号"] for item in matches],
            "匹配算子明细": matches,
            "匹配数量": len(matches),
            "校验结果": self._match_result(matches),
            "函数状态": entry.get("status"),
            "引用版本": int(entry.get("引用版本", 0)),
        }, "引用索引匹配完成"

    # ---------- 写入 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 沿用存量字段：可选的函数签名与代码一并登记，缺失时留空但保留键位。
        entry["返回类型"] = values.get("返回类型")
        entry["函数代码"] = values.get("函数代码")
        entry["测试用例"] = values.get("测试用例")
        entry["注册时间"] = values.get("注册时间") or date.today().isoformat()
        entry["status"] = STATUS_DRAFT
        entry["函数状态"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["引用版本"] = 0
        entry["最近引用记录编号"] = None
        entry["最近引用校验"] = None
        rows.append(entry)
        return entry, []

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        if action == CHECK_ACTION:
            return self._check_reference(entry_id, values)
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于自定义函数库可执行范围"
        required, target = ACTION_RULES[action]
        try:
            with store.transaction(MODULE, REF_MODULE, "operator"):
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    raise TransactionAbort(f"函数定义 {entry_id} 不存在或已归档")
                status = str(entry.get("status"))
                if status != required:
                    if action == "停用函数" and status == STATUS_DISABLED:
                        raise TransactionAbort(
                            f"函数定义 {entry.get('函数编号')} 已处于停用终态，"
                            "重复停用不会再次生效，历史引用结果仍可追溯"
                        )
                    raise TransactionAbort(
                        f"函数定义当前状态为「{status}」，只有「{required}」可以执行{action}"
                    )
                if action == "停用函数":
                    return self._apply_disable(entry, values)
                entry["status"] = target
                entry["函数状态"] = target
                entry["pending"] = target != STATUS_DELETED
                entry["abnormal"] = action in NEGATIVE_ACTIONS
                return entry, f"函数定义已{action}"
        except TransactionAbort as abort:
            return None, abort.message

    def _apply_disable(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        # 停用前先完成引用索引匹配：缺字段或版本冲突都会在写状态前中止事务。
        missing = self._missing_reference_fields(entry)
        if missing:
            raise TransactionAbort(
                f"停用前引用校验失败：函数定义缺少{'、'.join(missing)}，无法建立引用索引"
            )
        expected, error = self._expected_version(entry, values)
        if error:
            raise TransactionAbort(error)
        # 停用与算子发布并发：若已有算子按同一份引用结果完成发布，只接受发布结果。
        consumers = self._published_consumers(entry)
        if consumers:
            raise TransactionAbort(
                f"函数 {entry.get('函数编号')} 的引用结果已被算子"
                f"{'、'.join(consumers)} 的最新发布采用，停用与发布并发只落一个结果，"
                "本次停用未被接受"
            )
        matches = self._match_operators(entry)
        version = expected + 1
        result = self.write_reference_result(
            entry,
            version=version,
            disabled_state=STATUS_DISABLED,
            confirmation=CONFIRMED,
            matches=matches,
        )
        entry["status"] = STATUS_DISABLED
        entry["函数状态"] = STATUS_DISABLED
        entry["pending"] = False
        entry["abnormal"] = True
        entry["引用版本"] = version
        entry["最近引用记录编号"] = result["记录编号"]
        entry["最近引用校验"] = result["校验结果"]
        return entry, (
            f"函数定义已停用，引用索引命中 {len(matches)} 个算子，"
            f"引用结果记录 {result['记录编号']}"
        )

    def _check_reference(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """校验引用关系并写入一条已确认的引用数据结果（不改函数状态）。"""
        try:
            with store.transaction(MODULE, REF_MODULE, "operator"):
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    raise TransactionAbort(f"函数定义 {entry_id} 不存在或已归档")
                if entry.get("status") in {STATUS_DISABLED, STATUS_DELETED}:
                    raise TransactionAbort(
                        f"函数定义已处于「{entry.get('status')}」，不能再执行{CHECK_ACTION}"
                    )
                missing = self._missing_reference_fields(entry)
                if missing:
                    raise TransactionAbort(f"引用校验缺少字段：{'、'.join(missing)}")
                expected, error = self._expected_version(entry, values)
                if error:
                    raise TransactionAbort(error)
                matches = self._match_operators(entry)
                version = expected + 1
                result = self.write_reference_result(
                    entry,
                    version=version,
                    disabled_state=str(entry.get("status")),
                    confirmation=CONFIRMED,
                    matches=matches,
                )
                entry["引用版本"] = version
                entry["最近引用记录编号"] = result["记录编号"]
                entry["最近引用校验"] = result["校验结果"]
                return entry, (
                    f"引用校验完成，命中 {len(matches)} 个算子，"
                    f"引用结果记录 {result['记录编号']}"
                )
        except TransactionAbort as abort:
            return None, abort.message

    def write_reference_result(
        self,
        entry: dict[str, Any],
        *,
        version: int,
        disabled_state: str,
        confirmation: str,
        matches: list[dict[str, Any]],
        adopted_operator: str | None = None,
    ) -> dict[str, Any]:
        """追加一条函数引用数据结果，保留匹配算子、停用状态和测试用例。"""
        rows = store.rows(REF_MODULE)
        record_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        adopters: list[str] = []
        if adopted_operator and adopted_operator not in adopters:
            adopters.append(adopted_operator)
        result = {
            "id": record_id,
            "记录编号": self._next_record_number(rows),
            "函数编号": entry.get("函数编号"),
            "函数名称": entry.get("函数名称"),
            "输入参数": entry.get("输入参数"),
            "返回类型": entry.get("返回类型"),
            "函数代码": entry.get("函数代码"),
            "测试用例": entry.get("测试用例"),
            "匹配算子": [item["算子编号"] for item in matches],
            "匹配算子明细": [dict(item) for item in matches],
            "匹配数量": len(matches),
            "停用状态": disabled_state,
            "版本": version,
            "确认状态": confirmation,
            "校验结果": self._match_result(matches),
            "采用算子": adopters,
            "生成时间": date.today().isoformat(),
        }
        rows.append(result)
        return result

    # ---------- 内部工具 ----------

    def _match_operators(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        index = self.build_reference_index()
        return index.get(str(entry.get("函数编号")), [])

    @staticmethod
    def _published_consumers(entry: dict[str, Any]) -> list[str]:
        """找出已按该函数最新引用结果完成发布、且尚未下线的算子编号。"""
        function_number = str(entry.get("函数编号"))
        latest = None
        for row in store.rows(REF_MODULE):
            if row.get("函数编号") != function_number:
                continue
            if latest is None or int(row.get("版本", 0)) > int(latest.get("版本", 0)):
                latest = row
        if latest is None:
            return []
        consumed = [str(code) for code in latest.get("采用算子") or []]
        return sorted(
            code
            for code in consumed
            if any(
                operator.get("算子编号") == code and operator.get("发布状态") == "已发布"
                for operator in store.rows("operator")
            )
        )

    @staticmethod
    def _match_result(matches: list[dict[str, Any]]) -> str:
        return "引用索引命中" if matches else "无算子引用"

    @staticmethod
    def _missing_reference_fields(entry: dict[str, Any]) -> list[str]:
        return [
            field
            for field in REFERENCE_FIELDS
            if not str(entry.get(field) or "").strip()
        ]

    @staticmethod
    def _next_record_number(rows: list[dict[str, Any]]) -> str:
        largest = 0
        for row in rows:
            code = str(row.get("记录编号", ""))
            if code.startswith("UDFREF-"):
                try:
                    largest = max(largest, int(code.split("-", 1)[1]))
                except ValueError:
                    continue
        return f"UDFREF-{largest + 1:04d}"

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
                f"函数 {entry.get('函数编号')} 的引用数据结果版本已变为 {current}"
                f"（期望 {expected}），竞态写入未被接受，请按最新版本重试"
            )
        return current, None
