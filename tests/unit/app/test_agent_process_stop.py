import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from copaw.app.routers.agent import stop_agent_process


@pytest.mark.asyncio
async def test_stop_agent_process_uses_authenticated_user_and_session():
    calls = []

    class RuntimeApp:
        async def stop_chat(self, user_id: str, session_id: str) -> None:
            calls.append((user_id, session_id))

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_runtime_app=RuntimeApp()),
        ),
    )

    result = await stop_agent_process(
        request,
        {"session_id": "home-session"},
        "account-uuid",
    )

    assert result == {"stopped": True}
    assert calls == [("account-uuid", "home-session")]


@pytest.mark.asyncio
async def test_stop_agent_process_requires_session_id():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(HTTPException) as exc_info:
        await stop_agent_process(request, {}, "account-uuid")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_stop_agent_process_cancels_same_instance_worker_immediately():
    session_id = "home-session"
    user_id = "account-uuid"
    task_id = f"{user_id}:{session_id}"
    worker = asyncio.create_task(asyncio.sleep(60))

    class RuntimeApp:
        _local_tasks = {task_id: worker}

        @staticmethod
        def _get_interrupt_key(current_user_id: str, current_session_id: str):
            return f"{current_user_id}:{current_session_id}"

        async def stop_chat(self, current_user_id: str, current_session_id: str):
            return None

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agent_runtime_app=RuntimeApp()),
        ),
    )

    result = await stop_agent_process(
        request,
        {"session_id": session_id},
        user_id,
    )

    assert result == {"stopped": True}
    assert worker.cancelling()
    with pytest.raises(asyncio.CancelledError):
        await worker
