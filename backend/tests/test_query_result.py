"""查询执行链路的验收用例：参数绑定、源记录读取、字段投影、快照分页与复核。"""
from __future__ import annotations

import threading
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.services.query_result import result_service
from app.store import store


class QueryResultTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都从干净的查询定义和结果表开始，避免互相污染。
        store._tables["query"] = [dict(row) for row in store._tables["query"][:3]]
        for row in store.rows("query"):
            row["revision"] = 1
            row["param_version"] = 1
            row["sort_version"] = 1
            row["field_version"] = 1
        store._tables["query_result"] = []
        store._tables["query_field_ledger"] = []
        result_service._runs = []
        result_service._by_id = {}
        result_service._fingerprints = {}
        result_service._run_seq = 0
        result_service._ledger_seq = 0
        self.client = TestClient(app)

    def execute(self, definition: str = "QUER-0003", parameters=None, **extra):
        values = {"定义编号": definition, **(parameters or {}), **extra}
        return self.client.post("/api/query-result/execute", json={"values": values}).json()

    def test_parameter_binding_reads_only_matching_source_rows(self) -> None:
        body = self.execute("QUER-0003", {"order_id": "ORD-1001"})
        self.assertTrue(body["ok"], body)
        entry = body["entry"]
        self.assertEqual(entry["参数快照"], {"order_id": "ORD-1001"})
        self.assertEqual(entry["源记录数"], 12)
        self.assertEqual(entry["匹配记录数"], 8)
        self.assertEqual(entry["结果字段"], ["金额", "状态"])

        other = self.execute("QUER-0003", {"order_id": "ORD-1002"})
        self.assertTrue(other["ok"], other)
        self.assertEqual(other["entry"]["匹配记录数"], 4)
        self.assertEqual(
            [item["字段名称"] for item in other["entry"]["字段台账"]], ["金额", "状态"]
        )
        page = self.client.get(
            f"/api/query-result/{other['entry']['run_id']}/page", params={"page": 1}
        ).json()
        self.assertTrue(all(set(item.keys()) == {"金额", "状态"} for item in page["items"]))

    def test_default_parameter_and_missing_parameter(self) -> None:
        body = self.execute("QUER-0003")
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["entry"]["参数快照"], {"order_id": "ORD-1001"})

        missing = self.client.post(
            "/api/query-result/execute",
            json={"values": {"定义编号": "QUER-0003", "order_id": "   "}},
        ).json()
        self.assertTrue(missing["ok"])
        missing_definition = self.client.post(
            "/api/query-result/execute",
            json={"定义编号": "QUER-0003", "parameters": {}},
        ).json()
        self.assertTrue(missing_definition["ok"])

    def test_pagination_never_repeats_records(self) -> None:
        run_id = self.execute()["entry"]["run_id"]
        seen: list[str] = []
        for page in (1, 2, 3):
            payload = self.client.get(
                f"/api/query-result/{run_id}/page", params={"page": page, "size": 3}
            ).json()
            self.assertEqual(payload["run_id"], run_id)
            self.assertEqual(len(payload["items"]), 3 if page < 3 else 2)
            seen.extend(payload["记录编号"])
        self.assertEqual(len(seen), 8)
        self.assertEqual(len(set(seen)), 8)

    def test_duplicate_submissions_only_take_effect_once(self) -> None:
        results: list[tuple[str, bool]] = []

        def submit() -> None:
            body = self.execute()
            results.append((body["entry"]["run_id"], body["entry"]["duplicated"]))

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 并发提交里只有一次真正执行，其余都复用同一条查询结果。
        self.assertEqual({run_id for run_id, _ in results}, {"RUN-000001"})
        self.assertEqual(sum(1 for _, duplicated in results if not duplicated), 1)
        self.assertEqual(sum(1 for _, duplicated in results if duplicated), len(results) - 1)
        listing = self.client.get(
            "/api/query-result", params={"definition_no": "QUER-0003"}
        ).json()
        self.assertEqual(listing["total"], 1)

    def test_paging_keeps_snapshot_after_definition_changes(self) -> None:
        run_id = self.execute()["entry"]["run_id"]
        first_page = self.client.get(
            f"/api/query-result/{run_id}/page", params={"page": 1, "size": 3}
        ).json()

        updated = self.client.patch(
            "/api/query/3",
            json={"values": {"排序规则": "金额 DESC", "参数默认值": {"order_id": "ORD-1002"}}},
        ).json()
        self.assertTrue(updated["ok"], updated)
        self.assertEqual(updated["entry"]["sort_version"], 2)
        self.assertEqual(updated["entry"]["param_version"], 2)

        # 旧运行继续按原参数快照和排序规则翻页。
        same_page = self.client.get(
            f"/api/query-result/{run_id}/page", params={"page": 1, "size": 3}
        ).json()
        self.assertEqual(same_page["参数快照"], {"order_id": "ORD-1001"})
        self.assertEqual(same_page["排序规则"], "下单时间 ASC")
        self.assertEqual(same_page["items"], first_page["items"])

        verification = self.client.post(f"/api/query-result/{run_id}/verify").json()
        self.assertTrue(verification["ok"])
        self.assertTrue(verification["分页记录一致"])
        self.assertTrue(any("排序规则已漂移" in item for item in verification["口径漂移"]))

        # 重新执行后才采用新的参数默认值与排序规则。
        rerun = self.client.post(
            "/api/query/3/actions", json={"values": {"action": "重新执行"}}
        ).json()
        self.assertTrue(rerun["ok"], rerun)
        self.assertNotEqual(rerun["entry"]["run_id"], run_id)
        self.assertEqual(rerun["entry"]["参数快照"], {"order_id": "ORD-1002"})
        self.assertEqual(rerun["entry"]["排序规则"], "金额 DESC")

        new_page = self.client.get(
            f"/api/query-result/{rerun['entry']['run_id']}/page",
            params={"page": 1, "size": 3},
        ).json()
        self.assertEqual([item["金额"] for item in new_page["items"]], [310.0, 199.0, 88.0])

    def test_field_ledger_is_shared_with_contract_and_export(self) -> None:
        run_id = self.execute()["entry"]["run_id"]
        ledger = self.client.get(
            "/api/query-field-ledger", params={"definition_no": "QUER-0003", "run_id": run_id}
        ).json()
        self.assertEqual([item["字段名称"] for item in ledger["items"]], ["金额", "状态"])
        self.assertEqual(ledger["items"][0]["字段来源"], "查询定义结果字段")

        contract = self.client.get(
            "/api/query-contract", params={"run_id": run_id}
        ).json()
        self.assertEqual([item["台账编号"] for item in contract["items"]],
                         [item["台账编号"] for item in ledger["items"]])

        exported = self.client.get("/api/query-field-ledger/export",
                                   params={"run_id": run_id}).json()
        self.assertEqual([item["字段名称"] for item in exported["items"]], ["金额", "状态"])

        dataset = self.client.get(f"/api/query-result/{run_id}/dataset").json()
        self.assertEqual(dataset["结果字段"], ["金额", "状态"])
        self.assertEqual(len(dataset["记录"]), 8)

    def test_result_list_filters_sorts_and_pages(self) -> None:
        first = self.execute()["entry"]["run_id"]
        second = self.client.post(
            "/api/query/3/actions", json={"values": {"action": "重新执行"}}
        ).json()["entry"]["run_id"]
        self.assertNotEqual(first, second)

        listing = self.client.get(
            "/api/query-result",
            params={
                "definition_no": "QUER-0003",
                "status": "已完成",
                "sort": "创建时间",
                "order": "desc",
                "page": 1,
                "size": 1,
            },
        ).json()
        self.assertEqual(listing["total"], 2)
        self.assertEqual([item["run_id"] for item in listing["items"]], [second])

    def test_literal_conditions_and_select_projection_are_supported(self) -> None:
        created = self.client.post(
            "/api/query",
            json={
                "values": {
                    "定义编号": "QUER-0201",
                    "关联数据源": "DATA-0003",
                    "查询模板": "SELECT 金额 FROM order_items WHERE 状态 = '已支付' ORDER BY 金额 DESC",
                    "结果字段": "金额",
                    "参数占位": "",
                }
            },
        ).json()
        self.assertTrue(created["ok"], created)
        run = self.client.post(
            f"/api/query/{created['entry']['id']}/execute", json={"values": {}}
        ).json()
        self.assertTrue(run["ok"], run)
        self.assertEqual(run["entry"]["匹配记录数"], 5)
        page = self.client.get(
            f"/api/query-result/{run['entry']['run_id']}/page", params={"page": 1}
        ).json()
        self.assertEqual(
            [item["金额"] for item in page["items"]], [200.0, 150.0, 120.0, 88.0, 45.0]
        )

    def test_field_projection_falls_back_to_template_projection(self) -> None:
        created = self.client.post(
            "/api/query",
            json={
                "values": {
                    "定义编号": "QUER-0100",
                    "关联数据源": "DATA-0003",
                    "查询模板": "SELECT 金额, 状态 FROM order_items WHERE order_id = {{order_id}}",
                    "参数默认值": {"order_id": "ORD-1002"},
                }
            },
        ).json()
        self.assertTrue(created["ok"], created)
        run = self.client.post(
            f"/api/query/{created['entry']['id']}/execute", json={"values": {}}
        ).json()
        self.assertTrue(run["ok"], run)
        self.assertEqual(run["entry"]["结果字段"], ["金额", "状态"])
        self.assertEqual(run["entry"]["字段台账"][0]["字段来源"], "查询模板SELECT投影")


if __name__ == "__main__":
    unittest.main()
