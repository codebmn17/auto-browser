from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.approvals import ApprovalRequiredError
from app.models import ApprovalRecord, BrowserActionDecision, McpToolCallRequest, ProviderInfo
from app.tool_gateway import McpToolGateway


class ToolGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = SimpleNamespace(
            create_session=AsyncMock(return_value={"id": "session-1"}),
            list_sessions=AsyncMock(return_value=[{"id": "session-1"}]),
            get_session_record=AsyncMock(return_value={"id": "session-1", "status": "active"}),
            observe=AsyncMock(return_value={"session": {"id": "session-1"}, "url": "https://example.com"}),
            list_tabs=AsyncMock(return_value=[{"index": 0, "active": True, "url": "https://example.com"}]),
            activate_tab=AsyncMock(return_value={"index": 1, "tabs": [{"index": 1, "active": True}]}),
            close_tab=AsyncMock(return_value={"closed_index": 1, "tabs": [{"index": 0, "active": True}]}),
            list_downloads=AsyncMock(return_value=[{"filename": "report.csv"}]),
            execute_decision=AsyncMock(return_value={"action": "click", "verification": {"verified": True}}),
            save_storage_state=AsyncMock(return_value={"saved_to": "/data/auth/session-1/state.json.enc"}),
            request_human_takeover=AsyncMock(return_value={"takeover_url": "http://127.0.0.1:6080/vnc.html"}),
            close_session=AsyncMock(return_value={"closed": True}),
            list_approvals=AsyncMock(return_value=[]),
            approve=AsyncMock(return_value={"id": "approval-1", "status": "approved"}),
            reject=AsyncMock(return_value={"id": "approval-1", "status": "rejected"}),
            execute_approval=AsyncMock(return_value={"approval": {"id": "approval-1", "status": "executed"}}),
            get_remote_access_info=lambda: {"active": False, "status": "inactive"},
            get_session=AsyncMock(return_value={"id": "session-1"}),
        )
        self.orchestrator = SimpleNamespace(
            list_providers=lambda: [ProviderInfo(provider="openai", configured=True, model="gpt-4.1-mini")]
        )
        self.job_queue = SimpleNamespace(
            list_jobs=AsyncMock(return_value=[]),
            get_job=AsyncMock(return_value={"id": "job-1", "status": "completed"}),
            enqueue_step=AsyncMock(return_value={"id": "job-1", "kind": "agent_step"}),
            enqueue_run=AsyncMock(return_value={"id": "job-2", "kind": "agent_run"}),
        )
        self.gateway = McpToolGateway(
            manager=self.manager,
            orchestrator=self.orchestrator,
            job_queue=self.job_queue,
        )

    async def test_list_tools_includes_expected_browser_tools(self) -> None:
        tools = self.gateway.list_tools()
        names = {tool["name"] for tool in tools}

        self.assertIn("browser.create_session", names)
        self.assertIn("browser.list_tabs", names)
        self.assertIn("browser.list_downloads", names)
        self.assertIn("browser.execute_action", names)
        self.assertIn("browser.list_agent_jobs", names)
        self.assertIn("browser.get_remote_access", names)
        self.assertEqual(len(names), len(tools))

    async def test_execute_action_tool_returns_structured_payload(self) -> None:
        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.execute_action",
                arguments={
                    "session_id": "session-1",
                    "action": {
                        "action": "click",
                        "reason": "Click the main CTA",
                        "element_id": "op-123",
                    },
                },
            )
        )

        self.assertFalse(response.isError)
        self.assertEqual(response.structuredContent["action"], "click")
        self.manager.execute_decision.assert_awaited_once()
        called_args = self.manager.execute_decision.await_args.args
        self.assertEqual(called_args[0], "session-1")
        self.assertIsInstance(called_args[1], BrowserActionDecision)
        self.assertEqual(called_args[1].element_id, "op-123")

    async def test_approval_required_bubbles_back_as_tool_error(self) -> None:
        approval = ApprovalRecord(
            id="approval-1",
            session_id="session-1",
            kind="payment",
            status="pending",
            created_at="2026-03-09T00:00:00Z",
            updated_at="2026-03-09T00:00:00Z",
            reason="Payment requires approval",
            action=BrowserActionDecision(
                action="click",
                reason="Submit payment",
                element_id="op-pay",
                risk_category="payment",
            ),
        )
        self.manager.execute_decision = AsyncMock(side_effect=ApprovalRequiredError(approval))

        response = await self.gateway.call_tool(
            McpToolCallRequest(
                name="browser.execute_action",
                arguments={
                    "session_id": "session-1",
                    "action": {
                        "action": "click",
                        "reason": "Submit payment",
                        "element_id": "op-pay",
                        "risk_category": "payment",
                    },
                },
            )
        )

        self.assertTrue(response.isError)
        self.assertEqual(response.structuredContent["status"], "approval_required")
        self.assertEqual(response.structuredContent["approval"]["id"], "approval-1")
