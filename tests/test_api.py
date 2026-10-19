import pytest
from conftest import ScriptedModel
from fastapi.testclient import TestClient
from langchain_core import exceptions as model_errors

from entrypoints.api import create_app


def test_http_extract_restore_and_clear_share_the_same_session(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    app = create_app(directory=tmp_path, model_factory=lambda: model, ui=False)
    with TestClient(app) as client:
        response = client.post("/extractions", json={"text": "重复扣款，已发起退款。"})
        assert response.status_code == 200
        result = response.json()
        assert result["outcome"] == "SUCCESS" and result["data"]["customer_id"] == "CUST-98234"
        session_id = result["session_id"]
        restored = client.get(f"/sessions/{session_id}")
        assert restored.status_code == 200
        assert restored.json()["sources"][0]["text"] == "重复扣款，已发起退款。"
        assert client.delete(f"/sessions/{session_id}").status_code == 204
        assert client.get(f"/sessions/{session_id}").status_code == 410
        assert len(model.calls) == 1


def test_http_fallback_is_durable_and_raw_evidence_requires_explicit_opt_in(tmp_path):
    model = ScriptedModel(responses=["密码是 FakePassword123", "bad", "bad"])
    app = create_app(directory=tmp_path, model_factory=lambda: model, ui=False)
    with TestClient(app) as client:
        result = client.post("/extractions", json={"text": "密码是 FakePassword123。支付失败。"})
        assert result.status_code == 200
        body = result.json()
        assert body["outcome"] == "FALLBACK" and body["data"] is None
        reviews = client.get("/reviews").json()
        assert reviews[0]["review_id"] == body["review_id"]
        safe = client.get(f"/reviews/{body['review_id']}")
        assert safe.status_code == 200 and "FakePassword123" not in safe.text
        raw = client.get(f"/reviews/{body['review_id']}?include_raw=true")
        assert "FakePassword123" in raw.text
        assert len(raw.json()["attempts"]) == 3
        assert client.get("/reviews/missing").status_code == 404


def test_http_rejections_do_not_expose_request_values_or_call_model(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    with TestClient(create_app(directory=tmp_path, model_factory=lambda: model, ui=False,
                               max_source_chars=10)) as client:
        for payload, code in [({"text": "x" * 11}, 413),
                              ({"record_id": "unauthorized"}, 403),
                              ({"text": ""}, 422),
                              ({"text": "密码是 NeverEcho123", "extra": "NeverEcho123"}, 422)]:
            response = client.post("/extractions", json=payload)
            assert response.status_code == code
            assert response.json()["request_id"]
            assert "NeverEcho123" not in response.text
        assert model.calls == []


def test_app_passes_the_configured_session_ttl_to_the_store(tmp_path):
    model = ScriptedModel(responses=[])
    app = create_app(
        directory=tmp_path, model_factory=lambda: model, ui=False, session_ttl_days=1
    )
    with TestClient(app) as client:
        assert client.app.state.service.store.session_ttl == 86400


def test_http_message_limit_returns_413_with_a_distinct_error_code(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    app = create_app(
        directory=tmp_path,
        model_factory=lambda: model,
        ui=False,
        max_message_chars=10,
    )
    with TestClient(app) as client:
        accepted = client.post("/extractions", json={"text": "x" * 10})
        assert accepted.status_code == 200
        session_id = accepted.json()["session_id"]
        response = client.post(
            "/extractions", json={"text": "x" * 11, "session_id": session_id}
        )
        assert response.status_code == 413
        assert response.json()["error_code"] == "MESSAGE_TOO_LARGE"
        assert response.json()["request_id"]
        sources = client.get(f"/sessions/{session_id}").json()["sources"]
        assert len(sources) == 1
    assert len(model.calls) == 1


@pytest.mark.parametrize("value", [0, -1, "1000"])
def test_create_app_rejects_an_invalid_message_limit(tmp_path, value):
    with pytest.raises(ValueError, match="max_message_chars"):
        create_app(
            directory=tmp_path,
            model_factory=lambda: None,
            ui=False,
            max_message_chars=value,
        )


def test_ui_is_mounted_beside_the_api_routes(tmp_path, valid_json):
    model = ScriptedModel(responses=[valid_json])
    app = create_app(directory=tmp_path, model_factory=lambda: model, ui=True)
    with TestClient(app) as client:
        page = client.get("/ui/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        response = client.post("/extractions", json={"text": "重复扣款，已发起退款。"})
        assert response.status_code == 200
        assert response.json()["outcome"] == "SUCCESS"
        assert len(model.calls) == 1


def test_missing_model_configuration_returns_explicit_service_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with TestClient(create_app(directory=tmp_path, ui=False)) as client:
        response = client.post("/extractions", json={"text": "支付失败。"})
        assert response.status_code == 500
        assert response.json()["error_code"] == "MODEL_NOT_CONFIGURED"
        assert response.json()["source_saved"] is True
        assert client.get("/reviews").json() == []


def test_http_authentication_failure_is_a_visible_fallback(tmp_path):
    model = ScriptedModel(responses=[model_errors.ModelAuthenticationError("bad key")])
    app = create_app(directory=tmp_path, model_factory=lambda: model, ui=False)
    with TestClient(app) as client:
        response = client.post("/extractions", json={"text": "支付失败。"})
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == "FALLBACK" and body["data"] is None
        assert body["error_code"] == "MODEL_CALL_FAILED"
        assert body["model_calls"] == 1 and body["repair_count"] == 0
        reviews = client.get("/reviews").json()
        assert reviews[0]["review_id"] == body["review_id"]
        detail = client.get(f"/reviews/{body['review_id']}").json()
        assert detail["error_code"] == "MODEL_CALL_FAILED"
        assert detail["attempts"][0]["error_code"] == "MODEL_CALL_FAILED"
        assert detail["attempts"][0]["latency_ms"] >= 0
        assert len(model.calls) == 1


def test_http_model_configuration_failure_has_no_review(tmp_path):
    model = ScriptedModel(responses=[model_errors.ModelNotFoundError("model missing")])
    app = create_app(directory=tmp_path, model_factory=lambda: model, ui=False)
    with TestClient(app) as client:
        response = client.post("/extractions", json={"text": "支付失败。"})
        assert response.status_code == 500
        body = response.json()
        assert body["error_code"] == "MODEL_CONFIG_ERROR"
        assert body["request_id"]
        assert body["source_saved"] is True
        assert client.get("/reviews").json() == []
        assert len(model.calls) == 1
