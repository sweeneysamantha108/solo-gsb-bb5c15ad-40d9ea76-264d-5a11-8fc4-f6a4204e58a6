"""同一潮位修订下的多船批次预排 / 确认测试。

规则：
* 一批为按优先顺序排列的 2～5 艘船，共用同一修订（可省略取最新）；
  数量或字段不合法按 VALIDATION_ERROR 拒绝；
* 预排只读不占位：逐艘沿用「最早抵达 + 航段序列字典序」，后船避开已确认
  占用与本批前船拟占用；任一船无解指出首个失败序号（1 起）；
* 确认在单事务内核对最新修订 + 当时占用，逐艘复算逐段比对；任一船无解或
  不一致 PLAN_MISMATCH 全批不保存；修订过期 REVISION_STALE；
* 成功一次保存全部快照并返回行程号；同键同内容重放返回原结果（即使修订已
  更新）；同键异内容 IDEMPOTENCY_CONFLICT；
* 旧单船接口语义保持不变。
"""
from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _ship(origin="A", destination="B", start=0, window=20,
          draft=1.0, height=0.0, margin=0.0):
    return {
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "draft": draft, "height": height, "margin": margin,
    }


def _batch_plan(client, ships, rev=None):
    body = {"ships": ships}
    if rev is not None:
        body["revision"] = rev
    return client.post("/api/trips/batch/plan", json=body)


def _batch_confirm(client, rev, batch_key, ships, plans):
    payload_ships = []
    for s, p in zip(ships, plans):
        payload_ships.append({**s, "itinerary": p["itinerary"]})
    return client.post("/api/trips/batch/confirm", json={
        "revision": rev, "batch_key": batch_key, "ships": payload_ships,
    })


def _raw_insert_trip(rev, itinerary, origin="A", destination="B",
                     start=0, window=20):
    conn = connect()
    arrival = start if not itinerary else itinerary[-1]["end_minute"]
    conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rev, origin, destination, start, window, 1.0, 0.0, 0.0,
         arrival, __import__("json").dumps(itinerary)),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. 入参校验：船数必须 2～5；字段不合法沿用 VALIDATION_ERROR
# ---------------------------------------------------------------------------

def test_batch_rejects_one_ship(client):
    r = _batch_plan(client, [_ship()])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_batch_rejects_six_ships(client):
    r = _batch_plan(client, [_ship() for _ in range(6)])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_batch_rejects_bad_fields(client):
    bad = _ship(window=99999)  # 窗口超 1440
    r = _batch_plan(client, [_ship(), bad])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    r = _batch_plan(client, [_ship(start=-1), _ship()])
    assert r.status_code == 422


def test_batch_confirm_requires_revision_and_key(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    pr = _batch_plan(client, [_ship(), _ship()], rev=rev).json()
    payload_ships = [
        {**s, "itinerary": pr["ships"][i]["plan"]["itinerary"]}
        for i, s in enumerate([_ship(), _ship()])
    ]
    # 缺 revision
    r = client.post("/api/trips/batch/confirm",
                    json={"batch_key": "k1", "ships": payload_ships})
    assert r.status_code == 422
    # 空 batch_key
    r = client.post("/api/trips/batch/confirm",
                    json={"revision": rev, "batch_key": "",
                          "ships": payload_ships})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 2. 预排：后船避开本批前船的拟占用（只读不占位）
# ---------------------------------------------------------------------------

def test_batch_plan_later_ships_avoid_earlier_ships(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    r = _batch_plan(client, [_ship(), _ship(), _ship()], rev=rev)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["revision"] == rev
    dep = [sp["plan"]["itinerary"][0]["start_minute"] for sp in data["ships"]]
    # 前船 0 进入占 0,1；次船最早 2 进入占 2,3；第三艘最早 4 进入
    assert dep == [0, 2, 4]
    arrivals = [sp["plan"]["arrival_minute"] for sp in data["ships"]]
    assert arrivals == [2, 4, 6]
    assert [sp["ship_index"] for sp in data["ships"]] == [1, 2, 3]
    # 预排不占位
    assert client.get("/api/trips").json() == []


def test_batch_plan_avoids_confirmed_and_prior_batch_ships(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 已确认船占 0,1
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    r = _batch_plan(client, [_ship(), _ship()], rev=rev)
    dep = [sp["plan"]["itinerary"][0]["start_minute"]
           for sp in r.json()["ships"]]
    assert dep == [2, 4]


def test_batch_plan_defaults_to_latest_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    seed_initial_revision(_level(0, 19, 3.0))
    # 新建修订 #2（名义潮位不变）
    client.post("/api/tide/corrections",
                json={"corrections": {100: 3.0}})
    r = _batch_plan(client, [_ship(), _ship()])
    assert r.status_code == 200
    assert r.json()["revision"] == 2


def test_batch_plan_no_feasible_reports_first_failing_index(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 已确认占用把航段占满：第 1 艘就无解
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 20, "travel_time": 20},
    ])
    r = _batch_plan(client, [_ship(), _ship()], rev=rev)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NO_FEASIBLE_ITINERARY"
    assert err["details"]["ship_index"] == 1


def test_batch_plan_second_ship_no_feasible(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 第 1 艘正常（占 0,1）；第 2 艘窗口仅 [0,4) 且 0..3 被已确认/拟占用封死
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])
    r = _batch_plan(client, [_ship(), _ship(window=4)], rev=rev)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NO_FEASIBLE_ITINERARY"
    assert err["details"]["ship_index"] == 2


def test_batch_plan_missing_tide_carries_index(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    # 只有 0..9 潮位：第 1 艘窗口 10 可行；第 2 艘窗口 20 缺潮位
    rev = seed_initial_revision(_level(0, 9, 3.0))
    r = _batch_plan(client, [_ship(window=10), _ship(window=20)], rev=rev)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "MISSING_TIDE_DATA"
    assert err["details"]["ship_index"] == 2


# ---------------------------------------------------------------------------
# 3. 确认成功：一次保存全部快照，返回行程号
# ---------------------------------------------------------------------------

def _setup_ab(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    return seed_initial_revision(_level(0, 19, 3.0))


def test_batch_confirm_saves_all_snapshots(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship(), _ship()]
    pr = _batch_plan(client, ships, rev=rev).json()
    plans = [sp["plan"] for sp in pr["ships"]]

    r = _batch_confirm(client, rev, "batch-1", ships, plans)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["batch_key"] == "batch-1"
    assert data["revision"] == rev
    assert data["replayed"] is False
    trip_ids = [t["trip"]["trip_id"] for t in data["trips"]]
    assert trip_ids == [1, 2, 3]
    assert [t["ship_index"] for t in data["trips"]] == [1, 2, 3]
    deps = [t["trip"]["itinerary"][0]["start_minute"] for t in data["trips"]]
    assert deps == [0, 2, 4]
    for t in data["trips"]:
        assert t["trip"]["batch_key"] == "batch-1"

    # 行程快照全部落库；之后的规划须避开全批占用
    trips = client.get("/api/trips").json()
    assert len(trips) == 3
    follow = client.post("/api/trips/plan", json=_ship()).json()
    assert follow["itinerary"][0]["start_minute"] == 6


def test_batch_confirm_same_itinerary_overlap_within_batch(client):
    # 批次内彼此避让（后船避开前船），不允许提交重叠：
    # 若手工提交与复算不符 -> PLAN_MISMATCH
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    pr = _batch_plan(client, ships, rev=rev).json()
    plans = [sp["plan"] for sp in pr["ships"]]
    # 篡改第 2 艘行程为与第 1 艘重叠（0 进入）
    tampered = [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ]
    r = _batch_confirm(client, rev, "batch-x", ships,
                       [plans[0], {"itinerary": tampered, **{
                           k: v for k, v in plans[1].items() if k != "itinerary"}}])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get("/api/trips").json() == []


# ---------------------------------------------------------------------------
# 4. PLAN_MISMATCH：占用变化 / 无解，且全批不保存
# ---------------------------------------------------------------------------

def test_batch_confirm_mismatch_on_occupancy_change(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    plans = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]
    assert [p["itinerary"][0]["start_minute"] for p in plans] == [0, 2]

    # 预排之后、确认之前，他船占 0,1
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    r = _batch_confirm(client, rev, "batch-2", ships, plans)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["ship_index"] == 1
    # 全批不保存：只有那条历史快照
    assert len(client.get("/api/trips").json()) == 1


def test_batch_confirm_mismatch_when_ship_has_no_solution(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    plans = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]

    # 他船把窗口全部占满：两船复算均无解
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 20, "travel_time": 20},
    ])
    r = _batch_confirm(client, rev, "batch-3", ships, plans)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["ship_index"] == 1
    assert err["details"]["reason"] == "no_feasible_itinerary"
    assert len(client.get("/api/trips").json()) == 1


def test_batch_confirm_mismatch_index_is_second_ship(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    plans = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]

    # 他船恰好占 2,3：第 1 艘（0 进入）复算仍一致，第 2 艘（2 进入）失效
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])
    r = _batch_confirm(client, rev, "batch-4", ships, plans)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["ship_index"] == 2
    assert len(client.get("/api/trips").json()) == 1


# ---------------------------------------------------------------------------
# 5. REVISION_STALE
# ---------------------------------------------------------------------------

def test_batch_confirm_revision_stale_saves_nothing(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    plans = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]

    # 确认前新增修订（潮位内容不变），rev 不再最新
    client.post("/api/tide/corrections",
                json={"corrections": {100: 3.0}})
    r = _batch_confirm(client, rev, "batch-5", ships, plans)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REVISION_STALE"
    assert client.get("/api/trips").json() == []


# ---------------------------------------------------------------------------
# 6. 幂等：同键同内容重放返回原结果（即使修订已更新）；同键异内容冲突
# ---------------------------------------------------------------------------

def test_batch_confirm_idempotent_replay_after_revision_update(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    plans = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]

    r1 = _batch_confirm(client, rev, "idem-1", ships, plans)
    assert r1.status_code == 201
    ids1 = [t["trip"]["trip_id"] for t in r1.json()["trips"]]
    assert ids1 == [1, 2]

    # 修订更新后用旧结果重试：仍幂等重放（不做最新性/复算校验）
    client.post("/api/tide/corrections",
                json={"corrections": {100: 3.0}})
    r2 = _batch_confirm(client, rev, "idem-1", ships, plans)
    assert r2.status_code == 200
    data = r2.json()
    assert data["replayed"] is True
    ids2 = [t["trip"]["trip_id"] for t in data["trips"]]
    assert ids2 == ids1
    # 不新增快照
    assert len(client.get("/api/trips").json()) == 2


def test_batch_confirm_same_key_different_content_conflict(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    plans = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]
    assert _batch_confirm(client, rev, "idem-2", ships, plans).status_code == 201

    # 同键、不同内容（改第 1 艘吃水）重提
    ships2 = [{**ships[0], "draft": 2.0}, ships[1]]
    r = _batch_confirm(client, rev, "idem-2", ships2, plans)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "IDEMPOTENCY_CONFLICT"
    assert err["details"]["batch_key"] == "idem-2"
    assert len(client.get("/api/trips").json()) == 2


def test_batch_confirm_same_key_different_order_conflict(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    plans = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]
    assert _batch_confirm(client, rev, "idem-3", ships, plans).status_code == 201

    # 同键但调换船序 -> 内容不同
    r = _batch_confirm(client, rev, "idem-3",
                       [ships[1], ships[0]], [plans[1], plans[0]])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------------------------
# 7. 不同批次顺序衔接：后批避开前批已确认占用
# ---------------------------------------------------------------------------

def test_consecutive_batches_chain_occupancy(client):
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]

    plans1 = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]
    assert _batch_confirm(client, rev, "b-a", ships, plans1).status_code == 201

    plans2 = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]
    deps = [p["itinerary"][0]["start_minute"] for p in plans2]
    assert deps == [4, 6]
    r = _batch_confirm(client, rev, "b-b", ships, plans2)
    assert r.status_code == 201
    assert [t["trip"]["itinerary"][0]["start_minute"]
            for t in r.json()["trips"]] == [4, 6]


# ---------------------------------------------------------------------------
# 8. 起终点相同的零航段船：立即抵达、不占航段，不影响后船
# ---------------------------------------------------------------------------

def test_batch_zero_leg_ships(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 第 1 艘 A->A 零航段；第 2 艘 A->B 仍可 0 进入（零航段不占段）
    r = _batch_plan(client, [_ship(origin="A", destination="A"), _ship()], rev=rev)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ships"][0]["plan"]["itinerary"] == []
    assert data["ships"][0]["plan"]["arrival_minute"] == 0
    assert data["ships"][1]["plan"]["itinerary"][0]["start_minute"] == 0

    plans = [sp["plan"] for sp in data["ships"]]
    ships = [_ship(origin="A", destination="A"), _ship()]
    r = _batch_confirm(client, rev, "zero-leg", ships, plans)
    assert r.status_code == 201, r.text
    assert [t["trip"]["arrival_minute"] for t in r.json()["trips"]] == [0, 2]


# ---------------------------------------------------------------------------
# 9. 未知修订 / 未知节点沿用既有错误语义
# ---------------------------------------------------------------------------

def test_batch_plan_unknown_revision(client):
    _setup_ab(client)  # 节点存在；仅修订号未知
    r = _batch_plan(client, [_ship(), _ship()], rev=999)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"


def test_batch_plan_unknown_node(client):
    rev = _setup_ab(client)
    r = _batch_plan(client, [_ship(origin="Z"), _ship()], rev=rev)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NODE_NOT_FOUND"


def test_batch_confirm_unknown_node_is_mismatch(client):
    # 预排合法但确认时节点被删（直接操作库），复算 NODE_NOT_FOUND 原样上抛
    rev = _setup_ab(client)
    ships = [_ship(), _ship()]
    plans = [sp["plan"] for sp in _batch_plan(client, ships, rev=rev).json()["ships"]]
    conn = connect()
    conn.execute("DELETE FROM segments")
    conn.execute("DELETE FROM nodes")
    conn.commit()
    conn.close()
    r = _batch_confirm(client, rev, "node-gone", ships, plans)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NODE_NOT_FOUND"
    assert client.get("/api/trips").json() == []


# ---------------------------------------------------------------------------
# 10. 旧单船接口语义保持
# ---------------------------------------------------------------------------

def test_single_ship_api_unchanged(client):
    rev = _setup_ab(client)
    body = _ship()
    body["revision"] = rev
    p = client.post("/api/trips/plan", json=body)
    assert p.status_code == 200
    plan = p.json()
    assert "ship_index" not in plan
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 201
    trip = r.json()
    assert "batch_key" not in trip
    assert trip["trip_id"] == 1

    # 单船确认不受批次幂等表影响
    r2 = client.post("/api/trips/confirm", json=plan)
    # 占用变化 -> PLAN_MISMATCH（单船旧语义，非幂等重放）
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "PLAN_MISMATCH"
