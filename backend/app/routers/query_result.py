"""查询结果接口：执行查询、分页读取结果记录、复核快照与查询字段台账。

一次执行冻结参数快照、结果字段、排序规则和定义版本；分页只按 run_id 取记录，
翻页不会重复，也不会把两个口径混在一起。定义编号（如 QUER-0003）和数字 id
都可以作为入口参数。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, FlexiblePayload, PageResult
from app.services.query import QueryService
from app.services.query_result import ARBITRATION, result_service

router = APIRouter(prefix="/api/query-result", tags=["查询结果"])
ledger_router = APIRouter(prefix="/api/query-field-ledger", tags=["查询字段台账"])
# “查询结果契约”模块同步读取同一份查询字段台账，避免导出和下游转换各拿一套字段。
contract_router = APIRouter(prefix="/api/query-contract", tags=["查询结果契约"])

query_service = QueryService()
STATUSES = ["已完成", "失败"]


DEFINITION_KEYS = ("定义编号", "definition", "definition_no", "entry_id", "query")
PARAMETER_KEYS = ("parameters", "params", "参数", "参数值", "query_params")
REQUEST_KEYS = ("request_id", "idempotency_key", "idempotencyKey", "请求编号")


def _run_from_values(values: dict[str, Any], remark: str | None = None) -> ActionResult:
    """从入参里取出定义编号、参数与幂等键，执行一次查询。"""
    definition = next(
        (values.pop(key) for key in DEFINITION_KEYS if values.get(key) not in (None, "")),
        None,
    )
    if definition is None:
        service_entry = query_service.find_by_definition(str(remark or ""))
        if service_entry is None:
            return ActionResult(ok=False, message="缺少必填字段：定义编号")
        definition = service_entry.get("定义编号")
    parameters = next(
        (values.pop(key) for key in PARAMETER_KEYS if isinstance(values.get(key), dict)),
        None,
    )
    request_id = next(
        (values.pop(key) for key in REQUEST_KEYS if values.get(key)),
        None,
    )
    action = str(values.pop("action", "") or "").strip()
    force = bool(values.pop("force", False)) or action == "重新执行"
    run, message, duplicated = result_service.execute(
        str(definition),
        values,
        parameters=parameters,
        request_id=str(request_id) if request_id else None,
        force=force,
    )
    if run is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={**run, "duplicated": duplicated})


@router.post("/execute", response_model=ActionResult)
def execute(payload: FlexiblePayload) -> ActionResult:
    """执行查询并生成查询结果记录；重复提交只会生效一次。

    入参既可以是 {"values": {...}}，也可以把定义编号、参数放在顶层。
    """
    return _run_from_values(payload.all_values(), payload.remark)


@router.post("", response_model=ActionResult)
def create_result(payload: FlexiblePayload) -> ActionResult:
    """查询结果的 POST 入口与执行接口等价：提交定义编号与参数即生成结果记录。"""
    return execute(payload)


@router.get("", response_model=PageResult[dict])
def list_results(
    keyword: str | None = Query(default=None, description="按定义编号或运行编号检索"),
    definition_no: str | None = Query(default=None, description="按定义编号精确检索"),
    status: str | None = Query(default=None, description="已完成、失败"),
    sort: str = Query(default="创建时间", description="创建时间、定义编号、运行编号、记录数"),
    order: str = Query(default="asc", description="asc 或 desc"),
    sort_by: str | None = Query(default=None, description="sort 的别名"),
    sort_order: str | None = Query(default=None, description="order 的别名"),
    page: int = 1,
    size: int = 20,
    page_size: int | None = Query(default=None, description="size 的别名"),
    snapshot: int | None = Query(default=None, description="首次响应的 snapshot，翻页时回传"),
) -> PageResult[dict]:
    """查询结果清单：支持定义编号检索、状态筛选、排序和分页定位。

    snapshot 把翻页范围锁定在首次查询时的运行集合，翻页期间新增的执行不会把
    后面的页往后挤，避免同一条结果记录在两页里重复出现。
    """
    size = page_size or size
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    page = max(page, 1)
    items, total, resolved_snapshot = result_service.list_runs(
        keyword=keyword,
        status=status,
        definition_no=definition_no,
        sort=sort_by or sort,
        order=sort_order or order,
        page=page,
        size=size,
        as_of=snapshot,
    )
    return PageResult(
        items=items, total=total, page=page, size=size, snapshot=resolved_snapshot
    )


@router.post("/actions", response_model=ActionResult)
def execute_action(payload: FlexiblePayload) -> ActionResult:
    """统一执行入口：action=执行查询/重新执行，values 里带定义编号与参数。"""
    return _run_from_values(payload.all_values(), payload.remark)


@router.get("/definition/{definition_no}", response_model=dict)
def result_by_definition(
    definition_no: str,
    page: int = 1,
    size: int | None = Query(default=None, description="不传时使用运行快照的分页大小"),
) -> dict:
    """按定义编号读取最近一次查询结果并分页，入口与 QUER-0003 这类编号一致。"""
    run = result_service.latest_run(definition_no)
    if run is None:
        return {"ok": False, "message": f"查询定义 {definition_no} 还没有查询结果", "entry": None}
    payload, message = result_service.records_page(run["run_id"], page=page, size=size)
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": "已按最近一次运行快照分页", "entry": payload}


@router.get("/export")
def export_results() -> dict[str, Any]:
    """导出查询结果清单：返回全量运行记录（不含逐条结果记录）。"""
    items, total, snapshot = result_service.list_runs(page=1, size=10000)
    return {"module": "query-result", "total": total, "snapshot": snapshot, "items": items}


@router.get("/latest")
def latest_result(definition_no: str = Query(description="定义编号，如 QUER-0003")) -> dict[str, Any]:
    """取某个查询定义最近一次执行的快照；没有执行过时返回空结果而不是报错。"""
    run = result_service.latest_run(definition_no)
    if run is None:
        return {"ok": False, "message": f"查询定义 {definition_no} 还没有查询结果", "entry": None}
    return {"ok": True, "message": "已返回最近一次查询结果", "entry": run}


@router.get("/arbitration", response_model=dict)
def arbitration_rules() -> dict[str, Any]:
    """口径冲突的仲裁优先级：参数、结果字段、翻页与重复提交各自按顺序取值。"""
    return {
        "ok": True,
        "message": "口径冲突按以下优先级仲裁，靠前的取值优先",
        "rules": {key: list(value) for key, value in ARBITRATION.items()},
    }


@router.get("/{run_id}", response_model=dict)
def get_result(run_id: str) -> dict:
    """读取一次查询运行的结果摘要；run_id 也可以用定义编号代替。"""
    run = result_service.resolve_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"查询结果 {run_id} 不存在或已归档")
    return run


@router.get("/{run_id}/records", response_model=dict)
def list_records(
    run_id: str,
    page: int = 1,
    size: int | None = Query(default=None, description="不传时使用运行快照的分页大小"),
) -> dict:
    """按运行快照分页读取结果记录；返回 items/total/page/size 和快照说明。"""
    payload, message = result_service.records_page(run_id, page=page, size=size)
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return payload


@router.get("/{run_id}/page", response_model=dict)
def records_page_detail(
    run_id: str,
    page: int = 1,
    size: int | None = Query(default=None, description="不传时使用运行快照的分页大小"),
) -> dict:
    """分页定位结果记录，同时回显快照参数、结果字段和记录编号，便于复核翻页不重复。"""
    payload, message = result_service.records_page(run_id, page=page, size=size)
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return payload


@router.get("/{run_id}/dataset", response_model=dict)
def result_dataset(run_id: str) -> dict:
    """查询结果数据集：记录、参数快照与字段台账在同一视图，供下游转换复核。"""
    payload = result_service.dataset(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"查询结果 {run_id} 不存在或已归档")
    return payload


@router.get("/{run_id}/fields", response_model=dict)
def result_fields(run_id: str) -> dict:
    """结果字段清单：供导出和下游转换读取同一次运行冻结的字段口径。"""
    payload = result_service.fields_for_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"查询结果 {run_id} 不存在或已归档")
    return payload


@router.post("/{run_id}/verify", response_model=dict)
def verify_result(run_id: str) -> dict[str, Any]:
    """按查询定义复核结果字段与分页记录，输出口径漂移与重复记录。"""
    payload, message = result_service.verify(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return payload


@ledger_router.get("", response_model=PageResult[dict])
def list_ledger(
    definition_no: str | None = Query(default=None, description="按定义编号检索"),
    run_id: str | None = Query(default=None, description="按查询运行编号检索"),
    field_name: str | None = Query(default=None, description="按字段名称检索"),
    page: int = 1,
    size: int = 200,
) -> PageResult[dict]:
    """查询字段台账：导出与下游转换读取同一份字段清单。"""
    if size > 500:
        raise HTTPException(status_code=400, detail="每页最多 500 条，请缩小分页范围")
    items, total = result_service.list_ledger(
        definition_no=definition_no,
        run_id=run_id,
        field_name=field_name,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@ledger_router.get("/export")
def export_ledger(
    definition_no: str | None = Query(default=None, description="按定义编号检索"),
    run_id: str | None = Query(default=None, description="按查询运行编号检索"),
) -> dict[str, Any]:
    """导出查询字段台账：导出与下游转换读取同一份字段清单。"""
    items, total = result_service.list_ledger(
        definition_no=definition_no, run_id=run_id, page=1, size=100000
    )
    return {"module": "query-field-ledger", "total": total, "items": items}


@ledger_router.get("/{run_id}", response_model=dict)
def ledger_for_run(run_id: str) -> dict:
    """读取某次查询运行的字段台账；定义编号也可作为入口。"""
    payload = result_service.fields_for_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"查询结果 {run_id} 不存在或已归档")
    return payload


@contract_router.get("", response_model=PageResult[dict])
def contract_fields(
    definition_no: str | None = Query(default=None, description="按定义编号检索"),
    run_id: str | None = Query(default=None, description="按查询运行编号检索"),
    page: int = 1,
    size: int = 200,
) -> PageResult[dict]:
    """查询结果契约读取字段台账：与导出、下游转换共享同一字段清单。"""
    if size > 500:
        raise HTTPException(status_code=400, detail="每页最多 500 条，请缩小分页范围")
    items, total = result_service.list_ledger(
        definition_no=definition_no,
        run_id=run_id,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@contract_router.get("/{run_id}", response_model=dict)
def contract_for_run(run_id: str) -> dict:
    """某次运行的字段契约：字段清单、字段版本与投影来源。"""
    payload = result_service.fields_for_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"查询结果 {run_id} 不存在或已归档")
    return payload
