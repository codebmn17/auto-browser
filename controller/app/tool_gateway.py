from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from .approvals import ApprovalRequiredError
from .models import (
    AgentRunRequest,
    AgentStepRequest,
    ApprovalDecisionRequest,
    BrowserActionDecision,
    CreateSessionRequest,
    McpToolCallContent,
    McpToolCallRequest,
    McpToolCallResponse,
    McpToolDescriptor,
    SocialScrollRequest,
    SocialScrapeRequest,
    SocialPostRequest,
)


class EmptyInput(BaseModel):
    pass


class SessionIdInput(BaseModel):
    session_id: str


class ObserveInput(SessionIdInput):
    limit: int = Field(default=40, ge=1, le=100)


class ExecuteActionInput(SessionIdInput):
    approval_id: str | None = None
    action: BrowserActionDecision


class SaveAuthStateInput(SessionIdInput):
    path: str


class TakeoverInput(SessionIdInput):
    reason: str = "Manual review requested"


class ListDownloadsInput(SessionIdInput):
    pass


class ListTabsInput(SessionIdInput):
    pass


class TabActionInput(SessionIdInput):
    index: int = Field(ge=0)


class ApprovalIdInput(BaseModel):
    approval_id: str


class ApprovalDecisionInput(ApprovalIdInput):
    comment: str | None = Field(default=None, max_length=2000)


class ListApprovalsInput(BaseModel):
    status: str | None = None
    session_id: str | None = None


class ListAgentJobsInput(BaseModel):
    status: str | None = None
    session_id: str | None = None


class GetRemoteAccessInput(BaseModel):
    session_id: str | None = None


class AgentJobIdInput(BaseModel):
    job_id: str


class QueueAgentStepInput(SessionIdInput):
    request: AgentStepRequest


class QueueAgentRunInput(SessionIdInput):
    request: AgentRunRequest


class SocialScrollInput(SessionIdInput):
    direction: str = "down"
    screens: int = Field(default=3, ge=1, le=20)


class SocialScrapeInput(SessionIdInput):
    limit: int = Field(default=20, ge=1, le=100)


class SocialPostInput(SessionIdInput):
    text: str = Field(min_length=1, max_length=5000)
    image_path: str | None = None


class SocialLikeInput(SessionIdInput):
    post_index: int = Field(default=0, ge=0, le=50)


class SocialSearchInput(SessionIdInput):
    query: str = Field(min_length=1, max_length=500)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[dict[str, Any] | list[dict[str, Any]]]]


class McpToolGateway:
    def __init__(self, *, manager, orchestrator, job_queue):
        self.manager = manager
        self.orchestrator = orchestrator
        self.job_queue = job_queue
        self._tools = {
            spec.name: spec
            for spec in [
                ToolSpec(
                    name="browser.create_session",
                    description="Create a new browser session and optionally navigate to a start URL.",
                    input_model=CreateSessionRequest,
                    handler=self._create_session,
                ),
                ToolSpec(
                    name="browser.list_sessions",
                    description="List live and persisted browser sessions.",
                    input_model=EmptyInput,
                    handler=self._list_sessions,
                ),
                ToolSpec(
                    name="browser.get_session",
                    description="Get one browser session summary.",
                    input_model=SessionIdInput,
                    handler=self._get_session,
                ),
                ToolSpec(
                    name="browser.observe",
                    description="Capture the current browser observation with screenshot, interactables, and perception summary.",
                    input_model=ObserveInput,
                    handler=self._observe,
                ),
                ToolSpec(
                    name="browser.list_downloads",
                    description="List files captured from browser downloads for one session.",
                    input_model=ListDownloadsInput,
                    handler=self._list_downloads,
                ),
                ToolSpec(
                    name="browser.list_tabs",
                    description="List currently open tabs/pages for one session.",
                    input_model=ListTabsInput,
                    handler=self._list_tabs,
                ),
                ToolSpec(
                    name="browser.activate_tab",
                    description="Switch the active session page to one tab index.",
                    input_model=TabActionInput,
                    handler=self._activate_tab,
                ),
                ToolSpec(
                    name="browser.close_tab",
                    description="Close one tab index if more than one tab is open.",
                    input_model=TabActionInput,
                    handler=self._close_tab,
                ),
                ToolSpec(
                    name="browser.execute_action",
                    description="Execute one browser action using the shared internal action schema.",
                    input_model=ExecuteActionInput,
                    handler=self._execute_action,
                ),
                ToolSpec(
                    name="browser.save_auth_state",
                    description="Save session storage state to the per-session auth-state root.",
                    input_model=SaveAuthStateInput,
                    handler=self._save_auth_state,
                ),
                ToolSpec(
                    name="browser.request_human_takeover",
                    description="Ask for a human to take over the shared browser desktop.",
                    input_model=TakeoverInput,
                    handler=self._takeover,
                ),
                ToolSpec(
                    name="browser.close_session",
                    description="Close a session and finalize its trace/artifacts.",
                    input_model=SessionIdInput,
                    handler=self._close_session,
                ),
                ToolSpec(
                    name="browser.list_approvals",
                    description="List pending or historical approval items.",
                    input_model=ListApprovalsInput,
                    handler=self._list_approvals,
                ),
                ToolSpec(
                    name="browser.approve_approval",
                    description="Approve a pending approval item.",
                    input_model=ApprovalDecisionInput,
                    handler=self._approve_approval,
                ),
                ToolSpec(
                    name="browser.reject_approval",
                    description="Reject a pending approval item.",
                    input_model=ApprovalDecisionInput,
                    handler=self._reject_approval,
                ),
                ToolSpec(
                    name="browser.execute_approval",
                    description="Execute an already approved action.",
                    input_model=ApprovalIdInput,
                    handler=self._execute_approval,
                ),
                ToolSpec(
                    name="browser.list_agent_jobs",
                    description="List queued or completed browser-agent jobs.",
                    input_model=ListAgentJobsInput,
                    handler=self._list_agent_jobs,
                ),
                ToolSpec(
                    name="browser.get_agent_job",
                    description="Read one browser-agent job record.",
                    input_model=AgentJobIdInput,
                    handler=self._get_agent_job,
                ),
                ToolSpec(
                    name="browser.queue_agent_step",
                    description="Queue one agent step for background execution.",
                    input_model=QueueAgentStepInput,
                    handler=self._queue_agent_step,
                ),
                ToolSpec(
                    name="browser.queue_agent_run",
                    description="Queue a short agent loop for background execution.",
                    input_model=QueueAgentRunInput,
                    handler=self._queue_agent_run,
                ),
                ToolSpec(
                    name="browser.list_providers",
                    description="List configured model providers for browser-agent orchestration.",
                    input_model=EmptyInput,
                    handler=self._list_providers,
                ),
                ToolSpec(
                    name="browser.get_remote_access",
                    description="Read current remote-access metadata for takeover/API forwarding.",
                    input_model=GetRemoteAccessInput,
                    handler=self._get_remote_access,
                ),
                ToolSpec(
                    name="social.scroll_feed",
                    description="Smoothly scroll the current page feed up or down by N screens using human-paced motion.",
                    input_model=SocialScrollInput,
                    handler=self._social_scroll,
                ),
                ToolSpec(
                    name="social.extract_posts",
                    description="Scrape visible feed posts from the current page. Returns structured list of {text, links, images, y_position}.",
                    input_model=SocialScrapeInput,
                    handler=self._social_extract_posts,
                ),
                ToolSpec(
                    name="social.extract_profile",
                    description="Extract profile info (username, bio, followers, following, avatar) from the current page.",
                    input_model=SessionIdInput,
                    handler=self._social_extract_profile,
                ),
                ToolSpec(
                    name="social.post",
                    description=(
                        "Find the text composer on the current page (tweet box, post field, comment box) "
                        "and type + submit the provided text with human-like delays. "
                        "Navigate to the platform first, then call this tool."
                    ),
                    input_model=SocialPostInput,
                    handler=self._social_post,
                ),
                ToolSpec(
                    name="social.like",
                    description="Find and click the like/heart button for a visible post. Use post_index to target a specific post (0 = first).",
                    input_model=SocialLikeInput,
                    handler=self._social_like,
                ),
                ToolSpec(
                    name="social.follow",
                    description="Find and click the Follow button on the current profile page.",
                    input_model=SessionIdInput,
                    handler=self._social_follow,
                ),
                ToolSpec(
                    name="social.search",
                    description="Find the search input on the current page and type a query, then press Enter.",
                    input_model=SocialSearchInput,
                    handler=self._social_search,
                ),
            ]
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            McpToolDescriptor(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_model.model_json_schema(),
            ).model_dump()
            for spec in self._tools.values()
        ]

    async def call_tool(self, payload: McpToolCallRequest) -> McpToolCallResponse:
        spec = self._tools.get(payload.name)
        if spec is None:
            return self._error_response(f"Unknown tool: {payload.name}")

        try:
            arguments = spec.input_model.model_validate(payload.arguments)
            result = await spec.handler(arguments)
            return McpToolCallResponse(
                content=[McpToolCallContent(text=json.dumps(result, ensure_ascii=False))],
                structuredContent=result,
                isError=False,
            )
        except ApprovalRequiredError as exc:
            detail = exc.payload
            return McpToolCallResponse(
                content=[McpToolCallContent(text=json.dumps(detail, ensure_ascii=False))],
                structuredContent=detail,
                isError=True,
            )
        except Exception as exc:
            return self._error_response(str(exc))

    @staticmethod
    def _error_response(message: str) -> McpToolCallResponse:
        return McpToolCallResponse(
            content=[McpToolCallContent(text=message)],
            structuredContent={"error": message},
            isError=True,
        )

    async def _create_session(self, payload: CreateSessionRequest) -> dict[str, Any]:
        return await self.manager.create_session(
            name=payload.name,
            start_url=payload.start_url,
            storage_state_path=payload.storage_state_path,
            request_proxy_server=payload.proxy_server,
            request_proxy_username=payload.proxy_username,
            request_proxy_password=payload.proxy_password,
            user_agent=payload.user_agent,
            stealth_enabled=payload.stealth,
        )

    async def _list_sessions(self, _: EmptyInput) -> list[dict[str, Any]]:
        return await self.manager.list_sessions()

    async def _get_session(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.get_session_record(payload.session_id)

    async def _observe(self, payload: ObserveInput) -> dict[str, Any]:
        return await self.manager.observe(payload.session_id, limit=payload.limit)

    async def _list_downloads(self, payload: ListDownloadsInput) -> list[dict[str, Any]]:
        return await self.manager.list_downloads(payload.session_id)

    async def _list_tabs(self, payload: ListTabsInput) -> list[dict[str, Any]]:
        return await self.manager.list_tabs(payload.session_id)

    async def _activate_tab(self, payload: TabActionInput) -> dict[str, Any]:
        return await self.manager.activate_tab(payload.session_id, payload.index)

    async def _close_tab(self, payload: TabActionInput) -> dict[str, Any]:
        return await self.manager.close_tab(payload.session_id, payload.index)

    async def _execute_action(self, payload: ExecuteActionInput) -> dict[str, Any]:
        return await self.manager.execute_decision(
            payload.session_id,
            payload.action,
            approval_id=payload.approval_id,
        )

    async def _save_auth_state(self, payload: SaveAuthStateInput) -> dict[str, Any]:
        return await self.manager.save_storage_state(payload.session_id, payload.path)

    async def _takeover(self, payload: TakeoverInput) -> dict[str, Any]:
        return await self.manager.request_human_takeover(payload.session_id, payload.reason)

    async def _close_session(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.close_session(payload.session_id)

    async def _list_approvals(self, payload: ListApprovalsInput) -> list[dict[str, Any]]:
        return await self.manager.list_approvals(status=payload.status, session_id=payload.session_id)

    async def _approve_approval(self, payload: ApprovalDecisionInput) -> dict[str, Any]:
        return await self.manager.approve(payload.approval_id, comment=payload.comment)

    async def _reject_approval(self, payload: ApprovalDecisionInput) -> dict[str, Any]:
        return await self.manager.reject(payload.approval_id, comment=payload.comment)

    async def _execute_approval(self, payload: ApprovalIdInput) -> dict[str, Any]:
        return await self.manager.execute_approval(payload.approval_id)

    async def _list_agent_jobs(self, payload: ListAgentJobsInput) -> list[dict[str, Any]]:
        return await self.job_queue.list_jobs(status=payload.status, session_id=payload.session_id)

    async def _get_agent_job(self, payload: AgentJobIdInput) -> dict[str, Any]:
        return await self.job_queue.get_job(payload.job_id)

    async def _queue_agent_step(self, payload: QueueAgentStepInput) -> dict[str, Any]:
        await self.manager.get_session(payload.session_id)
        return await self.job_queue.enqueue_step(payload.session_id, payload.request)

    async def _queue_agent_run(self, payload: QueueAgentRunInput) -> dict[str, Any]:
        await self.manager.get_session(payload.session_id)
        return await self.job_queue.enqueue_run(payload.session_id, payload.request)

    async def _list_providers(self, _: EmptyInput) -> list[dict[str, Any]]:
        return [item.model_dump() for item in self.orchestrator.list_providers()]

    async def _get_remote_access(self, payload: GetRemoteAccessInput) -> dict[str, Any]:
        if payload.session_id and payload.session_id not in self.manager.sessions:
            record = await self.manager.get_session_record(payload.session_id)
            return record["remote_access"]
        return self.manager.get_remote_access_info(payload.session_id)

    async def _social_scroll(self, payload: SocialScrollInput) -> dict[str, Any]:
        return await self.manager.scroll_feed(
            payload.session_id,
            direction=payload.direction,
            screens=payload.screens,
        )

    async def _social_extract_posts(self, payload: SocialScrapeInput) -> list[dict[str, Any]]:
        return await self.manager.extract_posts(payload.session_id, limit=payload.limit)

    async def _social_extract_profile(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.extract_profile(payload.session_id)

    async def _social_post(self, payload: SocialPostInput) -> dict[str, Any]:
        return await self.manager.post_content(payload.session_id, text=payload.text)

    async def _social_like(self, payload: SocialLikeInput) -> dict[str, Any]:
        return await self.manager.like_post(payload.session_id, post_index=payload.post_index)

    async def _social_follow(self, payload: SessionIdInput) -> dict[str, Any]:
        return await self.manager.follow_user(payload.session_id)

    async def _social_search(self, payload: SocialSearchInput) -> dict[str, Any]:
        return await self.manager.search_page(payload.session_id, query=payload.query)
