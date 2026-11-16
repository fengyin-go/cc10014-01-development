"""数据查询业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "query"
REQUIRED_FIELDS = ["定义编号", "关联数据源", "查询模板"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用查询": "已启用", "停用查询": "已停用"}
NEGATIVE_ACTIONS = ["停用查询"]

# 修改查询定义时，按改动的字段推进对应版本；一次执行会冻结当时的版本与口径。
PARAM_FIELDS = ["查询模板", "参数占位", "参数默认值", "参数默认"]
SORT_FIELDS = ["排序规则"]
FIELD_FIELDS = ["结果字段"]


class QueryService:
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
            rows = [row for row in rows if keyword in str(row.get("定义编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        page = max(page, 1)
        size = max(size, 1)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_definition(self, definition_no: str) -> dict[str, Any] | None:
        """按定义编号（如 QUER-0003）定位查询定义，供执行与复核入口使用。"""
        target = str(definition_no or "").strip()
        if not target:
            return None
        for row in store.rows(MODULE):
            if str(row.get("定义编号", "")) == target:
                return row
        return None

    def resolve(self, entry_ref: str | int) -> dict[str, Any] | None:
        """入口参数既可以是数字 id，也可以是定义编号，先按 id 再按编号查找。"""
        text = str(entry_ref or "").strip()
        if text.isdigit():
            found = store.find(MODULE, int(text))
            if found is not None:
                return found
        return self.find_by_definition(text)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 查询执行需要的可选字段：参数默认值、排序规则、结果字段、分页大小。
        for field in ("参数默认值", "排序规则", "结果字段", "分页大小", "超时秒数"):
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["revision"] = 1
        entry["param_version"] = 1
        entry["sort_version"] = 1
        entry["field_version"] = 1
        rows.append(entry)
        return entry, []

    def update_entry(
        self, entry_id: int | str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, list[str]]:
        """修改查询定义并推进版本号；返回 (定义, 说明, 发生变化的口径)。"""
        entry = self.resolve(entry_id)
        if entry is None:
            return None, f"查询定义 {entry_id} 不存在或已归档", []
        changed: list[str] = []
        for key, value in values.items():
            if key in ("id", "status", "pending", "abnormal", "action"):
                continue
            if key.startswith("_"):
                continue
            if entry.get(key) != value:
                entry[key] = value
                if key not in changed:
                    changed.append(key)
        if not changed:
            return entry, "查询定义没有变化", []
        entry["revision"] = int(entry.get("revision", entry.get("版本号", 1)) or 1) + 1
        if any(field in changed for field in PARAM_FIELDS):
            entry["param_version"] = int(entry.get("param_version", 1) or 1) + 1
        if any(field in changed for field in SORT_FIELDS):
            entry["sort_version"] = int(entry.get("sort_version", 1) or 1) + 1
        if any(field in changed for field in FIELD_FIELDS):
            entry["field_version"] = int(entry.get("field_version", 1) or 1) + 1
        entry["pending"] = True
        return entry, f"查询定义已更新，版本推进到 v{entry['revision']}", changed

    def run_action(self, entry_id: int | str, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.resolve(entry_id)
        if entry is None:
            return None, f"查询定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据查询可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"查询定义已{action}"
