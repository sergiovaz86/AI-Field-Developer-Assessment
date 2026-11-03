"""Static deployment contracts. Container execution is a separate check."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _text(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def test_compose_is_production_and_keeps_task1_on_stdio() -> None:
    compose = _text("compose.yaml")
    assert "GATEWAY_MODE: production" in compose
    assert "assessment_mock" not in compose.split("services:", 1)[1]
    assert "dockerfile: Dockerfile.task1" in compose
    assert "stdin_open: true" in compose
    assert "8080:8080" in compose
    assert "condition: service_completed_successfully" in compose
    assert "http://127.0.0.1:8080/health" in compose
    assert "working_dir: /app" in compose
    assert compose.count("http://downstream:9000/mcp") == 2
    assert "scripts/probe_http.py" in compose
    assert "http://127.0.0.1:9000/health" in compose
    assert "9000:9000" not in compose
    task1 = compose.split("\n  task1:\n", 1)[1]
    assert "ports:" not in task1
    downstream = compose.split("\n  downstream:\n", 1)[1].split("\n  task1:\n", 1)[0]
    assert "ports:" not in downstream


def test_acceptance_overlay_keeps_production_and_uses_internal_mocks() -> None:
    base = _text("compose.yaml")
    overlay = _text("compose.acceptance.yaml")
    assert "GATEWAY_MODE: production" in base
    assert "compose.acceptance.yaml" not in base
    assert "GATEWAY_MODE: production" in overlay
    assert "assessment_mock" not in overlay
    assert "http://llm-primary:9100" in overlay
    assert "http://llm-backup:9100" in overlay
    assert "primary.example" not in overlay
    assert "!override" in overlay
    assert "8081:8080" in overlay
    assert "acceptance=acceptance-token-value" in overlay


def test_images_drop_privileges_and_separate_sources() -> None:
    gateway = _text("Dockerfile.gateway")
    task1 = _text("Dockerfile.task1")
    assert "useradd --system --uid 10001 --gid gateway" in gateway
    assert "useradd --system --uid 10001 --gid gateway" in task1
    assert 'ENTRYPOINT ["python", "scripts/container_entrypoint.py"]' in gateway
    assert "EXPOSE 8080" in gateway
    assert "COPY task1_mcp_server" not in gateway
    assert "USER gateway" in task1
    assert "EXPOSE" not in task1
    assert "COPY gateway_service" not in task1
    assert "COPY database" not in task1
    entrypoint = _text("scripts/container_entrypoint.py")
    assert "os.setuid" in entrypoint
    assert "init_database" not in entrypoint


def test_ignore_rules_keep_secrets_and_runtime_files_out_of_images() -> None:
    ignored = _text(".dockerignore")
    for name in (".env", ".runtime", "*.db", "tests", "docs"):
        assert name in ignored
