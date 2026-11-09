"""Protocol adapters for non-streaming model and tool-call requests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .model_connection_models import ModelProtocol, validate_base_url


MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


class ModelGatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModelTarget:
    connection_id: str
    connection_version: str
    name: str
    protocol: ModelProtocol
    base_url: str
    model: str
    api_key: str | None = field(repr=False)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None
    arguments_error: str | None = None


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: ModelUsage | None

    @property
    def output_limited(self) -> bool:
        return self.finish_reason in {"length", "max_tokens", "incomplete"}


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output: str
    is_error: bool = False


class JsonTransport(Protocol):
    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, Any]: ...


class HttpxJsonTransport:
    """Small HTTP transport with TLS verification and redirects disabled."""

    def __init__(self, client_factory: Any = httpx.AsyncClient) -> None:
        self._client_factory = client_factory

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        try:
            body = asyncio.run(self._read_response(url, headers, payload, timeout_seconds, max_response_bytes))
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ModelGatewayError("timeout", "模型服务请求超时") from exc
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            raise ModelGatewayError("unreachable", "无法连接模型服务") from exc
        try:
            decoded = json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise ModelGatewayError("invalid_response", "模型服务返回的不是有效 JSON") from exc
        if not isinstance(decoded, dict):
            raise ModelGatewayError("invalid_response", "模型服务响应结构异常")
        return decoded

    async def _read_response(
        self, url: str, headers: dict[str, str], payload: dict[str, Any],
        timeout_seconds: float, max_response_bytes: int,
    ) -> bytes:
        async with asyncio.timeout(timeout_seconds), self._client_factory(
            follow_redirects=False, verify=True, trust_env=False, timeout=timeout_seconds,
        ) as client, client.stream(
            "POST", url, headers={**headers, "Accept-Encoding": "identity"}, json=payload,
        ) as response:
            _check_http_status(response)
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ModelGatewayError("invalid_response", "模型服务未按请求返回未压缩的响应")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_response_bytes:
                    raise ModelGatewayError("response_too_large", "模型服务响应超过大小限制")
                body.extend(chunk)
            return bytes(body)


def _check_http_status(response: httpx.Response) -> None:
    code = response.status_code
    if 300 <= code < 400:
        raise ModelGatewayError("redirect_rejected", "模型服务返回了重定向，已拒绝跟随")
    if code in {401, 403}:
        raise ModelGatewayError("authentication_failed", "模型服务拒绝了认证")
    if code in {400, 404, 405, 422}:
        raise ModelGatewayError("endpoint_or_model_not_found", "请检查接口地址、协议、模型及服务商兼容性")
    if code == 429:
        raise ModelGatewayError("rate_limited", "模型服务触发限流")
    if not response.is_success:
        raise ModelGatewayError("provider_error", f"模型服务返回 HTTP {code}")


def _endpoint(base_url: str, suffix: str) -> str:
    try:
        base_url = validate_base_url(base_url)
    except ValueError as exc:
        raise ModelGatewayError("invalid_base_url", "基础地址无效，请检查已保存的模型连接") from exc
    return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"


def _tool_call(call_id: Any, name: Any, arguments: Any) -> ToolCall:
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise ModelGatewayError("invalid_response", "模型返回了缺少标识或名称的工具调用")
    if isinstance(arguments, dict):
        return ToolCall(call_id, name, arguments)
    if not isinstance(arguments, str):
        return ToolCall(call_id, name, None, "工具参数不是 JSON 对象")
    try:
        decoded = json.loads(arguments)
    except (ValueError, RecursionError):
        return ToolCall(call_id, name, None, "工具参数不是有效 JSON")
    if not isinstance(decoded, dict):
        return ToolCall(call_id, name, None, "工具参数必须是 JSON 对象")
    return ToolCall(call_id, name, decoded)


class ModelConversation:
    def __init__(
        self,
        target: ModelTarget,
        system: str,
        user: str,
        tools: list[ToolDefinition],
        transport: JsonTransport,
        *,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_HTTP_RESPONSE_BYTES,
        max_output_tokens: int = 2048,
    ) -> None:
        self.target = target
        self.system = system
        self.tools = tools
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_output_tokens = max_output_tokens
        self.messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        self._last_assistant: Any = None

    def request(self) -> ModelTurn:
        adapters = {
            ModelProtocol.OPENAI_CHAT_COMPLETIONS: self._chat_request,
            ModelProtocol.OPENAI_RESPONSES: self._responses_request,
            ModelProtocol.ANTHROPIC_MESSAGES: self._anthropic_request,
        }
        adapter = adapters.get(self.target.protocol)
        if adapter is None:
            raise ModelGatewayError("unsupported_protocol", "不支持的模型接口协议")
        try:
            turn = adapter()
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            raise ModelGatewayError("invalid_response", "模型响应与所选协议格式不匹配") from exc
        if len({call.id for call in turn.tool_calls}) != len(turn.tool_calls):
            raise ModelGatewayError("invalid_response", "模型返回了重复的工具调用标识")
        if self.target.api_key and self.target.api_key in turn.text:
            raise ModelGatewayError("invalid_response", "模型响应包含认证信息，已拒绝显示")
        return turn

    def submit_tool_results(self, results: list[ToolResult]) -> None:
        if self._last_assistant is None:
            raise RuntimeError("no model tool call is awaiting results")
        if self.target.protocol == ModelProtocol.OPENAI_CHAT_COMPLETIONS:
            self.messages.append(self._last_assistant)
            self.messages.extend(
                {"role": "tool", "tool_call_id": result.call_id, "content": result.output}
                for result in results
            )
        elif self.target.protocol == ModelProtocol.OPENAI_RESPONSES:
            self.messages.extend(self._last_assistant)
            self.messages.extend(
                {"type": "function_call_output", "call_id": result.call_id, "output": result.output}
                for result in results
            )
        else:
            self.messages.append({"role": "assistant", "content": self._last_assistant})
            self.messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result.call_id,
                            "content": result.output,
                            "is_error": result.is_error,
                        }
                        for result in results
                    ],
                }
            )
        self._last_assistant = None

    def _post(self, suffix: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        return self.transport.post_json(
            _endpoint(self.target.base_url, suffix),
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
        )

    def _chat_request(self) -> ModelTurn:
        headers = {"Content-Type": "application/json"}
        if self.target.api_key:
            headers["Authorization"] = f"Bearer {self.target.api_key}"
        payload: dict[str, Any] = {
            "model": self.target.model,
            "messages": [{"role": "system", "content": self.system}, *self.messages],
            "max_completion_tokens": self.max_output_tokens,
        }
        if self.tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in self.tools
            ]
        body = self._post("chat/completions", headers, payload)
        try:
            choice = body["choices"][0]
            message = choice["message"]
            text = message.get("content")
            if text is not None and not isinstance(text, str):
                raise TypeError("expected text")
            calls = [
                _tool_call(item.get("id"), item.get("function", {}).get("name"), item.get("function", {}).get("arguments"))
                for item in message.get("tool_calls", [])
            ]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelGatewayError("invalid_response", "Chat Completions 响应结构异常") from exc
        self._last_assistant = {**message, "role": "assistant"}
        return ModelTurn(text or "", calls, choice.get("finish_reason"), _usage(body.get("usage")))

    def _responses_request(self) -> ModelTurn:
        headers = {"Content-Type": "application/json"}
        if self.target.api_key:
            headers["Authorization"] = f"Bearer {self.target.api_key}"
        payload: dict[str, Any] = {
            "model": self.target.model,
            "instructions": self.system,
            "input": self.messages,
            "max_output_tokens": self.max_output_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if self.tools:
            payload["tools"] = [
                {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters, "strict": False}
                for t in self.tools
            ]
        body = self._post("responses", headers, payload)
        output = body.get("output")
        if not isinstance(output, list):
            raise ModelGatewayError("invalid_response", "Responses API 响应结构异常")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                calls.append(_tool_call(item.get("call_id"), item.get("name"), item.get("arguments")))
            elif item.get("type") == "message":
                for part in item.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text_parts.append(part["text"])
        if body.get("status") in {"failed", "cancelled", "queued", "in_progress"}:
            raise ModelGatewayError("invalid_response", "Responses API 未完成请求")
        self._last_assistant = output
        return ModelTurn("".join(text_parts), calls, body.get("status"), _usage(body.get("usage")))

    def _anthropic_request(self) -> ModelTurn:
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if self.target.api_key:
            headers["x-api-key"] = self.target.api_key
        payload: dict[str, Any] = {
            "model": self.target.model,
            "system": self.system,
            "messages": self.messages,
            "max_tokens": self.max_output_tokens,
        }
        if self.tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in self.tools
            ]
        body = self._post("messages", headers, payload)
        content = body.get("content")
        if not isinstance(content, list):
            raise ModelGatewayError("invalid_response", "Anthropic Messages 响应结构异常")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                calls.append(_tool_call(block.get("id"), block.get("name"), block.get("input")))
        self._last_assistant = content
        return ModelTurn("".join(text_parts), calls, body.get("stop_reason"), _usage(body.get("usage")))


def _usage(value: Any) -> ModelUsage | None:
    if not isinstance(value, dict):
        return None
    input_tokens = value.get("input_tokens") or value.get("prompt_tokens")
    output_tokens = value.get("output_tokens") or value.get("completion_tokens")
    total_tokens = value.get("total_tokens")
    return ModelUsage(
        input_tokens=input_tokens if isinstance(input_tokens, int) else None,
        output_tokens=output_tokens if isinstance(output_tokens, int) else None,
        total_tokens=total_tokens if isinstance(total_tokens, int) else None,
    )


class ModelGateway:
    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or HttpxJsonTransport()

    def conversation(
        self,
        target: ModelTarget,
        *,
        system: str,
        user: str,
        tools: list[ToolDefinition] | None = None,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_output_tokens: int = 2048,
    ) -> ModelConversation:
        return ModelConversation(
            target, system, user, tools or [], self.transport,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
