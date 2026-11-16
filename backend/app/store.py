"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS
from app.seed_query import source_rows

# 查询执行链路的内部表：可以由 rows() 读取，但不计入业务模块清单和运营概览，
# 避免把“查询结果 / 字段台账 / 源记录”当成新的业务模块展示。
INTERNAL_MODULES = {"query_result", "query_field_ledger", "query_source"}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 查询执行链路的数据：源记录、查询结果与查询字段台账。
        # 源记录单独存表，避免把明细数组塞进数据源列表响应当中。
        self._tables["query_source"] = source_rows()
        self._tables.setdefault("query_result", [])
        self._tables.setdefault("query_field_ledger", [])

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
