"""查询结果业务规则：参数绑定、源记录读取、结果字段投影与分页复核。

一次执行会冻结“参数快照 + 结果字段清单 + 排序规则 + 定义版本”，后续翻页只认
这份快照，查询定义在此期间被修改也不会把两个口径的记录混在一页里。重新执行才
会读取新的参数、排序规则和结果字段。

并发重复提交用双保险：
1. 调用方传入的 request_id / idempotency_key 相同，直接复用第一次的结果；
2. 没有显式幂等键时，用“定义 + 参数快照 + 定义版本”生成指纹，并发请求只生效一次。
显式传 force=True（或 action=重新执行）才会跳过指纹复用。

口径冲突时按固定优先级仲裁：
- 参数：本次执行显式参数 > 定义里的参数默认值 > 模板占位（缺参直接报错）。
- 结果字段：定义的结果字段 > 查询模板的 SELECT 投影 > 源记录全部字段。
- 翻页：本次运行快照 > 查询定义当前值；只有重新执行才采用新规则。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from typing import Any

from app.services.query import QueryService
from app.services.query_template import (
    parse_fields,
    parse_order_rules,
    parse_template,
    project_rows,
    read_source_rows,
    sort_rows,
)
from app.store import store

MODULE_RUN = "query_result"
MODULE_LEDGER = "query_field_ledger"
MODULE_SOURCE = "query_source"
DATASOURCE_MODULE = "datasource"
QUERY_MODULE = "query"

RUN_STATUS_DONE = "已完成"
RUN_STATUS_FAILED = "失败"
# 执行入口的参数包装字段别名：前端、脚本和下游系统各有各的写法。
PARAM_ALIASES = ("parameters", "params", "参数", "参数值", "query_params")
REQUEST_KEY_ALIASES = ("request_id", "requestId", "idempotency_key", "idempotencyKey", "请求编号")
# 执行动作的写法在不同调用方之间不完全一致，统一归一到“执行查询 / 重新执行”。
EXECUTE_ACTIONS = {"执行查询", "运行查询", "执行", "查询执行", "run", "execute"}
RERUN_ACTIONS = {"重新执行", "重跑", "重新查询", "rerun", "reexecute"}
ARBITRATION = {
    "参数": ["本次执行显式参数", "查询定义参数默认值", "查询模板参数占位"],
    "结果字段": ["查询定义结果字段", "查询模板SELECT投影", "源记录全部字段"],
    "翻页": ["本次运行参数/字段/排序快照", "查询定义当前值"],
    "重复提交": ["显式幂等键", "定义+参数快照+定义版本指纹"],
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _version(entry: dict[str, Any], snake: str, label: str) -> int:
    raw = entry.get(snake, entry.get(label, 1))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_payload(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def _param_defaults(entry: dict[str, Any]) -> dict[str, Any]:
    raw = _json_payload(entry.get("参数默认值", entry.get("参数默认", {})))
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    defaults: dict[str, Any] = {}
    for item in re.split(r"[,，;；]+", str(raw or "")):
        if not item.strip():
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            defaults[key.strip()] = value.strip()
    return defaults


def _entry_sort_rules(entry: dict[str, Any], plan) -> list[tuple[str, str]]:
    raw = entry.get("排序规则")
    rules = parse_order_rules(raw)
    if not rules:
        rules = list(plan.order_rules)
    if not rules:
        # 没有显式排序规则时按源记录顺序，保证翻页稳定。
        rules = [("__source_order__", "ASC")]
    return rules


def _sort_text(rules: list[tuple[str, str]]) -> str:
    return ", ".join(f"{name} {direction}" for name, direction in rules)


def _sort_snapshot(rules: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"字段": name, "方向": direction} for name, direction in rules]


def _source_rows(datasource_no: str) -> list[dict[str, Any]]:
    """从数据源连接里读取源记录；兼容 query_source 独立源记录表。"""
    rows: list[dict[str, Any]] = []
    for connection in store.rows(DATASOURCE_MODULE):
        if str(connection.get("连接编号", "")) != str(datasource_no):
            continue
        source = connection.get("源记录") or connection.get("记录") or []
        if isinstance(source, list):
            rows.extend(dict(item) for item in source if isinstance(item, dict))
    if rows:
        return rows
    for item in store.rows(MODULE_SOURCE):
        if str(item.get("数据源编号", item.get("关联数据源", ""))) == str(datasource_no):
            rows.append(dict(item))
    return rows


def _infer_type(value: Any) -> str:
    if value is None:
        return "未知"
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, (int, float)):
        return "数值"
    text = str(value)
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return "数值"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T].*)?", text):
        return "日期"
    return "文本"


def _fingerprint(
    entry_id: int,
    params: dict[str, Any],
    entry: dict[str, Any],
    template: str,
    fields: list[str],
    rules: list[tuple[str, str]],
) -> str:
    payload = {
        "entry_id": entry_id,
        "params": params,
        "revision": entry.get("revision", entry.get("版本号", 1)),
        "param_version": _version(entry, "param_version", "参数版本"),
        "sort_version": _version(entry, "sort_version", "排序版本"),
        "field_version": _version(entry, "field_version", "字段版本"),
        "template": template,
        "fields": fields,
        "sort": rules,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class QueryResultService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._fingerprints: dict[str, str] = {}
        self._run_seq = 0
        self._ledger_seq = 0
        self.query_service = QueryService()

    # ---------------------------------------------------------------- 执行
    def execute(
        self,
        entry_id: int | str,
        values: dict[str, Any] | None = None,
        *,
        parameters: dict[str, Any] | None = None,
        request_id: str | None = None,
        force: bool = False,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """执行一次查询，返回 (运行结果, 说明, 是否重复提交)。

        entry_id 既可以是数字 id，也可以是定义编号（如 QUER-0003）。
        """
        values = dict(values or {})
        entry = self.query_service.resolve(entry_id)
        if entry is None:
            return None, f"查询定义 {entry_id} 不存在或已归档", False
        resolved_id = int(entry.get("id", 0))
        if entry.get("status") == "已停用":
            return None, "查询定义已停用，不能执行查询", False

        template = str(entry.get("查询模板") or "").strip()
        if not template:
            return None, "查询定义未配置查询模板，无法解析参数占位", False

        plan = parse_template(template, entry.get("参数占位"))
        defaults = _param_defaults(entry)
        explicit: dict[str, Any] = {}
        if isinstance(parameters, dict):
            explicit.update(parameters)
        for key in PARAM_ALIASES:
            nested = _json_payload(values.get(key))
            if isinstance(nested, dict):
                explicit.update(nested)
        reserved = {
            "action",
            "request_id",
            "requestId",
            "idempotency_key",
            "idempotencyKey",
            "force",
            "run_id",
            "definition_no",
            "定义编号",
            "definition",
            "entry_id",
            *PARAM_ALIASES,
        }
        for key, value in values.items():
            if key in reserved:
                continue
            if key in plan.placeholders or key in defaults:
                explicit[str(key)] = value
        # 显式参数 > 定义默认值；空串和 None 等同于没给，回落到默认值，
        # 避免把空条件当成“匹配所有记录”。
        explicit = {
            key: value
            for key, value in explicit.items()
            if value is not None and str(value).strip() != ""
        }
        params = {**defaults, **explicit}
        missing = [
            name
            for name in plan.placeholders
            if name not in params or params.get(name) is None or str(params.get(name)).strip() == ""
        ]
        for name in missing:
            params.pop(name, None)
        if missing:
            return None, f"缺少查询参数：{'、'.join(missing)}", False

        idempotency_key = str(
            request_id
            or next((values.get(key) for key in REQUEST_KEY_ALIASES if values.get(key)), "")
            or entry.get("幂等键")
            or ""
        ).strip()
        if str(values.get("action") or "").strip() in RERUN_ACTIONS:
            force = True

        fields = parse_fields(entry.get("结果字段"))
        field_source = "查询定义结果字段"
        if not fields:
            fields = list(plan.select_fields)
            field_source = "查询模板SELECT投影"
        rules = _entry_sort_rules(entry, plan)
        fingerprint = _fingerprint(resolved_id, params, entry, template, fields, rules)

        with self._lock:
            if not force:
                existing_id = ""
                if idempotency_key and idempotency_key in self._fingerprints:
                    existing_id = self._fingerprints[idempotency_key]
                elif fingerprint in self._fingerprints:
                    existing_id = self._fingerprints[fingerprint]
                if existing_id and existing_id in self._by_id:
                    existing = self._by_id[existing_id]
                    if idempotency_key and idempotency_key not in self._fingerprints:
                        self._fingerprints[idempotency_key] = existing_id
                    return self._public_run(existing), f"重复提交已复用查询结果 {existing_id}", True

            source_rows = _source_rows(str(entry.get("关联数据源") or ""))
            if not source_rows:
                return None, f"数据源 {entry.get('关联数据源') or '未配置'} 中没有可读取的源记录", False

            selected, missing_params = read_source_rows(source_rows, plan, params)
            if missing_params:
                return None, f"缺少查询参数：{'、'.join(missing_params)}", False
            sorted_rows = sort_rows(selected, rules)

            if not fields:
                fields = [
                    key
                    for key in (sorted_rows[0].keys() if sorted_rows else [])
                    if not str(key).startswith("__")
                ]
                field_source = "源记录全部字段"

            projected = project_rows(sorted_rows, fields)
            self._run_seq += 1
            run_id = f"RUN-{self._run_seq:06d}"
            self._ledger_seq += 1
            ledger_no = f"QFLD-{self._ledger_seq:06d}"
            created_at = _now()

            records: list[dict[str, Any]] = []
            for index, row in enumerate(projected, start=1):
                source_row = sorted_rows[index - 1]
                records.append({
                    "记录编号": f"{run_id}-R{index:04d}",
                    "源记录编号": source_row.get("源记录编号")
                    or source_row.get("记录编号")
                    or source_row.get("id"),
                    "源记录顺序": source_row.get("__source_order__"),
                    "字段": row,
                })

            page_size = _as_int(entry.get("分页大小"), 20)
            if page_size <= 0:
                page_size = 20
            ledger: list[dict[str, Any]] = []
            for order, name in enumerate(fields, start=1):
                sample = next(
                    (row["字段"].get(name) for row in records if row["字段"].get(name) is not None),
                    None,
                )
                ledger.append({
                    "id": self._ledger_seq * 1000 + order,
                    "台账编号": f"{ledger_no}-{order:02d}",
                    "run_id": run_id,
                    "定义编号": str(entry.get("定义编号") or ""),
                    "字段序号": order,
                    "字段名称": name,
                    "字段类型": _infer_type(sample),
                    "字段来源": field_source,
                    "是否投影": True,
                    "字段版本": _version(entry, "field_version", "字段版本"),
                    "创建时间": created_at,
                })

            run = {
                "run_id": run_id,
                "id": self._run_seq,
                "定义编号": str(entry.get("定义编号") or ""),
                "entry_id": resolved_id,
                "status": RUN_STATUS_DONE,
                "关联数据源": str(entry.get("关联数据源") or ""),
                "参数快照": dict(params),
                "结果字段": list(fields),
                "结果字段快照": list(fields),
                "排序规则": _sort_text(rules),
                "排序快照": _sort_snapshot(rules),
                "参数版本": _version(entry, "param_version", "参数版本"),
                "排序版本": _version(entry, "sort_version", "排序版本"),
                "字段版本": _version(entry, "field_version", "字段版本"),
                "定义版本": _as_int(entry.get("revision", entry.get("版本号", 1)), 1),
                "分页大小": page_size,
                "查询模板快照": template,
                "源记录数": len(source_rows),
                "匹配记录数": len(selected),
                "记录数": len(records),
                "total": len(records),
                "创建时间": created_at,
                "执行时间": created_at,
                "口径仲裁": dict(ARBITRATION),
                "幂等键": idempotency_key,
                "幂等指纹": fingerprint,
                "记录": records,
                "字段台账": ledger,
                "审计": [{
                    "时间": created_at,
                    "动作": "执行查询",
                    "说明": f"按参数快照 {json.dumps(params, ensure_ascii=False, sort_keys=True)} 读取 {len(source_rows)} 条源记录，生成 {len(records)} 条结果记录",
                }],
            }
            self._runs.append(run)
            self._by_id[run_id] = run
            self._fingerprints[fingerprint] = run_id
            if idempotency_key:
                self._fingerprints[idempotency_key] = run_id
            self._write_ledger(ledger)
            self._write_dataset(run)
            return self._public_run(run), f"查询已执行，生成查询结果 {run_id}", False

    def _write_ledger(self, ledger: list[dict[str, Any]]) -> None:
        """字段台账一次运行写一批，之后的运行不会改写历史台账。"""
        rows = store.rows(MODULE_LEDGER)
        rows.extend(dict(item) for item in ledger)

    def _write_dataset(self, run: dict[str, Any]) -> None:
        """把查询结果数据集落到结果表：字段台账、参数快照与结果记录同源。"""
        rows = store.rows(MODULE_RUN)
        rows.append({
            "id": run["id"],
            "run_id": run["run_id"],
            "定义编号": run["定义编号"],
            "entry_id": run["entry_id"],
            "status": run["status"],
            "参数快照": dict(run["参数快照"]),
            "结果字段": list(run["结果字段"]),
            "排序规则": run["排序规则"],
            "排序快照": [dict(item) for item in run["排序快照"]],
            "记录数": run["记录数"],
            "记录": [dict(item) for item in run["记录"]],
            "字段台账": [dict(item) for item in run["字段台账"]],
            "创建时间": run["创建时间"],
        })

    def dataset(self, run_id: str) -> dict[str, Any] | None:
        """查询结果数据集：结果记录、字段清单与参数快照放在同一份复核视图里。"""
        resolved = self.resolve_run(run_id)
        run = self._by_id.get(resolved["run_id"]) if resolved else None
        if run is None:
            return None
        return {
            "run_id": run["run_id"],
            "定义编号": run["定义编号"],
            "status": run["status"],
            "参数快照": dict(run["参数快照"]),
            "结果字段": list(run["结果字段"]),
            "排序规则": run["排序规则"],
            "排序快照": [dict(item) for item in run["排序快照"]],
            "参数版本": run["参数版本"],
            "排序版本": run["排序版本"],
            "字段版本": run["字段版本"],
            "分页大小": run["分页大小"],
            "记录数": run["记录数"],
            "记录": [
                {"记录编号": item["记录编号"], **dict(item["字段"])}
                for item in run["记录"]
            ],
            "字段台账": [dict(item) for item in run["字段台账"]],
            "快照锁定": True,
            "口径仲裁": dict(ARBITRATION),
        }

    # ------------------------------------------------------------ 结果清单
    def list_runs(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        definition_no: str | None = None,
        entry_id: int | None = None,
        sort: str | None = None,
        order: str = "asc",
        page: int = 1,
        size: int = 20,
        as_of: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        snapshot = as_of if as_of is not None else max(
            (int(row.get("id", 0)) for row in self._runs), default=0
        )
        rows = [row for row in self._runs if int(row.get("id", 0)) <= snapshot]
        target = (definition_no or keyword or "").strip()
        if target:
            rows = [
                row for row in rows
                if target in str(row.get("定义编号", "")) or target in str(row.get("run_id", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("entry_id", 0)) == entry_id]
        sort_key = (sort or "创建时间").strip()
        reverse = str(order).lower() in ("desc", "降序", "倒序")
        field_map = {
            "定义编号": lambda row: str(row.get("定义编号", "")),
            "run_id": lambda row: str(row.get("run_id", "")),
            "运行编号": lambda row: str(row.get("run_id", "")),
            "status": lambda row: str(row.get("status", "")),
            "状态": lambda row: str(row.get("status", "")),
            "创建时间": lambda row: str(row.get("创建时间", "")),
            "记录数": lambda row: int(row.get("记录数", 0)),
        }
        key = field_map.get(sort_key, field_map["创建时间"])
        # 排序键后追加 run_id，保证翻页边界稳定、不会因同值记录换位重复。
        rows.sort(key=lambda row: (key(row), int(row.get("id", 0))), reverse=reverse)
        total = len(rows)
        page = max(page, 1)
        size = max(size, 1)
        start = (page - 1) * size
        return [self._public_run(row) for row in rows[start:start + size]], total, snapshot

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._by_id.get(str(run_id))
        return self._public_run(run) if run else None

    def latest_run(self, entry_ref: int | str) -> dict[str, Any] | None:
        entry = self.query_service.resolve(entry_ref)
        if entry is None:
            return None
        entry_id = int(entry.get("id", 0))
        for run in reversed(self._runs):
            if int(run.get("entry_id", 0)) == entry_id:
                return self._public_run(run)
        return None

    def resolve_run(self, run_ref: str) -> dict[str, Any] | None:
        """结果入口支持 run_id、结果记录 id、定义编号。

        数字优先按结果记录 id 解释，避免和查询定义 id 混淆；找不到再按定义编号
        取该定义最近一次运行。
        """
        text = str(run_ref or "").strip()
        if text in self._by_id:
            return self._public_run(self._by_id[text])
        if text.isdigit():
            for run in self._runs:
                if int(run.get("id", 0)) == int(text):
                    return self._public_run(run)
        return self.latest_run(text)

    def records_page(
        self, run_id: str, *, page: int = 1, size: int | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        resolved = self.resolve_run(run_id)
        run = self._by_id.get(resolved["run_id"]) if resolved else None
        if run is None:
            return None, f"查询结果 {run_id} 不存在或已归档"
        page = max(_as_int(page, 1), 1)
        page = max(page, 1)
        page_size = _as_int(size if size is not None else run.get("分页大小"), 20)
        if page_size <= 0:
            page_size = 20
        if page_size > 200:
            page_size = 200
        start = (page - 1) * page_size
        items = [dict(item["字段"]) for item in run["记录"][start:start + page_size]]
        record_ids = [item["记录编号"] for item in run["记录"][start:start + page_size]]
        source_ids = [item["源记录编号"] for item in run["记录"][start:start + page_size]]
        return {
            "items": items,
            "total": run["记录数"],
            "page": page,
            "size": page_size,
            "run_id": run["run_id"],
            "定义编号": run["定义编号"],
            "status": run["status"],
            "参数快照": dict(run["参数快照"]),
            "结果字段": list(run["结果字段"]),
            "排序规则": run["排序规则"],
            "排序快照": [dict(item) for item in run["排序快照"]],
            "记录编号": record_ids,
            "源记录编号": source_ids,
            "参数版本": run["参数版本"],
            "排序版本": run["排序版本"],
            "字段版本": run["字段版本"],
            "快照锁定": True,
            "口径仲裁": dict(ARBITRATION),
        }, ""

    # ------------------------------------------------------------ 字段台账
    def list_ledger(
        self,
        *,
        definition_no: str | None = None,
        run_id: str | None = None,
        field_name: str | None = None,
        page: int = 1,
        size: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(MODULE_LEDGER))
        if definition_no:
            rows = [row for row in rows if definition_no in str(row.get("定义编号", ""))]
        if run_id:
            resolved = self.resolve_run(run_id)
            target = resolved["run_id"] if resolved else str(run_id)
            rows = [row for row in rows if str(row.get("run_id", "")) == target]
        if field_name:
            rows = [row for row in rows if field_name in str(row.get("字段名称", ""))]
        total = len(rows)
        start = max(page - 1, 0) * max(size, 0)
        return [dict(row) for row in rows[start:start + size]], total

    def fields_for_run(self, run_id: str) -> dict[str, Any] | None:
        resolved = self.resolve_run(run_id)
        run = self._by_id.get(resolved["run_id"]) if resolved else None
        if run is None:
            return None
        return {
            "run_id": run["run_id"],
            "定义编号": run["定义编号"],
            "结果字段": list(run["结果字段"]),
            "字段清单": [dict(item) for item in run["字段台账"]],
            "字段来源": run["字段台账"][0]["字段来源"] if run["字段台账"] else "",
            "字段版本": run["字段版本"],
            "快照锁定": True,
            "口径仲裁": dict(ARBITRATION),
        }

    # -------------------------------------------------------------- 复核
    def verify(self, run_id: str) -> tuple[dict[str, Any] | None, str]:
        resolved = self.resolve_run(run_id)
        run = self._by_id.get(resolved["run_id"]) if resolved else None
        if run is None:
            return None, f"查询结果 {run_id} 不存在或已归档"
        entry = store.find(QUERY_MODULE, int(run["entry_id"]))
        snapshot_fields = list(run["结果字段"])
        snapshot_params = dict(run["参数快照"])
        snapshot_sort = run["排序规则"]

        current_fields: list[str] = []
        current_placeholders: list[str] = []
        current_sort = ""
        current_params: dict[str, Any] = {}
        if entry is not None:
            current_fields = parse_fields(entry.get("结果字段"))
            plan = parse_template(str(entry.get("查询模板") or ""), entry.get("参数占位"))
            current_placeholders = plan.placeholders
            current_sort = _sort_text(_entry_sort_rules(entry, plan))
            current_params = _param_defaults(entry)
            if not current_fields:
                current_fields = list(plan.select_fields)

        drift: list[str] = []
        if entry is None:
            drift.append("查询定义已不存在，翻页继续沿用运行快照")
        else:
            if current_fields and current_fields != snapshot_fields:
                drift.append(f"结果字段已漂移：快照 {snapshot_fields} → 当前 {current_fields}")
            if current_placeholders and sorted(current_placeholders) != sorted(snapshot_params):
                drift.append(f"参数占位已漂移：快照 {sorted(snapshot_params)} → 当前 {sorted(current_placeholders)}")
            if current_sort and current_sort != snapshot_sort:
                drift.append(f"排序规则已漂移：快照 {snapshot_sort} → 当前 {current_sort}")
            changed_params = {
                key: {"快照": value, "当前": current_params.get(key)}
                for key, value in snapshot_params.items()
                if key in current_params and current_params.get(key) != value
            }
            if changed_params:
                drift.append(
                    "参数默认值已漂移："
                    + "、".join(
                        f"{key} {value['快照']} → {value['当前']}"
                        for key, value in changed_params.items()
                    )
                )

        seen_ids: set[str] = set()
        duplicates: list[str] = []
        field_mismatch: list[str] = []
        for record in run["记录"]:
            if record["记录编号"] in seen_ids:
                duplicates.append(record["记录编号"])
            seen_ids.add(record["记录编号"])
            if list(record["字段"].keys()) != snapshot_fields:
                field_mismatch.append(record["记录编号"])

        ledger_fields = [
            item["字段名称"] for item in run["字段台账"]
        ]
        ledger_consistent = ledger_fields == snapshot_fields
        if not ledger_consistent:
            drift.append("查询字段台账与结果字段快照不一致")

        page_consistent = not field_mismatch and not duplicates
        ok = page_consistent and ledger_consistent
        pages = (run["记录数"] + max(run["分页大小"], 1) - 1) // max(run["分页大小"], 1)
        return {
            "ok": ok,
            "run_id": run["run_id"],
            "定义编号": run["定义编号"],
            "参数快照": snapshot_params,
            "结果字段": snapshot_fields,
            "排序规则": snapshot_sort,
            "当前结果字段": current_fields,
            "当前参数占位": current_placeholders,
            "当前排序规则": current_sort,
            "当前参数默认值": current_params,
            "口径漂移": drift,
            "定义已变更": bool(drift),
            "按当前定义复核": not drift,
            "快照可用": page_consistent,
            "分页记录一致": page_consistent,
            "字段台账一致": ledger_consistent,
            "字段不匹配记录": field_mismatch,
            "重复记录编号": duplicates,
            "记录数": run["记录数"],
            "分页大小": run["分页大小"],
            "分页数": pages,
            "复核结论": "结果字段与分页记录可按查询定义快照复核" if ok else "查询结果快照存在不一致，需重新执行",
            "口径仲裁": dict(ARBITRATION),
            "审计": [dict(item) for item in run["审计"]],
        }, ""

    # -------------------------------------------------------------- 输出
    def _public_run(self, run: dict[str, Any]) -> dict[str, Any]:
        return {
            key: (list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value)
            for key, value in run.items()
            if key != "记录"
        } | {
            "结果字段": list(run["结果字段"]),
            "参数快照": dict(run["参数快照"]),
            "排序快照": [dict(item) for item in run["排序快照"]],
            "字段台账": [dict(item) for item in run["字段台账"]],
            # 同义字段：调用方对“状态 / 字段清单 / 参数”的命名习惯不同。
            "状态": run["status"],
            "字段清单": list(run["结果字段"]),
            "参数": dict(run["参数快照"]),
            "记录总数": run["记录数"],
        }

    def records(self, run_id: str) -> list[dict[str, Any]]:
        run = self._by_id.get(str(run_id))
        return [dict(item["字段"]) for item in run["记录"]] if run else []


result_service = QueryResultService()
