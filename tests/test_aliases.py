"""定义集活动别名（custom_unit_set_alias）的端到端测试。

覆盖：

- 绑定（POST /custom-unit-sets 之外）：提交 alias、目标定义集标识与
  expected_id 的比较并交换语义——初建 expected_id 为 null；当前指向与
  expected_id 不一致返回 409，目标定义集不存在返回 422，且均不改写原指向；
- 查询（GET /custom-unit-set-aliases/{alias}）：返回当前指向；未知别名 422；
- /convert、/evaluate、/solve 可凭 custom_unit_set_alias 选用定义集，与
  custom_units、custom_unit_set_id 两两互斥；未知别名 422；
- 请求开始时解析一次别名：切换与计算并发时，单次请求只完整使用旧版或新版；
- 切换与落盘共同生效：写盘失败不改变进程内指向；容器重建后别名仍有效；
  旧版存储文件缺少 aliases 字段时也能加载原定义集；
- 未选别名或直接用原标识的调用维持原行为。
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import unit_sets

client = TestClient(app)
# 写盘失败场景：不把异常直接抛给测试，而是按 500 响应处理
client_lenient = TestClient(app, raise_server_exceptions=False)


def cu(symbol, factor, base_unit):
    return {"symbol": symbol, "factor": factor, "base_unit": base_unit}


def assert_err(resp, code, status=422):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["error"] == code


def publish(defs):
    r = client.post("/custom-unit-sets", json={"custom_units": defs})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def bind(alias, set_id, expected_id):
    return client.post(
        "/custom-unit-set-aliases",
        json={"alias": alias, "set_id": set_id, "expected_id": expected_id},
    )


def get_alias(alias):
    return client.get(f"/custom-unit-set-aliases/{alias}")


def convert_via_alias(value, from_unit, to_unit, alias):
    return client.post(
        "/convert",
        json={
            "value": value,
            "from": from_unit,
            "to": to_unit,
            "custom_unit_set_alias": alias,
        },
    )


# ---------------------------------------------------------------------------
# 绑定：比较并交换语义
# ---------------------------------------------------------------------------


def test_bind_create_then_query_and_use():
    sid = publish([cu("ft", "0.3048", "m")])
    r = bind("length", sid, None)
    assert r.status_code == 200, r.text
    assert r.json() == {"alias": "length", "set_id": sid}

    r = get_alias("length")
    assert r.status_code == 200
    assert r.json() == {"alias": "length", "set_id": sid}

    r = convert_via_alias("10", "ft", "m", "length")
    assert r.status_code == 200
    assert r.json() == {"value": "3.048", "unit": "m", "dimension": "m"}


def test_bind_expected_id_omitted_defaults_to_null():
    sid = publish([cu("ft", "0.3048", "m")])
    r = client.post(
        "/custom-unit-set-aliases", json={"alias": "length", "set_id": sid}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"alias": "length", "set_id": sid}


def test_bind_switch_with_matching_expected_id():
    sid_v1 = publish([cu("ft", "0.3048", "m")])
    sid_v2 = publish([cu("ft", "0.3", "m")])
    assert bind("length", sid_v1, None).status_code == 200
    # 维护者切换：提交当前指向作为 expected_id
    r = bind("length", sid_v2, sid_v1)
    assert r.status_code == 200, r.text
    assert r.json() == {"alias": "length", "set_id": sid_v2}
    assert get_alias("length").json()["set_id"] == sid_v2
    # 调用方沿用同一名称，实际定义已切换
    assert convert_via_alias("10", "ft", "m", "length").json()["value"] == "3"
    # 原标识仍可直接使用（已发布内容不可变）
    r = client.post(
        "/convert",
        json={"value": "10", "from": "ft", "to": "m", "custom_unit_set_id": sid_v1},
    )
    assert r.json()["value"] == "3.048"


def test_bind_conflict_returns_409_and_keeps_pointer():
    sid_v1 = publish([cu("ft", "0.3048", "m")])
    sid_v2 = publish([cu("ft", "0.3", "m")])
    assert bind("length", sid_v1, None).status_code == 200

    # 已存在的别名再按“初建”提交（expected_id 为 null）-> 409
    r = bind("length", sid_v2, None)
    assert_err(r, "alias_conflict", status=409)
    assert r.json()["detail"]["current_id"] == sid_v1
    # 陈旧的 expected_id -> 409
    r = bind("length", sid_v2, "cus_stale")
    assert_err(r, "alias_conflict", status=409)

    # 两次失败都不改写原指向
    assert get_alias("length").json()["set_id"] == sid_v1
    assert convert_via_alias("10", "ft", "m", "length").json()["value"] == "3.048"


def test_bind_to_nonexistent_set_returns_422_and_creates_nothing():
    r = bind("length", "cus_does_not_exist", None)
    assert_err(r, "invalid_request")
    # 目标不存在不会留下任何绑定
    assert_err(get_alias("length"), "invalid_request")


def test_bind_to_nonexistent_set_keeps_existing_pointer():
    sid = publish([cu("ft", "0.3048", "m")])
    assert bind("length", sid, None).status_code == 200
    assert_err(bind("length", "cus_does_not_exist", sid), "invalid_request")
    assert get_alias("length").json()["set_id"] == sid


def test_bind_rejects_invalid_alias_name():
    sid = publish([cu("ft", "0.3048", "m")])
    for bad in ("1abc", "has space", "", "a" * 65, "长度"):
        r = bind(bad, sid, None)
        assert_err(r, "invalid_request")
    assert unit_sets.get_store().count() == 1


def test_concurrent_binds_only_one_wins():
    sids = [publish([cu("ft", str(i + 1), "m")]) for i in range(8)]

    def try_bind(sid):
        return client.post(
            "/custom-unit-set-aliases",
            json={"alias": "race", "set_id": sid, "expected_id": None},
        ).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(try_bind, sids))
    assert codes.count(200) == 1
    assert codes.count(409) == len(sids) - 1
    # 最终指向就是唯一胜出的目标
    winner = sids[codes.index(200)]
    assert get_alias("race").json()["set_id"] == winner


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def test_query_unknown_alias_returns_422():
    assert_err(get_alias("nope"), "invalid_request")


# ---------------------------------------------------------------------------
# 三个接口凭别名选用
# ---------------------------------------------------------------------------


def test_evaluate_via_alias():
    sid = publish([cu("dozen", "12", "1")])
    assert bind("count", sid, None).status_code == 200
    r = client.post(
        "/evaluate",
        json={
            "variables": {
                "a": {"value": "2", "unit": "dozen"},
                "b": {"value": "3", "unit": "dozen"},
            },
            "formula": "a*b",
            "output_unit": "dozen^2",
            "custom_unit_set_alias": "count",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"value": "6", "unit": "dozen^2", "dimension": "1"}


def test_solve_via_alias():
    sid = publish([cu("rod", "5.0292", "m")])
    assert bind("survey", sid, None).status_code == 200
    r = client.post(
        "/solve",
        json={
            "variables": {"a": {"value": "3", "unit": "rod"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "m",
            "custom_unit_set_alias": "survey",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"value": "15.0876", "unit": "m", "dimension": "m"}


def test_unknown_alias_rejected_on_all_endpoints():
    bodies = [
        {"value": "1", "from": "m", "to": "m"},
        {
            "variables": {"a": {"value": "1", "unit": "m"}},
            "formula": "a",
            "output_unit": "m",
        },
        {
            "variables": {"a": {"value": "1", "unit": "m"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "m",
        },
    ]
    for path, body in zip(("/convert", "/evaluate", "/solve"), bodies):
        assert_err(
            client.post(path, json={**body, "custom_unit_set_alias": "nope"}),
            "invalid_request",
        )


def test_alias_is_mutually_exclusive_with_other_sources():
    sid = publish([cu("ft", "0.3048", "m")])
    assert bind("length", sid, None).status_code == 200
    base = {"value": "1", "from": "ft", "to": "m"}
    combos = [
        {"custom_unit_set_alias": "length", "custom_units": [cu("ft", "1", "m")]},
        {"custom_unit_set_alias": "length", "custom_unit_set_id": sid},
        {
            "custom_unit_set_alias": "length",
            "custom_unit_set_id": sid,
            "custom_units": [cu("ft", "1", "m")],
        },
    ]
    for extra in combos:
        assert_err(client.post("/convert", json={**base, **extra}), "invalid_request")


def test_null_alias_is_equivalent_to_absent():
    r = client.post(
        "/convert",
        json={"value": "1", "from": "m", "to": "cm", "custom_unit_set_alias": None},
    )
    assert r.status_code == 200
    assert r.json()["value"] == "100"


# ---------------------------------------------------------------------------
# 切换与计算并发：单次请求只完整使用旧版或新版
# ---------------------------------------------------------------------------


def test_request_sees_consistent_version_during_concurrent_switch():
    # 两版定义对两个符号给出不同含义：混用版本会得到 23/32，正确实现只会出现 22/33
    sid_old = publish([cu("xa", "2", "m"), cu("xb", "20", "m")])
    sid_new = publish([cu("xa", "3", "m"), cu("xb", "30", "m")])
    assert bind("mix", sid_old, None).status_code == 200

    def evaluate_sum():
        r = client.post(
            "/evaluate",
            json={
                "variables": {
                    "a": {"value": "1", "unit": "xa"},
                    "b": {"value": "1", "unit": "xb"},
                },
                "formula": "a+b",
                "output_unit": "m",
                "custom_unit_set_alias": "mix",
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["value"]

    def flip():
        for _ in range(30):
            cur = get_alias("mix").json()["set_id"]
            target = sid_new if cur == sid_old else sid_old
            client.post(
                "/custom-unit-set-aliases",
                json={"alias": "mix", "set_id": target, "expected_id": cur},
            )

    with ThreadPoolExecutor(9) as pool:
        flipper = pool.submit(flip)
        results = list(pool.map(lambda _: evaluate_sum(), range(120)))
        flipper.result()
    assert set(results) <= {"22", "33"}
    assert "22" in results and "33" in results


# ---------------------------------------------------------------------------
# 切换与落盘共同生效
# ---------------------------------------------------------------------------


def test_failed_persist_does_not_change_in_memory_pointer(monkeypatch):
    sid_v1 = publish([cu("ft", "0.3048", "m")])
    sid_v2 = publish([cu("ft", "0.3", "m")])
    assert bind("length", sid_v1, None).status_code == 200

    store = unit_sets.get_store()
    original = store._persist_locked

    def boom(aliases=None):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_persist_locked", boom)
    r = client_lenient.post(
        "/custom-unit-set-aliases",
        json={"alias": "length", "set_id": sid_v2, "expected_id": sid_v1},
    )
    assert r.status_code == 500
    monkeypatch.setattr(store, "_persist_locked", original)

    # 进程内指向未被改写，磁盘文件也仍是旧指向
    assert get_alias("length").json()["set_id"] == sid_v1
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["aliases"] == {"length": sid_v1}
    # 恢复后可正常切换
    assert bind("length", sid_v2, sid_v1).status_code == 200
    assert get_alias("length").json()["set_id"] == sid_v2


def test_failed_persist_on_create_leaves_no_alias(monkeypatch):
    sid = publish([cu("ft", "0.3048", "m")])
    store = unit_sets.get_store()
    monkeypatch.setattr(
        store,
        "_persist_locked",
        lambda aliases=None: (_ for _ in ()).throw(OSError("read-only fs")),
    )
    r = client_lenient.post(
        "/custom-unit-set-aliases",
        json={"alias": "length", "set_id": sid, "expected_id": None},
    )
    assert r.status_code == 500
    monkeypatch.undo()
    assert_err(get_alias("length"), "invalid_request")


# ---------------------------------------------------------------------------
# 持久化：容器重建后别名仍有效；旧文件缺少别名字段也能加载
# ---------------------------------------------------------------------------


def test_alias_survives_store_reinitialization():
    store = unit_sets.get_store()
    sid_v1 = publish([cu("ft", "0.3048", "m")])
    sid_v2 = publish([cu("ft", "0.3", "m")])
    assert bind("length", sid_v1, None).status_code == 200
    assert bind("length", sid_v2, sid_v1).status_code == 200
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["aliases"] == {"length": sid_v2}

    # 模拟容器/进程重建：在同一数据目录重新加载存储
    reloaded = unit_sets.UnitSetStore(os.path.dirname(store.path))
    reloaded.load()
    unit_sets._store = reloaded
    assert get_alias("length").json() == {"alias": "length", "set_id": sid_v2}
    assert convert_via_alias("10", "ft", "m", "length").json()["value"] == "3"
    # 重建后还可继续切换
    assert bind("length", sid_v1, sid_v2).status_code == 200
    assert convert_via_alias("10", "ft", "m", "length").json()["value"] == "3.048"


def test_legacy_store_file_without_aliases_field_loads(tmp_path):
    # 旧版本（无 aliases 字段）的存储文件：原定义集照常加载与使用
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy = {
        "version": 1,
        "sets": {
            "cus_legacy": [
                {"symbol": "ft", "factor": "0.3048", "base_unit": "m"},
            ]
        },
    }
    (data_dir / unit_sets.STORE_FILENAME).write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    store = unit_sets.UnitSetStore(str(data_dir))
    store.load()
    unit_sets._store = store

    r = client.post(
        "/convert",
        json={"value": "10", "from": "ft", "to": "m", "custom_unit_set_id": "cus_legacy"},
    )
    assert r.status_code == 200
    assert r.json()["value"] == "3.048"
    # 旧文件上新增绑定：落盘后文件带 aliases 字段，旧定义集仍在
    assert bind("length", "cus_legacy", None).status_code == 200
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["aliases"] == {"length": "cus_legacy"}
    assert "cus_legacy" in raw["sets"]
    assert convert_via_alias("10", "ft", "m", "length").json()["value"] == "3.048"


# ---------------------------------------------------------------------------
# 未选别名或直接用原标识的调用维持原行为
# ---------------------------------------------------------------------------


def test_calls_without_alias_keep_original_behaviour():
    sid = publish([cu("ft", "0.3048", "m")])
    assert bind("length", sid, None).status_code == 200
    # 不带任何自定义来源
    r = client.post("/convert", json={"value": "36", "from": "km/h", "to": "m/s"})
    assert r.json() == {"value": "10", "unit": "m/s", "dimension": "m/s"}
    assert_err(
        client.post("/convert", json={"value": "1", "from": "ft", "to": "m"}),
        "unknown_symbol",
    )
    # 直接用原标识
    r = client.post(
        "/convert",
        json={"value": "10", "from": "ft", "to": "m", "custom_unit_set_id": sid},
    )
    assert r.json()["value"] == "3.048"
    # 请求内 custom_units
    r = client.post(
        "/convert",
        json={
            "value": "10",
            "from": "ft",
            "to": "m",
            "custom_units": [cu("ft", "0.5", "m")],
        },
    )
    assert r.json()["value"] == "5"


def test_alias_does_not_modify_published_sets():
    sid_v1 = publish([cu("ft", "0.3048", "m")])
    sid_v2 = publish([cu("ft", "0.3", "m")])
    assert bind("length", sid_v1, None).status_code == 200
    assert bind("length", sid_v2, sid_v1).status_code == 200
    # 切换只改别名指向，两份定义集内容不变、数量不变
    assert unit_sets.get_store().count() == 2
    for sid, expected in ((sid_v1, "3.048"), (sid_v2, "3")):
        r = client.post(
            "/convert",
            json={"value": "10", "from": "ft", "to": "m", "custom_unit_set_id": sid},
        )
        assert r.json()["value"] == expected


def test_alias_names_are_independent_of_symbols():
    # 别名只是定义集的名字，与单位符号命名空间无关
    sid = publish([cu("ft", "0.3048", "m")])
    assert bind("ft", sid, None).status_code == 200
    assert convert_via_alias("1", "ft", "m", "ft").json()["value"] == "0.3048"
    # 不选别名时 ft 仍是未知符号
    assert_err(
        client.post("/convert", json={"value": "1", "from": "ft", "to": "m"}),
        "unknown_symbol",
    )


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_alias_endpoints_are_read_and_bind_only(method):
    sid = publish([cu("ft", "0.3048", "m")])
    assert bind("length", sid, None).status_code == 200
    r = getattr(client, method)("/custom-unit-set-aliases/length")
    assert r.status_code in (404, 405), (method, r.status_code)
    assert get_alias("length").json()["set_id"] == sid
