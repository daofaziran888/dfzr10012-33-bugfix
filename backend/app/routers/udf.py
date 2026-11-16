"""自定义函数库接口：维护函数定义，覆盖注册函数、停用函数、删除函数等动作。

另外提供「函数引用数据结果」的查询入口：结果由停用函数、校验引用两个入口写入，
流处理算子在发布时按记录编号消费同一份数据。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.udf import UdfService

router = APIRouter(prefix="/api/udf", tags=["自定义函数库"])

service = UdfService()

LIST_FIELDS = ["函数编号", "函数名称", "输入参数", "返回类型", "函数代码", "测试用例", "注册时间", "函数状态"]
STATUSES = ["草稿", "已注册", "已停用", "已删除"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按函数编号检索"),
    status: str | None = Query(default=None, description="草稿、已注册、已停用、已删除"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按函数编号与状态过滤自定义函数库列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/references", response_model=PageResult[dict])
def list_references(
    function_number: str | None = Query(default=None, description="按函数编号检索"),
    record_number: str | None = Query(default=None, description="按记录编号检索"),
    disabled_state: str | None = Query(default=None, description="按停用状态检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取函数引用数据结果：保留匹配算子、停用状态与测试用例，历史记录只追加不覆盖。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_references(
        function_number=function_number,
        record_number=record_number,
        disabled_state=disabled_state,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/references/{record_number}", response_model=dict)
def get_reference(record_number: str) -> dict:
    """按记录编号读取单条引用结果，供算子发布侧对表消费。"""
    result = service.get_reference(record_number)
    if result is None:
        raise HTTPException(status_code=404, detail=f"引用结果记录 {record_number} 不存在")
    return result


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出自定义函数库清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "udf", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条函数定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"函数定义 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/references", response_model=dict)
def get_entry_references(entry_id: int) -> dict:
    """读取某个函数的全部引用数据结果，按版本返回，历史引用可追溯。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"函数定义 {entry_id} 不存在或已归档")
    history = service.list_function_references(str(entry.get("函数编号")))
    return {
        "函数编号": entry.get("函数编号"),
        "当前引用版本": int(entry.get("引用版本", 0)),
        "总条数": len(history),
        "items": history,
    }


@router.get("/{entry_id}/reference-preview", response_model=dict)
def preview_reference(entry_id: int) -> dict:
    """只读预览引用索引匹配结果，不写引用数据结果、不改函数状态。"""
    preview, message = service.preview_reference(entry_id)
    if preview is None:
        raise HTTPException(status_code=400, detail=message)
    return preview


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条函数定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.merged_values())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="函数定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条函数定义执行注册函数、停用函数、删除函数、校验引用。"""
    values = payload.merged_values()
    action = str(values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
