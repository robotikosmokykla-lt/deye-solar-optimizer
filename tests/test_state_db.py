import datetime as dt
import tempfile
import unittest
from pathlib import Path

from state_db import StateDB


class StateDBWriteAccountingTests(unittest.TestCase):
    def test_dry_run_does_not_consume_accepted_budget(self):
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            now = dt.datetime(2026, 9, 1, 23, 0, tzinfo=dt.timezone.utc)
            db.add_write(now, 1000, 400, "night_plan", None, "dry-run", "test", accepted=False)
            self.assertEqual(db.writes_since("2026-09-01T00:00:00+00:00"), 0)
            self.assertIsNone(db.last_successful_write())
            db.add_write(now, 1000, 400, "night_plan", 123, "pending", "test", accepted=True)
            self.assertEqual(db.writes_since("2026-09-01T00:00:00+00:00"), 1)
            self.assertEqual(db.last_pending_write()["order_id"], "123")
            db.update_write_status(123, "success", "confirmed", now)
            self.assertEqual(db.last_successful_write()["status"], "success")
            db.close()

    def test_failed_accepted_order_counts_submission_but_not_success_budget(self):
        with tempfile.TemporaryDirectory() as td:
            db = StateDB(str(Path(td) / "state.db"))
            now = dt.datetime(2026, 9, 2, 0, 30, tzinfo=dt.timezone.utc)
            db.add_write(now, 1000, 300, "night_plan", 999, "pending", "accepted", accepted=True)
            db.update_write_status(999, "failed", "device returned 500", now)
            self.assertEqual(db.writes_since("2026-09-02T00:00:00+00:00"), 1)  # accepted/submitted history
            self.assertEqual(db.order_submissions_since("2026-09-02T00:00:00+00:00"), 1)
            self.assertEqual(db.successful_writes_since("2026-09-02T00:00:00+00:00"), 0)
            self.assertEqual(db.last_live_write()["status"], "failed")
            db.close()


if __name__ == "__main__":
    unittest.main()
