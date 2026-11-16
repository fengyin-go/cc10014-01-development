"""数据查询接口：维护查询定义，覆盖提交校验、启用查询、停用查询等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, FlexiblePayload, PageResult
from app.services.query import QueryService
from app.services.query_result import EXECUTE_ACTIONS, RERUN_ACTIONS, result_service

router = APIRouter(prefix="/api/query", tags=["数据查询"])

service = QueryService()

LIST_FIELDS = ["定义编号", "关联数据源", "查询模板", "参数占位", "结果字段", "排序规则", "分页大小", "超时秒数"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按定义编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按定义编号与状态过滤数据查询列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据查询清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "query", "total": total, "items": items}


@router.get("/results", response_model=PageResult[dict])
def all_results(
    definition_no: str | None = Query(default=None, description="按定义编号检索"),
    status: str | None = Query(default=None, description="已完成、失败"),
    sort: str = Query(default="创建时间"),
    order: str = Query(default="asc"),
    page: int = 1,
    size: int = 20,
    snapshot: int | None = Query(default=None, description="首次响应的 snapshot，翻页时回传"),
) -> PageResult[dict]:
    """查询结果清单的别名入口，便于从数据查询模块直接定位结果记录。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    page = max(page, 1)
    items, total, resolved = result_service.list_runs(
        definition_no=definition_no,
        status=status,
        sort=sort,
        order=order,
        page=page,
        size=size,
        as_of=snapshot,
    )
    return PageResult(items=items, total=total, page=page, size=size, snapshot=resolved)


@router.get("/{entry_ref}", response_model=dict)
def get_entry(entry_ref: str) -> dict:
    """读取单条查询定义明细；数字 id 和定义编号都能定位。"""
    entry = service.resolve(entry_ref)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry_ref} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: FlexiblePayload) -> ActionResult:
    """登记一条查询定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.all_values())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: FlexiblePayload) -> ActionResult:
    """对单条查询定义执行提交校验、启用查询、停用查询；不允许的动作会被拦下并说明原因。

    动作值为“执行查询”“重新执行”时走查询执行链路：首次执行按当前定义冻结快照，
    重新执行才会采用新的参数、排序规则与结果字段。
    """
    action = str(payload.all_values().get("action") or "").strip()
    if action in EXECUTE_ACTIONS or action in RERUN_ACTIONS:
        run, message, duplicated = result_service.execute(
            entry_id,
            payload.all_values(),
            force=action in RERUN_ACTIONS,
        )
        if run is None:
            return ActionResult(ok=False, message=message)
        return ActionResult(ok=True, message=message, entry={**run, "duplicated": duplicated})
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: str, payload: FlexiblePayload) -> ActionResult:
    """修改查询定义；参数、排序、结果字段变化会分别推进版本号。"""
    entry, message, changed = service.update_entry(entry_id, payload.all_values())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={**entry, "changed": changed})


@router.put("/{entry_id}", response_model=ActionResult)
def replace_entry(entry_id: str, payload: FlexiblePayload) -> ActionResult:
    """PATCH 的等价入口，便于前端和脚本统一用 PUT 提交修改。"""
    entry, message, changed = service.update_entry(entry_id, payload.all_values())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={**entry, "changed": changed})


@router.post("/{entry_id}", response_model=ActionResult)
def post_update_entry(entry_id: str, payload: FlexiblePayload) -> ActionResult:
    """以 POST 提交查询定义修改，动作字段仍然优先按动作执行。"""
    action = str(payload.all_values().get("action") or "").strip()
    if action in EXECUTE_ACTIONS or action in RERUN_ACTIONS:
        run, message, duplicated = result_service.execute(
            entry_id, payload.all_values(), force=action in RERUN_ACTIONS
        )
        if run is None:
            return ActionResult(ok=False, message=message)
        return ActionResult(ok=True, message=message, entry={**run, "duplicated": duplicated})
    entry, message, changed = service.update_entry(entry_id, payload.all_values())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={**entry, "changed": changed})


@router.post("/{entry_ref}/execute", response_model=ActionResult)
def execute_entry(entry_ref: str, payload: FlexiblePayload) -> ActionResult:
    """执行查询的统一入口：首次按当前定义冻结快照，重复提交只生效一次。"""
    run, message, duplicated = result_service.execute(entry_ref, payload.all_values())
    if run is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={**run, "duplicated": duplicated})


@router.get("/{entry_ref}/result", response_model=dict)
def latest_entry_result(entry_ref: str) -> dict:
    """读取某个查询定义最近一次执行的快照；没有执行过时返回可读说明。"""
    entry = service.resolve(entry_ref)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry_ref} 不存在或已归档")
    run = result_service.latest_run(entry_ref)
    if run is None:
        return {"ok": False, "message": f"查询定义 {entry.get('定义编号')} 还没有查询结果", "entry": None}
    return {"ok": True, "message": "已返回最近一次查询结果", "entry": run}


@router.get("/{entry_ref}/page", response_model=dict)
def latest_entry_page(
    entry_ref: str,
    page: int = 1,
    size: int | None = Query(default=None, description="不传时使用运行快照的分页大小"),
) -> dict:
    """按最近一次运行的快照分页读取结果记录，便于连续翻页复核。"""
    entry = service.resolve(entry_ref)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry_ref} 不存在或已归档")
    run = result_service.latest_run(entry_ref)
    if run is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry.get('定义编号')} 还没有查询结果")
    payload, message = result_service.records_page(run["run_id"], page=page, size=size)
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return payload


@router.post("/{entry_ref}/verify", response_model=dict)
def verify_entry_result(entry_ref: str) -> dict:
    """按查询定义复核该定义最近一次运行的结果字段与分页记录。"""
    entry = service.resolve(entry_ref)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry_ref} 不存在或已归档")
    run = result_service.latest_run(entry_ref)
    if run is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry.get('定义编号')} 还没有查询结果")
    payload, message = result_service.verify(run["run_id"])
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return payload


@router.get("/{entry_id}/results", response_model=PageResult[dict])
def entry_results(
    entry_id: str,
    page: int = 1,
    size: int = 20,
    snapshot: int | None = Query(default=None, description="首次响应的 snapshot，翻页时回传"),
) -> PageResult[dict]:
    """某个查询定义的历史运行清单，用来复核连续翻页用的是哪一份快照。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    entry = service.resolve(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry_id} 不存在或已归档")
    page = max(page, 1)
    items, total, resolved = result_service.list_runs(
        entry_id=int(entry.get("id", 0)), page=page, size=size, as_of=snapshot
    )
    return PageResult(items=items, total=total, page=page, size=size, snapshot=resolved)
