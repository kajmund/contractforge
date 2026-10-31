"""Tests for the provider boundary.

All HTTP tests use ``httpx.MockTransport``: no test here makes a network call.
The Cerebras adapter is exercised through its public ``complete`` behaviour
(HTTP contract, retries, usage/latency/raw) and the mock provider is tested for
determinism and factual fidelity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from contractforge.config import DEFAULT_CEREBRAS_BASE_URL, Settings
from contractforge.errors import ConfigError, GenerationError, ProviderError
from contractforge.models import Language, LLMRequest, Scenario
from contractforge.providers import CerebrasProvider, MockProvider, create_provider
from contractforge.providers.base import Provider

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:  # pragma: no cover - conftest already does this
    sys.path.insert(0, str(TESTS_DIR))

from factories import make_consulting_scenario  # noqa: E402

BASE_URL = DEFAULT_CEREBRAS_BASE_URL
CHAT_URL = f"{BASE_URL}/chat/completions"
OK_TEXT = '{"document_id": "main_agreement", "clauses": []}'


def _request(**overrides) -> LLMRequest:
    payload = {
        "provider": "cerebras",
        "model": "gpt-oss-120b",
        "prompt_version": "v1",
        "system": "system instructions",
        "user": "user instructions",
        "response_format": "json",
    }
    payload.update(overrides)
    return LLMRequest(**payload)


def _cerebras(handler, **overrides) -> CerebrasProvider:
    kwargs = {
        "api_key": "test-key",
        "base_url": BASE_URL,
        "model": "gpt-oss-120b",
        "timeout_s": 5.0,
        "max_retries": 0,
        "backoff_s": 0.0,
        "temperature": 0.7,
        "top_p": None,
        "max_tokens": 256,
        "transport": httpx.MockTransport(handler),
    }
    kwargs.update(overrides)
    return CerebrasProvider(**kwargs)


def _ok_body(
    text: str = OK_TEXT,
    *,
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
    total_tokens: int | None = 19,
) -> dict:
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gpt-oss-120b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Cerebras: request/response contract
# ---------------------------------------------------------------------------


def test_successful_completion_records_usage_latency_and_raw():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    provider = _cerebras(handler)
    response = provider.complete(_request(temperature=0.1, top_p=0.5, max_tokens=99, seed=42))

    assert seen["url"] == CHAT_URL
    assert seen["auth"] == "Bearer test-key"
    assert seen["content_type"] == "application/json"
    body = seen["body"]
    assert body["model"] == "gpt-oss-120b"
    assert body["messages"] == [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "user instructions"},
    ]
    assert body["temperature"] == 0.1
    assert body["top_p"] == 0.5
    assert body["max_tokens"] == 99
    assert body["seed"] == 42
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}

    assert response.text == OK_TEXT
    assert response.provider == "cerebras"
    assert response.model == "gpt-oss-120b"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 7
    assert response.usage.total_tokens == 19
    assert response.finish_reason == "stop"
    assert response.latency_ms >= 0.0
    assert response.raw["id"] == "chatcmpl-1"


def test_text_mode_omits_json_response_format_and_seed():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    provider = _cerebras(handler)
    provider.complete(_request(response_format="text"))

    assert "response_format" not in seen["body"]
    assert "seed" not in seen["body"]
    assert "top_p" not in seen["body"]


def test_request_values_override_provider_defaults_and_vice_versa():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    provider = _cerebras(handler, temperature=0.3, top_p=0.9, max_tokens=500)
    provider.complete(_request(model="other-model"))

    body = seen["body"]
    assert body["model"] == "other-model"
    assert body["temperature"] == 0.3
    assert body["top_p"] == 0.9
    assert body["max_tokens"] == 500


# ---------------------------------------------------------------------------
# Cerebras: retry, backoff and failure modes
# ---------------------------------------------------------------------------


def test_429_retry_after_then_success():
    calls = {"count": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": {"message": "rate limited"}},
            )
        return httpx.Response(200, json=_ok_body())

    provider = _cerebras(handler, max_retries=2, sleep=sleeps.append)
    response = provider.complete(_request())

    assert calls["count"] == 2
    assert sleeps == [0.0]
    assert response.text == OK_TEXT


def test_exhausted_5xx_raises_provider_error_after_all_retries():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider = _cerebras(handler, max_retries=2)
    with pytest.raises(ProviderError) as excinfo:
        provider.complete(_request())

    assert calls["count"] == 3
    assert excinfo.value.details["attempts"] == 3
    assert excinfo.value.details["status_code"] == 503
    assert excinfo.value.details["retryable"] is True
    assert "503" in excinfo.value.message


def test_timeout_is_retried_and_then_succeeds():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ReadTimeout("slow provider")
        return httpx.Response(200, json=_ok_body())

    provider = _cerebras(handler, max_retries=1, sleep=lambda _: None)
    response = provider.complete(_request())

    assert calls["count"] == 2
    assert response.text == OK_TEXT


def test_exhausted_timeout_raises_provider_error():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ReadTimeout("slow provider")

    provider = _cerebras(handler, max_retries=1, sleep=lambda _: None)
    with pytest.raises(ProviderError) as excinfo:
        provider.complete(_request())

    assert calls["count"] == 2
    assert excinfo.value.details["attempts"] == 2
    assert "timed out" in excinfo.value.message


def test_client_error_is_not_retried():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = _cerebras(handler, max_retries=3)
    with pytest.raises(ProviderError) as excinfo:
        provider.complete(_request())

    assert calls["count"] == 1
    assert excinfo.value.details["status_code"] == 401
    assert excinfo.value.details["retryable"] is False


def test_non_json_success_body_raises_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    provider = _cerebras(handler)
    with pytest.raises(ProviderError):
        provider.complete(_request())


def test_success_body_without_choices_raises_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "choices": []})

    provider = _cerebras(handler)
    with pytest.raises(ProviderError):
        provider.complete(_request())


def test_usage_total_is_derived_when_missing_but_parts_are_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body(total_tokens=None))

    provider = _cerebras(handler)
    response = provider.complete(_request())
    assert response.usage.total_tokens == 19


# ---------------------------------------------------------------------------
# Credential handling and provider construction
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_config_error_at_creation():
    with pytest.raises(ConfigError, match="CEREBRAS_API_KEY"):
        create_provider(Settings(provider="cerebras", api_key=None))


def test_empty_api_key_raises_config_error_in_constructor():
    with pytest.raises(ConfigError, match="CEREBRAS_API_KEY"):
        CerebrasProvider(
            api_key="",
            base_url=BASE_URL,
            model="gpt-oss-120b",
            timeout_s=5.0,
            max_retries=0,
            backoff_s=0.0,
            temperature=0.7,
            top_p=None,
            max_tokens=256,
        )


def test_unknown_provider_raises_config_error():
    with pytest.raises(ConfigError, match="unknown provider"):
        create_provider(Settings(provider="openai"))


def test_api_key_never_leaks_into_response_or_repr():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    provider = _cerebras(handler, api_key="super-secret-key")
    response = provider.complete(_request())

    serialized = json.dumps(
        {
            "response": response.model_dump(mode="json"),
            "repr": repr(provider),
        }
    )
    assert "super-secret-key" not in serialized


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------


def _mock_request(scenario: Scenario, document_id: str) -> LLMRequest:
    document = scenario.document(document_id)
    return LLMRequest(
        provider="mock",
        model="mock-1",
        prompt_version="v1",
        system="system",
        user="user",
        response_format="json",
        metadata={
            "scenario": scenario.model_dump_json(),
            "document": document.model_dump_json(),
            "attempt": 1,
            "feedback": [],
        },
    )


def test_mock_provider_is_explicitly_selectable():
    provider = create_provider(Settings(provider="mock"))
    assert isinstance(provider, MockProvider)
    assert provider.name == "mock"
    assert provider.model == "mock-1"
    assert isinstance(provider, Provider)


def test_mock_provider_keeps_custom_model_but_defaults_to_mock_model():
    assert create_provider(Settings(provider="mock", model="mock-1")).model == "mock-1"
    assert create_provider(Settings(provider="mock", model="custom-mock")).model == ("custom-mock")


def test_mock_provider_has_at_least_three_wording_variants_per_clause_type():
    base = make_consulting_scenario()
    texts = set()
    for index in range(12):
        scenario = base.model_copy(update={"contract_id": f"contract_{index:04d}"})
        payload = json.loads(
            MockProvider().complete(_mock_request(scenario, "main_agreement")).text
        )
        clause_text = {clause["clause_id"]: clause["text"] for clause in payload["clauses"]}
        texts.add(clause_text["clause_02"])
    assert len(texts) >= 3


def test_mock_provider_output_is_deterministic_and_structured():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    provider = MockProvider()
    request = _mock_request(scenario, document.document_id)

    first = provider.complete(request)
    second = provider.complete(request)
    assert first.text == second.text

    payload = json.loads(first.text)
    assert payload["document_id"] == document.document_id
    returned = payload["clauses"]
    assert [clause["clause_id"] for clause in returned] == [
        clause.clause_id for clause in document.clauses
    ]
    for clause in returned:
        assert clause["heading"].strip()
        assert clause["text"].strip()
        assert "clause_" not in clause["text"]
        assert "fact_" not in clause["text"]
        assert scenario.contract_id not in clause["text"]

    assert first.provider == "mock"
    assert first.model == "mock-1"
    assert first.usage.input_tokens is None
    assert first.usage.total_tokens is None
    assert first.raw["deterministic"] is True


def test_mock_provider_uses_explicit_surfaces_and_parties():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    payload = json.loads(
        MockProvider().complete(_mock_request(scenario, document.document_id)).text
    )
    texts = {clause["clause_id"]: clause["text"] for clause in payload["clauses"]}

    assert "Nordvik Teknik AB" in texts["clause_01"]
    assert "Sundberg Konsult AB" in texts["clause_01"]
    assert "(client)" in texts["clause_01"]
    assert "(supplier)" in texts["clause_01"]

    hourly_rate = scenario.fact("fact_hourly_rate")
    assert any(surface in texts["clause_02"] for surface in hourly_rate.explicit_surfaces)

    notice_period = scenario.fact("fact_notice_period")
    assert any(surface in texts["clause_03"] for surface in notice_period.explicit_surfaces)

    liability_cap = scenario.fact("fact_liability_cap")
    assert any(surface in texts["clause_05"] for surface in liability_cap.explicit_surfaces)


def test_mock_provider_supports_english():
    scenario = make_consulting_scenario().model_copy(update={"language": Language.EN})
    document = scenario.documents[0]
    payload = json.loads(
        MockProvider().complete(_mock_request(scenario, document.document_id)).text
    )
    first_text = payload["clauses"][0]["text"]
    second_text = payload["clauses"][1]["text"]
    assert "This agreement has been entered into between" in first_text
    assert "Detta avtal" not in first_text
    assert "950 SEK" in second_text


def test_mock_provider_requires_scenario_and_document_metadata():
    provider = MockProvider()
    with pytest.raises(GenerationError, match="scenario"):
        provider.complete(_request(provider="mock", model="mock-1", metadata={}))
    with pytest.raises(GenerationError, match="document"):
        provider.complete(
            _request(
                provider="mock",
                model="mock-1",
                metadata={"scenario": make_consulting_scenario().model_dump_json()},
            )
        )


def test_mock_provider_parses_metadata_dicts_not_only_json_strings():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    request = _request(
        provider="mock",
        model="mock-1",
        metadata={
            "scenario": scenario.model_dump(mode="json"),
            "document": document.model_dump(mode="json"),
        },
    )
    payload = json.loads(MockProvider().complete(request).text)
    assert payload["document_id"] == document.document_id


def test_mock_provider_never_consumes_api_key():
    scenario = make_consulting_scenario()
    document = scenario.documents[0]
    request = _mock_request(scenario, document.document_id)
    request = request.model_copy(update={"metadata": {**request.metadata, "api_key": "x"}})
    response = MockProvider().complete(request)
    assert "api_key" not in response.raw


# ---------------------------------------------------------------------------
# Live smoke test (skipped unless CEREBRAS_API_KEY is exported)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("CEREBRAS_API_KEY"),
    reason="CEREBRAS_API_KEY not set; live provider smoke test skipped",
)
def test_live_cerebras_json_completion():
    settings = Settings.from_env()
    provider = create_provider(settings)
    request = LLMRequest(
        provider="cerebras",
        model=settings.model,
        prompt_version="v1",
        system="You answer with a single JSON object and nothing else.",
        user='Return exactly {"ok": true}',
        temperature=0.0,
        max_tokens=64,
        response_format="json",
    )
    response = provider.complete(request)
    payload = json.loads(response.text)
    assert isinstance(payload, dict)
    assert response.provider == "cerebras"
    assert response.model
    assert response.latency_ms >= 0.0


def test_mock_provider_is_deterministic_across_processes():
    project_root = Path(__file__).resolve().parents[1]
    script = (
        "import sys; sys.path.insert(0, 'tests')\n"
        "from factories import make_consulting_scenario\n"
        "from contractforge.config import Settings\n"
        "from contractforge.generation.prompts import build_contract_request\n"
        "from contractforge.providers import MockProvider\n"
        "scenario = make_consulting_scenario()\n"
        "request = build_contract_request(\n"
        "    scenario, scenario.documents[0],\n"
        "    Settings(provider='mock', model='mock-1', seed=42),\n"
        ")\n"
        "print(MockProvider().complete(request).text)\n"
    )
    outputs = []
    for hash_seed in ("0", "12345"):
        environment = {**os.environ, "PYTHONHASHSEED": hash_seed}
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert outputs[0].strip()


def test_swedish_number_words_are_correct_compounds():
    from contractforge.providers.mock import _sv_number_words

    assert _sv_number_words(3) == "tre"
    assert _sv_number_words(950) == "niohundrafemtio"
    assert _sv_number_words(40250) == "fyrtiotusentvåhundrafemtio"
    assert _sv_number_words(500000) == "femhundratusen"
