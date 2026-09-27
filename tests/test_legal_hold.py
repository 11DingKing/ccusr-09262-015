"""数据保留与法律冻结：命中留存、清理/覆盖拦截、解除批准人审计。"""
from __future__ import annotations

import unittest

from service_09252_010.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from service_09252_010.persistence.store import Store
from support import INST_A, PROJECT, SUPERVISOR, RigTestCase

NOTICE = "法务冻结通知〔2026〕09 号"
APPROVER = "法务专员-李某"


class LegalHoldCase(RigTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.evidence = self.seed_evidence()
        self.seed_import([
            {"measure": "enrollment_count", "period": "2023-06",
             "caliber": "DE-DUAL", "value": 10, "evidence_id": self.evidence},
            {"measure": "enrollment_count", "period": "2024-01",
             "caliber": "DE-DUAL", "value": 20, "evidence_id": self.evidence},
            {"measure": "employed_count", "period": "2024-01",
             "caliber": "DE-DUAL", "value": 8, "evidence_id": self.evidence},
        ], self.evidence)

    def place(self, **kwargs) -> str:
        kwargs.setdefault("reason", NOTICE)
        return self.rig.retention.place_hold(SUPERVISOR, PROJECT, **kwargs)["hold_id"]


class PlaceHoldTests(LegalHoldCase):
    def test_place_hold_preserves_hits_and_reason(self) -> None:
        result = self.rig.retention.place_hold(
            SUPERVISOR, PROJECT, reason=NOTICE, measure="enrollment_count")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["hits"], 2)

        hold = self.rig.retention.get_hold(SUPERVISOR, result["hold_id"])
        self.assertEqual(hold["reason"], NOTICE)
        self.assertEqual(hold["created_by"], "主管单位")
        self.assertIsNone(hold["approved_by"])
        # 命中数据（含取值与证据引用）随冻结留存
        values = {i["period"]: i["value"] for i in hold["items"]}
        self.assertEqual(values, {"2023-06": 10.0, "2024-01": 20.0})
        self.assertTrue(all(i["evidence_id"] == self.evidence
                            for i in hold["items"]))
        # 冻结成为事件
        self.assertEqual([e["event"] for e in hold["events"]], ["placed"])
        self.assertEqual(hold["events"][0]["actor"], "主管单位")
        self.assertEqual(hold["events"][0]["reason"], NOTICE)

    def test_place_hold_filters_by_period_range(self) -> None:
        hold_id = self.place(period_from="2024-01", period_to="2024-12")
        hold = self.rig.retention.get_hold(SUPERVISOR, hold_id)
        self.assertEqual(
            {(i["measure"], i["period"]) for i in hold["items"]},
            {("enrollment_count", "2024-01"), ("employed_count", "2024-01")},
        )

    def test_hold_requires_reason(self) -> None:
        for reason in ("", "   "):
            with self.assertRaises(ValidationError):
                self.place(reason=reason)

    def test_hold_without_hits_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.place(measure="nonexistent_measure")

    def test_hold_period_validated(self) -> None:
        with self.assertRaises(ValidationError):
            self.place(period_from="2024/01")
        with self.assertRaises(ValidationError):
            self.place(period_from="2024-12", period_to="2024-01")

    def test_hold_requires_supervisor(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.rig.retention.place_hold(INST_A, PROJECT, reason=NOTICE)
        hold_id = self.place()
        with self.assertRaises(PermissionDeniedError):
            self.rig.retention.get_hold(INST_A, hold_id)
        with self.assertRaises(PermissionDeniedError):
            self.rig.retention.release_hold(INST_A, hold_id,
                                            approved_by=APPROVER)
        with self.assertRaises(PermissionDeniedError):
            self.rig.retention.purge(INST_A, PROJECT, before_period="2024-01")

    def test_list_holds(self) -> None:
        first = self.place(measure="enrollment_count")
        second = self.place(measure="employed_count")
        holds = self.rig.retention.list_holds(SUPERVISOR, PROJECT)["holds"]
        self.assertEqual([h["hold_id"] for h in holds], [first, second])
        self.assertTrue(all(h["status"] == "active" for h in holds))


class PurgeGuardTests(LegalHoldCase):
    def test_purge_rejected_during_hold(self) -> None:
        hold_id = self.place(measure="enrollment_count", period_to="2023-12")
        with self.assertRaises(StateError) as ctx:
            self.rig.retention.purge(SUPERVISOR, PROJECT,
                                     before_period="2024-01")
        self.assertEqual(ctx.exception.detail["holds"], [hold_id])

    def test_purge_skips_unheld_rows(self) -> None:
        # 冻结 2024-01 数据；2023 年的旧数据仍可常规清理
        self.place(measure="employed_count")
        result = self.rig.retention.purge(SUPERVISOR, PROJECT,
                                          before_period="2024-01")
        self.assertEqual(result["purged"], 1)
        with self.rig.db.read() as conn:
            remaining = Store(conn).snapshot(PROJECT, 1)
        self.assertEqual(
            {(o.measure, o.period) for o in remaining},
            {("enrollment_count", "2024-01"), ("employed_count", "2024-01")},
        )

    def test_purge_without_hold_deletes_old_rows(self) -> None:
        result = self.rig.retention.purge(SUPERVISOR, PROJECT,
                                          before_period="2024-01")
        self.assertEqual(result["purged"], 1)
        again = self.rig.retention.purge(SUPERVISOR, PROJECT,
                                         before_period="2024-01")
        self.assertEqual(again["purged"], 0)

    def test_purge_validates_period(self) -> None:
        with self.assertRaises(ValidationError):
            self.rig.retention.purge(SUPERVISOR, PROJECT,
                                     before_period="2024/01")


class OverwriteGuardTests(LegalHoldCase):
    def test_import_overwrite_rejected_during_hold(self) -> None:
        self.place(measure="enrollment_count", period_from="2024-01")
        with self.assertRaises(StateError) as ctx:
            self.seed_import([
                {"measure": "enrollment_count", "period": "2024-01",
                 "caliber": "DE-DUAL", "value": 99,
                 "evidence_id": self.evidence},
            ], self.evidence)
        self.assertEqual(
            ctx.exception.detail["held_keys"],
            [{"measure": "enrollment_count", "period": "2024-01",
              "caliber": "DE-DUAL"}],
        )

    def test_import_retract_rejected_during_hold(self) -> None:
        self.place(measure="enrollment_count", period_from="2024-01")
        with self.assertRaises(StateError):
            self.seed_import([
                {"measure": "enrollment_count", "period": "2024-01",
                 "caliber": "DE-DUAL", "evidence_id": self.evidence,
                 "retract": True},
            ], self.evidence)

    def test_import_unheld_keys_still_allowed(self) -> None:
        self.place(measure="enrollment_count", period_from="2024-01")
        result = self.seed_import([
            {"measure": "enrollment_count", "period": "2024-02",
             "caliber": "DE-DUAL", "value": 30, "evidence_id": self.evidence},
        ], self.evidence)
        self.assertEqual(result["version_no"], 2)

    def test_import_allowed_after_release(self) -> None:
        hold_id = self.place(measure="enrollment_count", period_from="2024-01")
        self.rig.retention.release_hold(SUPERVISOR, hold_id,
                                        approved_by=APPROVER)
        result = self.seed_import([
            {"measure": "enrollment_count", "period": "2024-01",
             "caliber": "DE-DUAL", "value": 99, "evidence_id": self.evidence},
        ], self.evidence)
        self.assertEqual(result["version_no"], 2)


class ReleaseAuditTests(LegalHoldCase):
    def test_release_records_approver_audit(self) -> None:
        hold_id = self.place(measure="enrollment_count")
        result = self.rig.retention.release_hold(
            SUPERVISOR, hold_id, approved_by=APPROVER,
            reason="法务函〔2026〕18 号")
        self.assertEqual(result["status"], "released")

        hold = self.rig.retention.get_hold(SUPERVISOR, hold_id)
        self.assertEqual(hold["status"], "released")
        self.assertEqual(hold["approved_by"], APPROVER)
        self.assertEqual(hold["released_by"], "主管单位")
        self.assertIsNotNone(hold["released_at"])
        # 冻结与解除均为事件，批准人留痕
        self.assertEqual([e["event"] for e in hold["events"]],
                         ["placed", "released"])
        released = hold["events"][-1]
        self.assertEqual(released["actor"], "主管单位")
        self.assertEqual(released["approved_by"], APPROVER)
        self.assertEqual(released["reason"], "法务函〔2026〕18 号")

    def test_approver_auditable_after_release_and_purge(self) -> None:
        hold_id = self.place(measure="enrollment_count")
        self.rig.retention.release_hold(SUPERVISOR, hold_id,
                                        approved_by=APPROVER)
        # 解除后清理放行
        purged = self.rig.retention.purge(SUPERVISOR, PROJECT,
                                          before_period="2025-01")
        self.assertEqual(purged["purged"], 3)
        # 数据已清理，但批准人审计信息与命中快照仍可查
        audit = self.rig.retention.get_hold(SUPERVISOR, hold_id)
        self.assertEqual(audit["approved_by"], APPROVER)
        self.assertEqual(audit["events"][-1]["approved_by"], APPROVER)
        self.assertEqual(len(audit["items"]), 2)

    def test_release_requires_approver(self) -> None:
        hold_id = self.place()
        for approver in ("", "   "):
            with self.assertRaises(ValidationError):
                self.rig.retention.release_hold(SUPERVISOR, hold_id,
                                                approved_by=approver)

    def test_double_release_rejected(self) -> None:
        hold_id = self.place()
        self.rig.retention.release_hold(SUPERVISOR, hold_id,
                                        approved_by=APPROVER)
        with self.assertRaises(StateError):
            self.rig.retention.release_hold(SUPERVISOR, hold_id,
                                            approved_by=APPROVER)

    def test_release_unknown_hold(self) -> None:
        with self.assertRaises(NotFoundError):
            self.rig.retention.release_hold(SUPERVISOR, "hold-0000",
                                            approved_by=APPROVER)
        with self.assertRaises(NotFoundError):
            self.rig.retention.get_hold(SUPERVISOR, "hold-0000")


if __name__ == "__main__":
    unittest.main()
