import hashlib
import json
import os
import uuid

from .errors import ProxyError
from .prompts import INSTRUCTION_ROLES, PROMPT_MODE
from .schema import Message


def fingerprints(messages):
    result = []
    for message in messages:
        value = Message.model_validate(message).model_dump(exclude_none=True)
        for call in value.get("tool_calls", []):
            call["function"]["arguments"] = json.loads(call["function"]["arguments"])
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        result.append(hashlib.sha256(encoded).hexdigest())
    return result


class Conversations:
    """Single-worker store: upstream cursors and message hashes, never prompt text."""

    def __init__(self, path, limit):
        self.path = path
        self.limit = limit
        self.entries = json.loads(path.read_text()) if path.exists() else {}

    def resolve(self, request):
        hashes = fingerprints([m for m in request.messages if m.role not in INSTRUCTION_ROLES])
        entry = self.entries.get(request.conversation_id) if request.conversation_id else None
        if entry is None and not request.conversation_id:
            matches = [
                (key, value)
                for key, value in self.entries.items()
                if value.get("prompt_mode") == PROMPT_MODE
                and value["model"] == request.model
                and len(hashes) > len(value["hashes"])
                and hashes[: len(value["hashes"])] == value["hashes"]
            ]
            if len(matches) > 1:
                raise ProxyError(
                    "conversation_ambiguous", "Specify conversation_id to select this thread.", 409
                )
            if matches:
                request.conversation_id, entry = matches[0]
        if entry is not None:
            if entry.get("prompt_mode") != PROMPT_MODE:
                raise ProxyError(
                    "conversation_mode_changed",
                    "This thread used structured prompts. Use a new conversation_id for passthrough.",
                    409,
                )
            if not entry["valid"]:
                raise ProxyError(
                    "conversation_uncertain",
                    "The previous turn did not complete reliably; start a new conversation_id.",
                    409,
                )
            if (
                entry["model"] != request.model
                or len(hashes) <= len(entry["hashes"])
                or hashes[: len(entry["hashes"])] != entry["hashes"]
            ):
                raise ProxyError(
                    "conversation_conflict",
                    "Continue with the same model and full unchanged history, or use a new ID.",
                    409,
                )
        else:
            request.conversation_id = request.conversation_id or "conv_" + uuid.uuid4().hex
        return entry, hashes

    def save(self, key, entry):
        entries = dict(self.entries)
        entries.pop(key, None)
        entries[key] = entry
        while len(entries) > self.limit:
            del entries[next(iter(entries))]
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(entries, output)
        temporary.replace(self.path)
        self.entries = entries
