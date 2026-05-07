from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.extensions import register_all_routers
from app.startup.extensions import _init_social_clients, _register_workflow_actions


class _RecordingWorkflowEngine:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def register_action(self, name, handler) -> None:
        self.actions.append(name)


class ExperimentalSocialGateTests(unittest.TestCase):
    def test_social_routes_are_not_registered_by_default(self) -> None:
        app = FastAPI()
        app.state.settings = SimpleNamespace(experimental_social=False)
        register_all_routers(app)

        response = TestClient(app).post(
            "/social/empire/research",
            json={"niche": "browser tools", "subreddits": [], "yt_results": 1},
        )

        self.assertEqual(response.status_code, 404)

    def test_social_routes_register_only_when_experiment_enabled(self) -> None:
        app = FastAPI()
        app.state.settings = SimpleNamespace(experimental_social=True)
        app.state.viral_engine = None
        register_all_routers(app)

        response = TestClient(app).post(
            "/social/empire/research",
            json={"niche": "browser tools", "subreddits": [], "yt_results": 1},
        )

        self.assertEqual(response.status_code, 503)

    def test_social_clients_and_actions_are_disabled_by_default(self) -> None:
        app = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(experimental_social=False),
                workflow_engine=_RecordingWorkflowEngine(),
            )
        )

        _init_social_clients(app)
        _register_workflow_actions(app)

        self.assertIsNone(app.state.youtube_client)
        self.assertIsNone(app.state.veo3_client)
        self.assertIsNone(app.state.viral_engine)
        self.assertEqual(app.state.workflow_engine.actions, [])


if __name__ == "__main__":
    unittest.main()
