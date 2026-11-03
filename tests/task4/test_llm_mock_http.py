"""Primary returns 429. Backup streams the scripted email in separate frames."""

from __future__ import annotations

import threading

import httpx

from gateway_service.mocks.llm_http import bind


def test_primary_is_429_and_backup_splits_the_email_script() -> None:
    primary = bind("127.0.0.1", 0, "primary")
    backup = bind("127.0.0.1", 0, "backup")
    threads = [
        threading.Thread(target=primary.serve_forever, daemon=True),
        threading.Thread(target=backup.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        primary_url = _url(primary)
        backup_url = _url(backup)
        payload = {
            "model": "mock-email-parts",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        }
        with httpx.Client(timeout=2.0) as client:
            rejected = client.post(f"{primary_url}/chat/completions", json=payload)
            streamed = client.post(f"{backup_url}/chat/completions", json=payload)
        assert rejected.status_code == 429
        assert streamed.status_code == 200
        text = streamed.text
        assert '"content":"alex"' in text
        assert '"content":"@"' in text
        assert '"content":"example"' in text
        assert '"content":"."' in text
        assert '"content":"com"' in text
        assert text.index('"content":"alex"') < text.index('"content":"com"')
    finally:
        primary.shutdown()
        backup.shutdown()
        for thread in threads:
            thread.join(timeout=2)
        primary.server_close()
        backup.server_close()


def _url(server: object) -> str:
    host, port = server.server_address  # type: ignore[attr-defined]
    return f"http://{host}:{port}"
