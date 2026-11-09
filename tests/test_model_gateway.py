from __future__ import annotations

import asyncio
import json

import pytest
import httpx

from local_workspace_service.model_connection_models import ModelProtocol
from local_workspace_service.model_gateway import (
    ModelGateway,
    ModelTarget,
    ToolDefinition,
    ToolResult,
    HttpxJsonTransport,
    ModelGatewayError,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post_json(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def target(protocol):
    return ModelTarget("id", "v1", "test", protocol, "https://models.example.test/custom/v1/", "demo", "fake-secret")


@pytest.mark.parametrize(
    "protocol,response,suffix,text",
    [
        (ModelProtocol.OPENAI_CHAT_COMPLETIONS, {"choices": [{"message": {"content": "chat ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}, "/chat/completions", "chat ok"),
        (ModelProtocol.OPENAI_RESPONSES, {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "responses ok"}]}], "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}, "/responses", "responses ok"),
        (ModelProtocol.ANTHROPIC_MESSAGES, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "anthropic ok"}], "usage": {"input_tokens": 2, "output_tokens": 3}}, "/messages", "anthropic ok"),
    ],
)
def test_three_protocols_parse_text_and_preserve_base_path(protocol, response, suffix, text):
    transport = FakeTransport([response])
    turn = ModelGateway(transport).conversation(target(protocol), system="system", user="hello").request()
    assert turn.text == text
    assert transport.requests[0][0] == "https://models.example.test/custom/v1" + suffix
    assert turn.usage.input_tokens == 2
    assert "fake-secret" not in repr(turn)


@pytest.mark.parametrize(
    "protocol,first,second",
    [
        (
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
            {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}, {"id": "c2", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]},
        ),
        (
            ModelProtocol.OPENAI_RESPONSES,
            {"status": "completed", "output": [{"type": "reasoning", "encrypted_content": "opaque", "summary": []}, {"type": "function_call", "id": "item1", "call_id": "c1", "name": "probe", "arguments": "{}"}, {"type": "function_call", "id": "item2", "call_id": "c2", "name": "probe", "arguments": "{}"}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]},
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "c1", "name": "probe", "input": {}}, {"type": "tool_use", "id": "c2", "name": "probe", "input": {}}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
        ),
    ],
)
def test_three_protocols_round_trip_multiple_tool_calls(protocol, first, second):
    transport = FakeTransport([first, second])
    tool = ToolDefinition("probe", "test", {"type": "object", "properties": {}})
    conversation = ModelGateway(transport).conversation(target(protocol), system="s", user="u", tools=[tool])
    turn = conversation.request()
    assert [call.id for call in turn.tool_calls] == ["c1", "c2"]
    conversation.submit_tool_results([ToolResult("c1", '{"n":1}'), ToolResult("c2", '{"n":2}')])
    assert conversation.request().text == "done"
    second_payload = transport.requests[1][1]["payload"]
    headers = transport.requests[1][1]["headers"]
    if protocol == ModelProtocol.OPENAI_CHAT_COMPLETIONS:
        assert headers["Authorization"] == "Bearer fake-secret"
        assert [message["tool_call_id"] for message in second_payload["messages"][-2:]] == ["c1", "c2"]
    elif protocol == ModelProtocol.OPENAI_RESPONSES:
        assert headers["Authorization"] == "Bearer fake-secret"
        assert second_payload["store"] is False
        assert second_payload["tools"][0]["strict"] is False
        assert second_payload["input"][1]["encrypted_content"] == "opaque"
        assert [item["call_id"] for item in second_payload["input"][-2:]] == ["c1", "c2"]
    else:
        assert headers["x-api-key"] == "fake-secret"
        assert headers["anthropic-version"] == "2023-06-01"
        assert [block["tool_use_id"] for block in second_payload["messages"][-1]["content"]] == ["c1", "c2"]


def test_invalid_tool_json_is_exposed_as_structured_parse_failure():
    transport = FakeTransport([{"choices": [{"message": {"tool_calls": [{"id": "c1", "function": {"name": "probe", "arguments": "not-json"}}]}, "finish_reason": "tool_calls"}]}])
    turn = ModelGateway(transport).conversation(target(ModelProtocol.OPENAI_CHAT_COMPLETIONS), system="s", user="u").request()
    assert turn.tool_calls[0].arguments is None
    assert "JSON" in turn.tool_calls[0].arguments_error


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {}, "authentication_failed"),
        (429, {}, "rate_limited"),
        (200, b"not-json", "invalid_response"),
        (302, {}, "redirect_rejected"),
    ],
)
def test_http_transport_maps_errors_without_echoing_credentials(status, body, expected):
    def handler(request):
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body, headers={"location": "https://elsewhere.test"} if status == 302 else {})
    mock = httpx.MockTransport(handler)
    transport = HttpxJsonTransport(lambda **kwargs: httpx.AsyncClient(transport=mock, **kwargs))
    with pytest.raises(ModelGatewayError) as error:
        transport.post_json("https://models.example.test/v1/responses", headers={"Authorization": "Bearer fake-secret"}, payload={}, timeout_seconds=1, max_response_bytes=1024)
    assert error.value.code == expected
    assert "fake-secret" not in str(error.value)


def test_http_transport_maps_timeout():
    def handler(request):
        raise httpx.ReadTimeout("offline timeout", request=request)
    mock = httpx.MockTransport(handler)
    transport = HttpxJsonTransport(lambda **kwargs: httpx.AsyncClient(transport=mock, **kwargs))
    with pytest.raises(ModelGatewayError) as error:
        transport.post_json("https://models.example.test/v1/responses", headers={}, payload={}, timeout_seconds=1, max_response_bytes=1024)
    assert error.value.code == "timeout"


@pytest.mark.parametrize("mode, expected", [("slow", "timeout"), ("large", "response_too_large")])
def test_http_transport_bounds_total_time_and_response_body(mode, expected):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                if mode == "slow":
                    await asyncio.sleep(0.005)
                yield b"0123456789"

    mock = httpx.MockTransport(lambda _: httpx.Response(200, stream=Body()))
    transport = HttpxJsonTransport(lambda **kwargs: httpx.AsyncClient(transport=mock, **kwargs))
    with pytest.raises(ModelGatewayError) as error:
        transport.post_json("https://models.example.test/responses", headers={}, payload={}, timeout_seconds=0.03, max_response_bytes=100)
    assert error.value.code == expected


@pytest.mark.parametrize("body", [
    {"choices": [{"message": {"content": {"text": "malformed"}}}]},
    {"choices": [{"message": {"content": "echo fake-secret"}}]},
])
def test_malformed_or_credential_echo_responses_fail_without_exposing_secrets(body):
    connection = target(ModelProtocol.OPENAI_CHAT_COMPLETIONS)
    conversation = ModelGateway(FakeTransport([body])).conversation(connection, system="s", user="u")
    with pytest.raises(ModelGatewayError) as failure:
        conversation.request()
    assert failure.value.code == "invalid_response"
    assert "fake-secret" not in str(failure.value)
    assert "fake-secret" not in repr(connection)
