"""数据保留与法律冻结。

法务冻结（legal hold）按条件命中项目当前有效观测，命中数据与冻结原因
一并留存；冻结期间禁止常规清理（purge）或经导入覆盖命中数据；解除必须
登记批准人，冻结与解除均记录为 SQLite 事件，解除后批准人仍可审计。
"""
from __future__ import annotations

from ..domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from ..domain.models import (
    HoldStatus,
    LegalHold,
    LegalHoldItem,
    Principal,
)
from ..domain.periods import validate_period
from ..persistence.database import Database
from ..persistence.store import Store
from ..ports import Clock, IdGenerator


class RetentionService:
    def __init__(self, db: Database, clock: Clock, ids: IdGenerator) -> None:
        self.db = db
        self.clock = clock
        self.ids = ids

    def place_hold(self, principal: Principal, project_id: str, *,
                   reason: str, measure: str | None = None,
                   period_from: str | None = None,
                   period_to: str | None = None) -> dict:
        """登记法律冻结：命中行快照与冻结原因随冻结一并留存。"""
        _require_supervisor(principal)
        if not reason or not reason.strip():
            raise ValidationError("法律冻结必须登记冻结原因")
        if period_from is not None:
            validate_period(period_from)
        if period_to is not None:
            validate_period(period_to)
        if period_from and period_to and period_from > period_to:
            raise ValidationError("冻结期间起点晚于终点")

        now = self.clock.now()
        with self.db.uow() as uow:
            store = Store(uow.conn)
            seq = store.latest_batch_seq(project_id)
            snapshot = store.snapshot(project_id, seq) if seq is not None else []
            hits = [
                obs for obs in snapshot
                if (measure is None or obs.measure == measure)
                and (period_from is None or obs.period >= period_from)
                and (period_to is None or obs.period <= period_to)
            ]
            if not hits:
                raise ValidationError("冻结条件未命中任何数据")

            hold = LegalHold(
                id=self.ids.new_id("hold"),
                project_id=project_id,
                reason=reason.strip(),
                status=HoldStatus.ACTIVE,
                created_by=principal.institution_id,
                created_at=now,
                released_at=None,
                released_by=None,
                approved_by=None,
            )
            store.add_legal_hold(hold)
            for obs in hits:
                store.add_hold_item(LegalHoldItem(
                    hold_id=hold.id,
                    measure=obs.measure,
                    period=obs.period,
                    caliber=obs.caliber,
                    value=obs.value,
                    evidence_id=obs.evidence_id,
                ))
            store.add_hold_event(hold.id, "placed", principal.institution_id,
                                 None, hold.reason, now)
        return {"hold_id": hold.id, "status": hold.status.value,
                "hits": len(hits)}

    def release_hold(self, principal: Principal, hold_id: str, *,
                     approved_by: str, reason: str = "") -> dict:
        """解除冻结：必须登记批准人；解除事件与批准人永久留痕可审计。"""
        _require_supervisor(principal)
        if not approved_by or not approved_by.strip():
            raise ValidationError("解除冻结必须登记批准人")

        now = self.clock.now()
        with self.db.uow() as uow:
            store = Store(uow.conn)
            hold = store.get_legal_hold(hold_id)
            if hold is None:
                raise NotFoundError(f"冻结不存在: {hold_id}")
            if hold.status is HoldStatus.RELEASED:
                raise StateError("冻结已解除，为不可变终态")
            store.set_hold_released(hold_id, principal.institution_id,
                                    approved_by.strip(), now)
            store.add_hold_event(hold_id, "released", principal.institution_id,
                                 approved_by.strip(), reason or None, now)
        return {"hold_id": hold_id, "status": HoldStatus.RELEASED.value}

    def get_hold(self, principal: Principal, hold_id: str) -> dict:
        """冻结详情：命中数据、冻结原因与全部事件（含解除批准人）。"""
        _require_supervisor(principal)
        with self.db.read() as conn:
            store = Store(conn)
            hold = store.get_legal_hold(hold_id)
            if hold is None:
                raise NotFoundError(f"冻结不存在: {hold_id}")
            items = [
                {"measure": i.measure, "period": i.period, "caliber": i.caliber,
                 "value": i.value, "evidence_id": i.evidence_id}
                for i in store.list_hold_items(hold_id)
            ]
            events = store.list_hold_events(hold_id)
        return {**_hold_view(hold), "items": items, "events": events}

    def list_holds(self, principal: Principal, project_id: str) -> dict:
        _require_supervisor(principal)
        with self.db.read() as conn:
            holds = Store(conn).list_legal_holds(project_id)
        return {"holds": [_hold_view(h) for h in holds]}

    def purge(self, principal: Principal, project_id: str, *,
              before_period: str) -> dict:
        """常规清理：删除指定期间之前的观测；命中生效冻结时拒绝。"""
        _require_supervisor(principal)
        validate_period(before_period)
        with self.db.uow() as uow:
            store = Store(uow.conn)
            blocked = store.active_hold_items_before(project_id, before_period)
            if blocked:
                raise StateError(
                    "存在生效中的法律冻结，禁止清理命中数据",
                    detail={"holds": sorted({r["hold_id"] for r in blocked})},
                )
            purged = store.delete_observations_before(project_id, before_period)
        return {"project_id": project_id, "before_period": before_period,
                "purged": purged}


def _require_supervisor(principal: Principal) -> None:
    if not principal.is_supervisor:
        raise PermissionDeniedError("数据保留与法律冻结仅主管单位可操作")


def _hold_view(hold: LegalHold) -> dict:
    return {
        "hold_id": hold.id,
        "project_id": hold.project_id,
        "reason": hold.reason,
        "status": hold.status.value,
        "created_by": hold.created_by,
        "created_at": hold.created_at,
        "released_by": hold.released_by,
        "released_at": hold.released_at,
        "approved_by": hold.approved_by,
    }
