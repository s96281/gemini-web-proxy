import json
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FunctionCall(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    arguments: str


class ToolCall(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["function"] = "function"
    function: FunctionCall


class Message(StrictModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def text_only(self):
        if isinstance(self.content, list):
            if any(
                set(p) != {"type", "text"}
                or p.get("type") != "text"
                or not isinstance(p.get("text"), str)
                for p in self.content
            ):
                raise ValueError("Only text content parts are supported")
            self.content = "".join(p["text"] for p in self.content)
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls require assistant role")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.role != "tool" and self.tool_call_id:
            raise ValueError("tool_call_id requires tool role")
        if self.content is None and not self.tool_calls:
            raise ValueError("Message requires text or tool_calls")
        return self


class FunctionDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    strict: Literal[False] | None = None

    @model_validator(mode="after")
    def check_schema(self):
        try:
            Draft202012Validator.check_schema(self.parameters)
        except Exception:
            raise ValueError("Invalid function parameter schema") from None
        if self.parameters.get("type") != "object":
            raise ValueError("Function parameters must have type object")

        def check(value):
            if isinstance(value, dict):
                if "$ref" in value and (
                    not isinstance(value["$ref"], str) or not value["$ref"].startswith("#")
                ):
                    raise ValueError("External schema references are unsupported")
                if "$dynamicRef" in value or "$id" in value:
                    raise ValueError("Schema identifiers/dynamic references are unsupported")
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(self.parameters)
        return self


class Tool(StrictModel):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class ChatRequest(StrictModel):
    model: str = Field(min_length=1)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=256)
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    tools: list[Tool] = Field(default_factory=list, max_length=128)
    tool_choice: Literal["auto", "none", "required"] | dict[str, Any] = "auto"
    parallel_tool_calls: Literal[False] | None = None
    n: Literal[1] = 1
    stream_options: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_conversation(self):
        names = [t.function.name for t in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate tool names")
        if self.stream_options is not None and (
            set(self.stream_options) - {"include_usage"}
            or (
                "include_usage" in self.stream_options
                and not isinstance(self.stream_options["include_usage"], bool)
            )
        ):
            raise ValueError("stream_options only supports a boolean include_usage")
        if isinstance(self.tool_choice, dict):
            choice = self.tool_choice
            if set(choice) != {"type", "function"} or choice["type"] != "function":
                raise ValueError("Invalid tool_choice")
            if not isinstance(choice["function"], dict) or set(choice["function"]) != {"name"}:
                raise ValueError("tool_choice requires a function name")
            if choice["function"]["name"] not in names:
                raise ValueError("Selected tool is not defined")
        elif self.tool_choice == "required" and not self.tools:
            raise ValueError("required tool_choice needs tools")
        pending = set()
        seen = set()
        for m in self.messages:
            if m.role == "tool":
                if m.tool_call_id not in pending:
                    raise ValueError("Tool result must match a pending tool call")
                pending.remove(m.tool_call_id)
            else:
                if pending:
                    raise ValueError("Tool results must follow assistant tool calls")
                for call in m.tool_calls or []:
                    if call.id in seen:
                        raise ValueError("Duplicate tool call id")
                    seen.add(call.id)
                    pending.add(call.id)
                    try:
                        args = json.loads(call.function.arguments)
                        if not isinstance(args, dict):
                            raise ValueError()
                    except (ValueError, TypeError):
                        raise ValueError("Historical tool arguments must be JSON objects") from None
        if pending:
            raise ValueError("Missing tool results")
        return self
