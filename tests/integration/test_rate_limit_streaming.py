"""Streaming quota, cancellation, and isolated tenant accounting."""

from __future__ import annotations

import asyncio

from tests.task4.conftest import (
    AUTH,
    OTHER_AUTH,
    ScriptedUpstream,
    client_for,
    completion,
    gateway,
    rows,
)

from gateway_service.mocks.stream_scenarios import render_completion


def _frames(model: str = "mock-plain") -> list[bytes]:
    return render_completion({"model": model, "messages": [{"role": "user", "content": "hello"}]})


async def test_concurrent_streams_keep_separate_tenant_ledgers(tmp_path) -> None:
    primary = ScriptedUpstream(_frames())
    backup = ScriptedUpstream()
    application, _store, database = gateway(tmp_path, primary, backup, quota=50_000)

    async def once(headers: dict[str, str]) -> tuple[int, bytes]:
        async with client_for(application) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=completion(),
                headers=headers,
            )
        return response.status_code, response.content

    results = await asyncio.gather(once(AUTH), once(OTHER_AUTH))
    assert [status for status, _body in results] == [200, 200]
    stored = rows(database)
    assert len(stored) == 2
    assert len({row["tenant_id"] for row in stored}) == 2
    assert {row["charged_tokens"] for row in stored} == {2}, (
        [(row["status"], row["charged_tokens"], row["actual_input_tokens"]) for row in stored],
        [body[:180] for _status, body in results],
    )
    assert backup.calls == 0


async def test_client_cancellation_closes_the_provider_and_releases_quota(tmp_path) -> None:
    started = asyncio.Event()
    released = asyncio.Event()

    class BlockingUpstream(ScriptedUpstream):
        async def _iterate(self):
            self.calls += 1
            try:
                frame = _frames()[0]
                started.set()
                yield frame
                await asyncio.Event().wait()
            finally:
                self.closed += 1
                released.set()

    primary = BlockingUpstream()
    application, _store, database = gateway(
        tmp_path,
        primary,
        ScriptedUpstream(),
        timeout_ms=3000,
    )

    async def consume() -> None:
        async with client_for(application) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json=completion(),
                headers=AUTH,
            ) as response:
                async for _chunk in response.aiter_bytes():
                    await asyncio.Event().wait()

    request_task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=2)
    request_task.cancel()
    try:
        await asyncio.wait_for(request_task, timeout=2)
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(released.wait(), timeout=2)

    async def charge_released() -> None:
        while True:
            stored_rows = rows(database)
            if stored_rows and stored_rows[0]["status"] == "released":
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(charge_released(), timeout=2)
    stored = rows(database)
    stored = rows(database)
    assert stored[0]["status"] == "released"
    assert stored[0]["charged_tokens"] == 0
    assert primary.closed == 1
