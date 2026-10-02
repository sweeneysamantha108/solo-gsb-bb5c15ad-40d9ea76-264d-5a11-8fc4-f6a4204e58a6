"""Pydantic 请求 / 响应模型。"""
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from . import config


# ---- 节点 / 航段 ----------------------------------------------------------------

class NodeIn(BaseModel):
    node_id: str = Field(min_length=1, description="节点编号，如 A")
    name: Optional[str] = None


class NodeOut(BaseModel):
    node_id: str
    name: Optional[str] = None


class SegmentIn(BaseModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    travel_time: int = Field(gt=0, description="航时（正整数分钟）")
    depth: float = Field(ge=0, description="静水深度（米）")
    clearance: Optional[float] = Field(
        default=None, ge=0, description="桥底净空（米），不填表示无桥"
    )


class SegmentOut(BaseModel):
    seg_id: int
    origin: str
    destination: str
    travel_time: int
    depth: float
    clearance: Optional[float] = None


# ---- 潮位 ----------------------------------------------------------------------

class TideCorrection(BaseModel):
    corrections: dict[int, float] = Field(
        description="要覆盖的 {分钟: 名义潮位(米)}；未列出分钟继承基础修订名义潮位与误差"
    )
    base_revision: Optional[int] = Field(
        default=None, description="所基于的修订号；不填则取最新修订"
    )
    note: Optional[str] = None


class TideErrorRevision(BaseModel):
    errors: dict[int, float] = Field(
        description="要覆盖的 {分钟: 预测误差(米，非负)}；未列出分钟继承基础修订误差，"
                    "名义潮位不变"
    )
    base_revision: Optional[int] = Field(
        default=None, description="所基于的修订号；不填则取最新修订"
    )
    note: Optional[str] = None


class RevisionOut(BaseModel):
    revision: int
    note: Optional[str] = None
    created_at: str


# ---- 规划 / 确认 ----------------------------------------------------------------

class PlanRequest(BaseModel):
    origin: str
    destination: str
    start_minute: int = Field(ge=0, description="规划窗口起点（绝对分钟，>=0）")
    window_minutes: int = Field(gt=0, le=config.MAX_WINDOW, description="窗口长度，<=1440")
    draft: float = Field(ge=0, description="船舶吃水（米）")
    height: float = Field(ge=0, description="水面以上高度（米）")
    margin: float = Field(ge=0, description="安全余量（米）")
    revision: Optional[int] = Field(default=None, description="潮位修订号；不填用最新")


class LegOut(BaseModel):
    seg_id: int
    origin: str
    destination: str
    start_minute: int
    end_minute: int
    travel_time: int


class PlanResponse(BaseModel):
    revision: int
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    arrival_minute: int
    total_wait_minutes: int
    itinerary: list[LegOut]


class ConfirmRequest(PlanRequest):
    itinerary: list[LegOut] = Field(description="按规划结果原样回填的行程（含 seg_id/起止分钟）")


class TripOut(BaseModel):
    trip_id: int
    revision: int
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    arrival_minute: int
    itinerary: list[LegOut]
    confirmed_at: str


# ---- 潮位更正后的复核 / 改期 -------------------------------------------------------

class ReviewRequest(BaseModel):
    target_revision: Optional[int] = Field(
        default=None, description="目标潮位修订号；不填取最新修订"
    )


class ViolationOut(BaseModel):
    leg_index: int = Field(description="首次违规航段在快照行程中的下标（0 起）")
    seg_id: int
    origin: str
    destination: str
    minute: int = Field(description="首次违规的占用分钟（绝对分钟）")
    reasons: list[str] = Field(
        description="违规原因：depth=水深不足，clearance=桥高净空不足，missing_tide=缺潮位"
    )
    tide_level: Optional[float] = None
    tide_error: Optional[float] = Field(
        default=None, description="该分钟目标修订的预测误差（米）"
    )
    depth: float = Field(description="航段静水深度（米）")
    clearance: Optional[float] = Field(default=None, description="桥底净空（米），无桥为 null")
    required_depth: float = Field(description="要求的最小可用水深：吃水+余量（米）")
    required_clearance: float = Field(description="要求的最小净空：水面以上高度+余量（米）")
    available_depth: Optional[float] = Field(
        default=None, description="该分钟按误差带保守极值的实际可用水深：静水深度+名义潮位−误差（米）"
    )
    available_clearance: Optional[float] = Field(
        default=None,
        description="该分钟按误差带保守极值的实际桥高净空：桥底净空−名义潮位−误差（米），无桥为 null",
    )


class ChangedLegOut(BaseModel):
    leg_index: int
    seg_id: int
    original_departure_minute: int
    new_departure_minute: int


class RescheduleOut(BaseModel):
    revision: int = Field(description="改期所据目标修订号")
    arrival_minute: int
    total_wait_minutes: int
    changed_leg_count: int = Field(description="相对快照出发分钟发生变化的航段个数")
    changed_legs: list[ChangedLegOut]
    itinerary: list[LegOut]


class TripReviewOut(BaseModel):
    trip_id: int
    snapshot_revision: int = Field(description="确认快照所据修订号")
    target_revision: int = Field(description="本次复核所据目标修订号")
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    original_arrival_minute: int
    safe: bool = Field(description="原行程在目标修订下按原占用分钟是否安全")
    first_violation: Optional[ViolationOut] = Field(
        default=None, description="不安全时的首次违规航段/分钟/原因"
    )
    reschedule: Optional[RescheduleOut] = Field(
        default=None, description="不安全且窗口内有改期解时的改期行程"
    )
    no_solution: bool = Field(
        default=False, description="不安全且窗口内无改期解时为 true（快照不被修改）"
    )
    original_itinerary: list[LegOut]


# ---- 批次预排 / 确认（同一潮位修订下 2~5 艘船） -----------------------------------

class BatchShipRequest(BaseModel):
    """批次中的单船参数：字段语义与单船 /api/trips/plan 请求完全一致。"""
    origin: str
    destination: str
    start_minute: int = Field(ge=0, description="规划窗口起点（绝对分钟，>=0）")
    window_minutes: int = Field(gt=0, le=config.MAX_WINDOW, description="窗口长度，<=1440")
    draft: float = Field(ge=0, description="船舶吃水（米）")
    height: float = Field(ge=0, description="水面以上高度（米）")
    margin: float = Field(ge=0, description="安全余量（米）")


class BatchPlanRequest(BaseModel):
    revision: Optional[int] = Field(
        default=None, description="全批共用的潮位修订号；不填用最新修订"
    )
    ships: list[BatchShipRequest] = Field(
        min_length=2, max_length=5,
        description="按优先顺序排列的船舶，2～5 艘；后者须避开本批前者的拟占用",
    )


class BatchShipPlanOut(BaseModel):
    """批次预排中单船结果（只读、不占位）。"""
    ship_index: int = Field(description="在批次中的序号（1 起）")
    plan: PlanResponse


class BatchPlanResponse(BaseModel):
    revision: int = Field(description="本次预排实际使用（解析后）的潮位修订号")
    ships: list[BatchShipPlanOut]


class BatchConfirmShip(BaseModel):
    """批次确认中的单船提交：船舶参数 + 预排返回的完整行程。"""
    origin: str
    destination: str
    start_minute: int = Field(ge=0)
    window_minutes: int = Field(gt=0, le=config.MAX_WINDOW)
    draft: float = Field(ge=0)
    height: float = Field(ge=0)
    margin: float = Field(ge=0)
    itinerary: list[LegOut] = Field(description="预排结果原样回填的完整行程")


class BatchConfirmRequest(BaseModel):
    revision: int = Field(description="预排所据潮位修订号（必填，确认时核对是否仍最新）")
    batch_key: str = Field(
        min_length=1, max_length=128,
        description="批次幂等键：同键同内容重放返回原结果，同键异内容冲突",
    )
    ships: list[BatchConfirmShip] = Field(
        min_length=2, max_length=5,
        description="按优先顺序排列的船舶提交（顺序与预排一致），2～5 艘",
    )


class BatchTripOut(TripOut):
    batch_key: Optional[str] = Field(default=None, description="所属批次幂等键")


class BatchShipResultOut(BaseModel):
    ship_index: int = Field(description="在批次中的序号（1 起）")
    trip: BatchTripOut


class BatchConfirmResponse(BaseModel):
    batch_key: str
    revision: int
    replayed: bool = Field(
        description="true=同键同内容幂等重放（返回原行程号，不重新保存）"
    )
    trips: list[BatchShipResultOut] = Field(
        description="按提交顺序对应的不可变行程快照（含行程号）"
    )
