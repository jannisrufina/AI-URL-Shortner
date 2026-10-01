import getpass
import json
from pathlib import Path
from typing import Any

import pytest

import scripts.export_openapi as exporter
from scripts.export_openapi import _SPEC_PATH, generate_spec, render_spec


def test_committed_openapi_matches_generated_spec() -> None:
    committed = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))

    assert committed == generate_spec()


def test_openapi_has_only_expected_paths_and_methods() -> None:
    spec = generate_spec()
    paths = spec["paths"]

    assert set(paths) == {"/", "/api/links", "/{code}"}
    assert {method for method in paths["/"]} == {"get", "post"}
    assert set(paths["/api/links"]) == {"post"}
    assert set(paths["/{code}"]) == {"get"}


def test_json_create_documents_body_statuses_and_shared_error_schema() -> None:
    spec = generate_spec()
    operation = spec["paths"]["/api/links"]["post"]
    request_body = operation["requestBody"]
    body_schema = request_body["content"]["application/json"]["schema"]
    responses = operation["responses"]

    assert request_body["required"] is True
    assert body_schema["required"] == ["url"]
    assert set(body_schema["properties"]) == {"url", "expires_at"}
    assert body_schema["properties"]["url"]["type"] == "string"
    assert "string" in body_schema["properties"]["expires_at"]["type"]
    assert {"200", "422", "429", "503"} <= responses.keys()
    assert "both new and reused" in responses["200"]["description"]
    assert responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/CreateLinkResponse"
    )
    _assert_error_component_references(spec, responses, ("422", "429", "503"))


def test_redirect_documents_code_pattern_location_and_error_responses() -> None:
    spec = generate_spec()
    operation = spec["paths"]["/{code}"]["get"]
    code_parameter = next(
        parameter
        for parameter in operation["parameters"]
        if parameter["name"] == "code"
    )
    responses = operation["responses"]

    assert code_parameter["in"] == "path"
    assert code_parameter["required"] is True
    assert code_parameter["schema"]["pattern"] == "^[A-Za-z0-9]{7}$"
    assert {"302", "404", "503"} <= responses.keys()
    assert "Location" in responses["302"]["headers"]
    _assert_error_component_references(spec, responses, ("404", "503"))


def test_form_routes_describe_html_and_encoded_fields() -> None:
    spec = generate_spec()
    get_root = spec["paths"]["/"]["get"]
    post_root = spec["paths"]["/"]["post"]
    form_body = post_root["requestBody"]
    form_schema = form_body["content"]["application/x-www-form-urlencoded"]["schema"]

    assert "text/html" in get_root["responses"]["200"]["content"]
    assert form_body["required"] is True
    assert form_schema["required"] == ["url"]
    assert set(form_schema["properties"]) == {"url", "expires_at"}
    assert {"200", "422", "429", "503"} <= post_root["responses"].keys()
    assert all(
        "text/html" in post_root["responses"][code]["content"]
        for code in ("200", "422", "429", "503")
    )


def test_rendering_is_byte_deterministic() -> None:
    first = render_spec()
    second = render_spec()

    assert first == second
    assert first.endswith(b"\n")
    assert b"\r\n" not in first


def test_check_mode_uses_fixed_message_and_never_prints_spec(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "openapi.json"
    monkeypatch.setattr(exporter, "_SPEC_PATH", artifact)
    artifact.write_bytes(render_spec())

    assert exporter.main(["--check"]) == 0
    assert capsys.readouterr().out == ""

    artifact.write_text("{}\n", encoding="utf-8")
    assert exporter.main(["--check"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == exporter._STALE_MESSAGE + "\n"


def test_spec_has_no_environment_or_machine_specific_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass123@localhost/db")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://C:\\private\\path")

    rendered = render_spec().decode("utf-8")

    assert "pass123" not in rendered
    assert "password" not in rendered
    assert "C:\\" not in rendered
    assert getpass.getuser() not in rendered


def _assert_error_component_references(
    spec: dict[str, Any],
    responses: dict[str, Any],
    response_codes: tuple[str, ...],
) -> None:
    error_schema = spec["components"]["schemas"]["Error"]
    expected_reference = "#/components/schemas/Error"
    assert error_schema["properties"]["error"]["$ref"].endswith("/ErrorDetail")
    for response_code in response_codes:
        response = responses[response_code]
        reference = response["content"]["application/json"]["schema"]["$ref"]
        assert reference == expected_reference
