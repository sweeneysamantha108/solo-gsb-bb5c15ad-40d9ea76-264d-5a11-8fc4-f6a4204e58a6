"""同一潮位修订下的多船批次预排与确认。

批次规则（见 README「批次预排 / 确认」）：

* 一批为按**优先顺序**排列的 2～5 艘船，全批共用同一潮位修订（显式给出
  或取最新）；
* **预排（只读、不占位）**：逐艘沿用单船「最早抵达 + 同刻航段编号序列
  字典序」择优；后船须避开全部已确认占用**以及本批前船的拟占用**。任一船
  在窗口内无解，指出首个失败船序号（1 起）；
* **确认（单事务）**：核对所据修订是否仍最新（过期 REVISION_STALE），再
  依据当时全部已确认占用 + 本批前船提交占用逐艘复算并与提交行程**逐段
  比对**；任一船无解或不一致均 PLAN_MISMATCH，**全批不保存**；
* 通过后一次事务内保存全部不可变行程快照，并登记批次幂等键；
* **幂等**：同键同内容重放直接返回原行程号（即使修订已更新）；同键异内容
  返回 IDEMPOTENCY_CONFLICT，不保存。
"""
import hashlib
import json
from typing import Any, Optional

from ..errors import ApiError
from .planning import find_itinerary, load_occupancy


# ---------------------------------------------------------------------------
# 预排
# ---------------------------------------------------------------------------

def plan_batch(conn, revision: Optional[int], ships: list[Any]) -> dict:
    """只读批次预排。ships 为 BatchShipRequest 列表（顺序即优先顺序）。

    返回 {"revision": int, "ships": [{"ship_index", "plan"}, ...]}。
    第 k 艘船无解时抛 404 NO_FEASIBLE_ITINERARY，details 带 ship_index（1 起）。
    """
    # 解析共用修订号：未知修订沿用 TIDE_REVISION_NOT_FOUND（让首船复算抛出）
    rev = revision
    if rev is None:
        row = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "TIDE_REVISION_NOT_FOUND",
                "系统中不存在任何潮位修订，无法规划",
            )
        rev = row["revision"]

    # 已确认占用只需按全批窗口并集读取一次（不同航段/分钟互不干扰）
    lo = min(s.start_minute for s in ships)
    hi = max(s.start_minute + s.window_minutes - 1 for s in ships)
    blocked = load_occupancy(conn, lo, hi)

    results = []
    for idx, s in enumerate(ships, start=1):
        try:
            plan = find_itinerary(
                conn,
                origin=s.origin,
                destination=s.destination,
                start_minute=s.start_minute,
                window_minutes=s.window_minutes,
                draft=s.draft,
                height=s.height,
                margin=s.margin,
                revision=rev,
                blocked=blocked,
            )
        except ApiError as exc:
            if exc.code == "NO_FEASIBLE_ITINERARY":
                # 指出首个失败船序号（按顺序逐艘处理，首个抛出即首个失败）
                exc.details.update({"ship_index": idx})
            elif exc.code == "MISSING_TIDE_DATA":
                exc.details.update({"ship_index": idx})
            raise
        results.append({"ship_index": idx, "plan": plan})
        # 本船拟占用并入 blocked，供本批后船避让
        for leg in plan["itinerary"]:
            blocked.setdefault(leg["seg_id"], set()).update(
                range(leg["start_minute"], leg["end_minute"])
            )

    return {"revision": rev, "ships": results}


# ---------------------------------------------------------------------------
# 确认
# ---------------------------------------------------------------------------

def _norm_legs(legs: list[Any]) -> list[tuple]:
    out = []
    for l in legs:
        if isinstance(l, dict):
            out.append((l["seg_id"], l["origin"], l["destination"],
                        l["start_minute"], l["end_minute"], l["travel_time"]))
        else:
            out.append((l.seg_id, l.origin, l.destination,
                        l.start_minute, l.end_minute, l.travel_time))
    return out


def _canonical_request(revision: int, ships: list[Any]) -> str:
    """把确认请求规范化为稳定 JSON，再取 SHA-256，用于幂等内容比对。"""
    payload = {
        "revision": revision,
        "ships": [
            {
                "origin": s.origin,
                "destination": s.destination,
                "start_minute": s.start_minute,
                "window_minutes": s.window_minutes,
                "draft": s.draft,
                "height": s.height,
                "margin": s.margin,
                "itinerary": [
                    [l.seg_id, l.origin, l.destination,
                     l.start_minute, l.end_minute, l.travel_time]
                    for l in s.itinerary
                ],
            }
            for s in ships
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _replay(conn, batch_key: str, request_hash: str) -> Optional[dict]:
    """幂等重放：同键同内容返回原结果；同键异内容抛 IDEMPOTENCY_CONFLICT。

    键不存在返回 None（需走正常确认流程）。即使所据修订已更新也仍重放。
    """
    row = conn.execute(
        "SELECT revision, request_hash, trip_ids FROM batch_confirmations"
        " WHERE batch_key = ?",
        (batch_key,),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise ApiError(
            409,
            "IDEMPOTENCY_CONFLICT",
            f"批次幂等键 {batch_key} 已用于不同内容的确认请求",
            {"batch_key": batch_key,
             "original_revision": row["revision"]},
        )
    trip_ids = json.loads(row["trip_ids"])
    return {
        "batch_key": batch_key,
        "revision": row["revision"],
        "replayed": True,
        "trip_ids": trip_ids,
    }


def confirm_batch(conn, revision: int, batch_key: str, ships: list[Any]) -> dict:
    """在已开启的 IMMEDIATE 事务中执行批次确认。

    成功返回 {"batch_key", "revision", "replayed": False, "trip_ids": [...]}
    （行程号按提交顺序）；重放走 _replay。任何冲突均抛出，由调用方回滚。
    """
    request_hash = _canonical_request(revision, ships)

    replay = _replay(conn, batch_key, request_hash)
    if replay is not None:
        return replay

    # 1) 修订是否仍为最新（过期沿用 REVISION_STALE，全批不保存）
    latest = conn.execute(
        "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    if latest is None or latest["revision"] != revision:
        raise ApiError(
            409,
            "REVISION_STALE",
            f"潮位已更新到修订 #{latest['revision'] if latest else None}，"
            f"批次所据修订 #{revision} 已过期，请重新预排",
            {"provided_revision": revision,
             "current_revision": latest["revision"] if latest else None},
        )

    # 2) 同一事务内按当时全部已确认占用 + 本批前船占用，逐艘复算并逐段比对
    lo = min(s.start_minute for s in ships)
    hi = max(s.start_minute + s.window_minutes - 1 for s in ships)
    blocked = load_occupancy(conn, lo, hi)
    recalculated: list[dict] = []

    for idx, s in enumerate(ships, start=1):
        try:
            plan = find_itinerary(
                conn,
                origin=s.origin,
                destination=s.destination,
                start_minute=s.start_minute,
                window_minutes=s.window_minutes,
                draft=s.draft,
                height=s.height,
                margin=s.margin,
                revision=revision,
                blocked=blocked,
            )
        except ApiError as exc:
            if exc.code == "NO_FEASIBLE_ITINERARY":
                raise ApiError(
                    409,
                    "PLAN_MISMATCH",
                    f"占用已变化：批次第 {idx} 艘船窗口内已不存在可行行程，"
                    "全批确认失败",
                    {"ship_index": idx, "reason": "no_feasible_itinerary"},
                )
            raise

        submitted = _norm_legs(s.itinerary)
        canonical = _norm_legs(plan["itinerary"])
        submitted_arrival = (
            s.start_minute if not s.itinerary else s.itinerary[-1].end_minute
        )
        if submitted != canonical or submitted_arrival != plan["arrival_minute"]:
            raise ApiError(
                409,
                "PLAN_MISMATCH",
                f"批次第 {idx} 艘船复算结果与提交行程不一致（占用/参数/潮位可能"
                "已变化），全批确认失败",
                {"ship_index": idx, "recalculated": plan},
            )
        recalculated.append(plan)

        # 已确认复算占用并入 blocked，供本批后船复算时避让
        for leg in plan["itinerary"]:
            blocked.setdefault(leg["seg_id"], set()).update(
                range(leg["start_minute"], leg["end_minute"])
            )

    # 3) 复核全部通过：一次保存全部不可变快照，并登记批次幂等键
    trip_ids: list[int] = []
    for s, plan in zip(ships, recalculated):
        cur = conn.execute(
            "INSERT INTO confirmed_trips"
            " (revision, origin, destination, start_minute, window_minutes,"
            "  draft, height, margin, arrival_minute, itinerary, batch_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision, s.origin, s.destination, s.start_minute, s.window_minutes,
             s.draft, s.height, s.margin, plan["arrival_minute"],
             json.dumps(plan["itinerary"]), batch_key),
        )
        trip_ids.append(cur.lastrowid)

    conn.execute(
        "INSERT INTO batch_confirmations(batch_key, revision, request_hash, trip_ids)"
        " VALUES (?, ?, ?, ?)",
        (batch_key, revision, request_hash, json.dumps(trip_ids)),
    )

    return {
        "batch_key": batch_key,
        "revision": revision,
        "replayed": False,
        "trip_ids": trip_ids,
    }
