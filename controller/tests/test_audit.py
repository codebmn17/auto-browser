from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.audit import AuditStore, reset_current_operator, set_current_operator


class AuditStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_and_filter_events(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = AuditStore(Path(tempdir))
            await store.startup()

            token = set_current_operator("operator-1", name="Alice")
            try:
                await store.append(
                    event_type="session_created",
                    status="ok",
                    action="create_session",
                    session_id="session-1",
                    details={"start_url": "https://example.com"},
                )
            finally:
                reset_current_operator(token)

            token = set_current_operator("operator-2", name="Bob")
            try:
                await store.append(
                    event_type="browser_action",
                    status="ok",
                    action="click",
                    session_id="session-2",
                )
            finally:
                reset_current_operator(token)

            all_events = await store.list(limit=10)
            self.assertEqual(len(all_events), 2)
            self.assertEqual(all_events[0].operator.id, "operator-2")

            session_events = await store.list(limit=10, session_id="session-1")
            self.assertEqual(len(session_events), 1)
            self.assertEqual(session_events[0].details["start_url"], "https://example.com")

            operator_events = await store.list(limit=10, operator_id="operator-1")
            self.assertEqual(len(operator_events), 1)
            self.assertEqual(operator_events[0].operator.name, "Alice")
