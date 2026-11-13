from .errors import ProxyError
from .schema import ChatRequest, Message

INSTRUCTION_ROLES = {"system", "developer"}
PROMPT_MODE = "passthrough"


def validate_passthrough(request: ChatRequest) -> None:
    if not any(m.role == "user" for m in request.messages):
        raise ProxyError("invalid_request", "Include a user message in the conversation.", 400)
    if request.tools and request.tool_choice != "none":
        raise ProxyError(
            "unsupported_tools",
            "Passthrough mode does not support function tools. Disable tools/memory tools "
            "in the client, or set tool_choice to none.",
            400,
        )
    if any(m.role == "tool" or m.tool_calls for m in request.messages):
        raise ProxyError(
            "unsupported_tool_history",
            "Passthrough mode cannot replay tool calls or tool results. Start a text/image thread.",
            400,
        )
    if request.messages[-1].role == "assistant":
        raise ProxyError(
            "unsupported_assistant_prefill", "End the request with a new user message.", 400
        )


def message_text(message: Message) -> str:
    if isinstance(message.content, list):
        return "".join(part["text"] for part in message.content if part["type"] == "text")
    return message.content or ""


def build_prompt(
    request: ChatRequest,
    start: int = 0,
    include_instructions: bool = True,
    updating_instructions: bool = False,
) -> str:
    """Send ordinary text; role labels are only needed when importing prior conversation."""
    messages = [m for m in request.messages[start:] if m.role not in INSTRUCTION_ROLES]
    if any(m.role == "assistant" for m in messages):
        # A fresh Gemini chat has no previous native turns. Import history as readable text.
        previous = messages[:-1]
        labels = {"user": "使用者", "assistant": "助手"}
        history = "\n\n".join(f"{labels[m.role]}：\n{message_text(m)}" for m in previous)
        body = "先前的對話：\n\n" + history + "\n\n現在的訊息：\n" + message_text(messages[-1])
    else:
        body = "\n\n".join(message_text(m) for m in messages)
    if include_instructions:
        instructions = [message_text(m) for m in request.messages if m.role in INSTRUCTION_ROLES]
        if any(instructions):
            prefix = (
                "對話指示已更新，請以以下指示為準：\n"
                if updating_instructions
                else "請在這段對話中遵循以下指示：\n"
            ) + "\n\n".join(instructions)
            body = prefix + ("\n\n使用者的訊息：\n" + body if body else "")
        elif updating_instructions:
            body = "先前提供的對話指示已取消。\n\n" + body
    # The pinned SDK asserts a nonempty prompt even for image-only messages.
    return body or " "
