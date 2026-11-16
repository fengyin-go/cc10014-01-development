"""查询模板解析：把模板里的参数占位、WHERE 条件、排序规则拆出来。

只实现数据分析场景常用的 SELECT ... FROM ... WHERE ... ORDER BY ... LIMIT
子集，够用来做参数绑定、源记录读取和结果字段投影；不引入 SQL 引擎，避免把
示例项目变成数据库客户端。模板无法解析时退化为“参数等于源记录同名字段”的
过滤方式，保证仍然能按参数读取。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(?P<double>[A-Za-z_][\w.\-一-鿿]*)\s*\}\}"
    r"|\$\{\s*(?P<dollar>[A-Za-z_][\w.\-一-鿿]*)\s*\}"
    r"|(?<![:{])\{(?P<single>[A-Za-z_][\w.\-一-鿿]*)\}"
    r"|(?<![:\w]):(?P<colon>[A-Za-z_][\w.]*)"
)
# 字段名既可能是英文列名，也可能是中文列名（如 状态、金额），首字符两类都允许。
IDENTIFIER = r"[A-Za-z_一-鿿][\w.\-一-鿿]*"
CONDITION_RE = re.compile(
    rf"^\s*(?P<field>{IDENTIFIER})\s*"
    r"(?P<op><=|>=|<>|!=|=|<|>|\bLIKE\b|\bIN\b)\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
FROM_RE = re.compile(rf"\bFROM\s+([`\"\[]?[\w.\-一-鿿]+[`\"\]]?)", re.IGNORECASE)
SELECT_RE = re.compile(r"\bSELECT\s+(.*?)\s+\bFROM\b", re.IGNORECASE | re.DOTALL)
WHERE_RE = re.compile(
    r"\bWHERE\b(.*?)(?:\bORDER\s+BY\b|\bLIMIT\b|$)", re.IGNORECASE | re.DOTALL
)
ORDER_RE = re.compile(
    r"\bORDER\s+BY\b(.*?)(?:\bLIMIT\b|$)", re.IGNORECASE | re.DOTALL
)
LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)


@dataclass
class Condition:
    field: str
    op: str
    token: str
    placeholder: str | None = None

    @property
    def direction(self) -> str:
        return self.op.upper()


@dataclass
class TemplatePlan:
    raw: str
    placeholders: list[str] = field(default_factory=list)
    table: str | None = None
    select_fields: list[str] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    order_rules: list[tuple[str, str]] = field(default_factory=list)
    limit: int | None = None


def _strip_identifier(value: str) -> str:
    return value.strip().strip("`\"[]")


def split_placeholders(value: Any) -> list[str]:
    """把“参数占位”字段拆成参数名列表，兼容中英文逗号和分号。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[,，;；、\s]+", str(value))
    result: list[str] = []
    for item in raw_items:
        name = item.strip()
        if name.startswith("{{") and name.endswith("}}"):
            name = name[2:-2].strip()
        elif name.startswith("${") and name.endswith("}"):
            name = name[2:-1].strip()
        elif name.startswith(":") or name.startswith("{") or name.endswith("}"):
            name = name.strip(":{} ")
        if name and name not in result:
            result.append(name)
    return result


def parse_fields(value: Any) -> list[str]:
    """结果字段支持列表或逗号/分号分隔的字符串；不会把字段名里的空格拆开。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[,，;；、]+", str(value))
    result: list[str] = []
    for item in raw_items:
        name = item.strip()
        if not name:
            continue
        # 兼容 SELECT 投影里的 “金额 AS amount”，对外仍使用别名。
        alias = re.split(r"\s+AS\s+", name, flags=re.IGNORECASE)
        name = alias[-1].strip().strip('`"[]') if len(alias) > 1 else name
        if name and name != "*" and name not in result:
            result.append(name)
    return result


def find_placeholders(template: str) -> list[str]:
    result: list[str] = []
    for match in PLACEHOLDER_RE.finditer(template or ""):
        name = next(value for value in match.groupdict().values() if value)
        if name not in result:
            result.append(name)
    return result


def _split_conditions(clause: str) -> list[str]:
    """按 AND 拆分条件，忽略单双引号里的 AND。"""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    tokens = re.split(r"(\s+AND\s+)", clause, flags=re.IGNORECASE)
    for token in tokens:
        for char in token:
            if char in ("'", '"'):
                quote = None if quote == char else (char if quote is None else quote)
        if quote is None and re.fullmatch(r"\s+AND\s+", token, flags=re.IGNORECASE):
            parts.append("".join(current))
            current = []
        else:
            current.append(token)
    if current:
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _condition_from_text(text: str) -> Condition | None:
    match = CONDITION_RE.match(text)
    if not match:
        return None
    field_name = _strip_identifier(match.group("field"))
    op = match.group("op").upper()
    token = match.group("value").strip()
    placeholder: str | None = None
    placeholder_match = PLACEHOLDER_RE.fullmatch(token)
    if placeholder_match:
        placeholder = next(value for value in placeholder_match.groupdict().values() if value)
    elif token == "?":
        placeholder = "?"
    return Condition(field=field_name, op=op, token=token, placeholder=placeholder)


def parse_order_rules(text: Any) -> list[tuple[str, str]]:
    """解析“金额 DESC, 下单时间 ASC”，返回 [(字段, ASC/DESC)]。"""
    if text is None:
        return []
    if isinstance(text, (list, tuple)):
        raw = ", ".join(str(item) for item in text)
    else:
        raw = str(text)
    raw = re.sub(r"^\s*ORDER\s+BY\s+", "", raw, flags=re.IGNORECASE)
    rules: list[tuple[str, str]] = []
    for chunk in re.split(r"[,，;；、]+", raw):
        item = chunk.strip()
        if not item:
            continue
        parts = item.split()
        field_name = _strip_identifier(parts[0])
        direction = "ASC"
        if len(parts) > 1 and parts[-1].upper() in ("ASC", "DESC"):
            direction = parts[-1].upper()
        if field_name:
            rules.append((field_name, direction))
    return rules


def parse_template(template: str, declared_placeholders: Any = None) -> TemplatePlan:
    raw = str(template or "")
    placeholders = find_placeholders(raw)
    for name in split_placeholders(declared_placeholders):
        if name not in placeholders:
            placeholders.append(name)
    plan = TemplatePlan(raw=raw, placeholders=placeholders)

    from_match = FROM_RE.search(raw)
    if from_match:
        plan.table = _strip_identifier(from_match.group(1))

    select_match = SELECT_RE.search(raw)
    if select_match:
        plan.select_fields = parse_fields(select_match.group(1))

    where_match = WHERE_RE.search(raw)
    if where_match:
        for text in _split_conditions(where_match.group(1)):
            condition = _condition_from_text(text)
            if condition is not None:
                plan.conditions.append(condition)

    order_match = ORDER_RE.search(raw)
    if order_match:
        plan.order_rules = parse_order_rules(order_match.group(1))

    limit_match = LIMIT_RE.search(raw)
    if limit_match:
        plan.limit = int(limit_match.group(1))
    return plan


def _parameter_value(token: str, params: dict[str, Any]) -> tuple[bool, Any]:
    placeholder_match = PLACEHOLDER_RE.fullmatch(token)
    if placeholder_match:
        name = next(value for value in placeholder_match.groupdict().values() if value)
        if name not in params:
            return False, None
        return True, params[name]
    if token == "?":
        return False, None
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return True, token[1:-1]
    if token.upper() == "NULL":
        return True, None
    return True, token


def _to_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "LIKE":
        pattern = re.escape(str(right)).replace(r"\%", ".*").replace("%", ".*").replace("_", ".")
        return re.fullmatch(pattern, str(left or ""), flags=re.IGNORECASE) is not None
    if op == "IN":
        values = [item.strip().strip("'\"") for item in str(right).strip("()[]").split(",")]
        return str(left) in values or str(left) in {str(item) for item in values}
    left_number = _to_number(left)
    right_number = _to_number(right)
    if left_number is not None and right_number is not None:
        left_value: Any = left_number
        right_value: Any = right_number
    else:
        left_value = "" if left is None else str(left)
        right_value = "" if right is None else str(right)
    if op == "=":
        return left_value == right_value
    if op in ("!=", "<>"):
        return left_value != right_value
    if op == ">":
        return left_value > right_value
    if op == ">=":
        return left_value >= right_value
    if op == "<":
        return left_value < right_value
    if op == "<=":
        return left_value <= right_value
    return False


def bind_conditions(
    plan: TemplatePlan, params: dict[str, Any]
) -> tuple[list[tuple[str, str, Any]], list[str]]:
    """把条件里的占位替换成参数值，返回 (已绑定条件, 缺少的参数)。"""
    bound: list[tuple[str, str, Any]] = []
    missing: list[str] = []
    positional = 0
    for condition in plan.conditions:
        token = condition.token
        if token == "?":
            name = plan.placeholders[positional] if positional < len(plan.placeholders) else None
            positional += 1
            if name is None or name not in params:
                if name:
                    missing.append(name)
                continue
            bound.append((condition.field, condition.op, params[name]))
            continue
        ok, value = _parameter_value(token, params)
        if not ok:
            if condition.placeholder and condition.placeholder not in missing:
                missing.append(condition.placeholder)
            continue
        bound.append((condition.field, condition.op, value))
    return bound, missing


def _value_for_sort(row: dict[str, Any], field_name: str) -> tuple[int, Any]:
    value = row.get(field_name)
    if value is None:
        return (2, "")
    number = _to_number(value)
    if number is not None:
        return (0, number)
    return (1, str(value))


def sort_rows(
    rows: Iterable[dict[str, Any]], rules: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """多列稳定排序；先按源记录顺序兜底，翻页不会因同值记录换位而重复。"""
    ordered = list(rows)
    if not rules:
        return ordered
    # 兜底键：源记录位置，保证相同排序值的记录顺序固定。
    ordered = sorted(ordered, key=lambda row: int(row.get("__source_order__", 0)))
    for field_name, direction in reversed(rules):
        ordered.sort(
            key=lambda row, name=field_name: _value_for_sort(row, name),
            reverse=direction.upper() == "DESC",
        )
    return ordered


def read_source_rows(
    rows: Iterable[dict[str, Any]],
    plan: TemplatePlan,
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """按模板 WHERE 条件读取源记录；模板没有条件时按同名参数过滤。"""
    prepared: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        item = dict(row)
        item["__source_order__"] = index
        prepared.append(item)

    bound, missing = bind_conditions(plan, params)
    if missing:
        return [], missing

    if bound:
        selected = [
            row
            for row in prepared
            if all(
                _compare(row.get(field_name), op, value)
                for field_name, op, value in bound
            )
        ]
    else:
        # 模板没有可解析的 WHERE 时，参数名与源记录字段同名则作为等值过滤，
        # 这样 “WHERE 条件写在查询模板里” 和 “只给参数占位” 两种定义都能执行。
        filters = {
            name: value
            for name, value in params.items()
            if name in plan.placeholders and any(name in row for row in prepared)
        }
        selected = [
            row
            for row in prepared
            if all(str(row.get(name)) == str(value) for name, value in filters.items())
        ] if filters else prepared

    if plan.limit is not None:
        selected = selected[: plan.limit]
    return selected, []


def project_rows(
    rows: Iterable[dict[str, Any]], fields: list[str]
) -> list[dict[str, Any]]:
    selected_fields = [name for name in fields if name and name != "*"]
    projected: list[dict[str, Any]] = []
    for row in rows:
        if selected_fields:
            projected.append({name: row.get(name) for name in selected_fields})
        else:
            projected.append({key: value for key, value in row.items() if not key.startswith("__")})
    return projected
