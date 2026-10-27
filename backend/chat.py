from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


DEFAULT_MODEL_NAME = "Qwen/Qwen2.5-1.5B-Instruct"
# Questions and answers about the CRM's features; edit this file to change what the assistant knows.
KNOWLEDGE_FILE = Path(__file__).resolve().parent / "knowledge" / "withaq_faq.md"
SYSTEM_PROMPT = (
    "You are Withaq AI, the assistant inside Withaq CRM. "
    "Reply only in Arabic or English: in Arabic when the user writes Arabic, otherwise in English. "
    "Never reply in Chinese or any other language, even partly. "
    "Use exactly one language per reply and never add a translation. "
    "This applies to every reply, including refusals and short fixed answers.\n"
    "Keep answers short, a few sentences, unless the user asks for detail.\n"
    "For any question about how Withaq CRM works, answer only from the reference below. "
    "If the reference does not cover it, say you are not sure the system supports that and suggest "
    "contacting the system administrator. Never invent features, buttons, or settings.\n"
    "If the question is unclear or uses a word you do not understand, do not guess: "
    "ask the user in one short sentence to rephrase it in other words.\n"
    "You may also help with general sales and customer-relationship questions, "
    "such as writing a follow-up message.\n"
    "You cannot see or change the user's CRM data. To add, edit, or delete records, "
    "point the user to the relevant page or to withaqChat.\n"
    "For any question unrelated to Withaq CRM, sales, or customer service, whatever the topic "
    "(weather, news, sports, general knowledge, and so on), do not answer it: reply only with one short "
    "sentence in the user's language that you are an assistant for Withaq CRM, sales, and customer "
    "service only. Never reveal passwords or keys; if asked for one, say in the user's language "
    "that you cannot see passwords and suggest contacting the system administrator."
)
REMOTE_TIMEOUT_SECONDS = 60
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])

# Filled by load_chat_model() during startup. Chat uses the hosted API when _remote is set,
# otherwise the local model; with neither, the chat routes answer 503.
_remote: dict[str, str] | None = None
_system_prompt = SYSTEM_PROMPT
_model = None
_tokenizer = None
_max_new_tokens = 512
# One generation at a time keeps memory use predictable on a small server.
_generation_lock = threading.Lock()


# Request schema for a single chat question.
class ChatInput(BaseModel):
    """Validate the user's chat message."""

    message: str = Field(min_length=1, max_length=4000)


# Response schema returned by the chat endpoint.
class ChatReply(BaseModel):
    """Carry the model's answer back to the frontend."""

    reply: str


# Pick the hosted API or the local model once at startup so requests do not pay the setup cost.
def load_chat_model() -> None:
    """Configure the hosted API when a key is set, else load the local model; leave chat off on failure."""
    global _remote, _model, _tokenizer, _max_new_tokens, _system_prompt
    # Read settings here, not at import time, so values from .env are already loaded.
    _max_new_tokens = int(os.environ.get("CHAT_MAX_NEW_TOKENS", "512"))
    _system_prompt = _build_system_prompt()
    api_key = os.environ.get("CHAT_API_KEY", "").strip()
    if api_key:
        base_url = os.environ.get("CHAT_API_BASE_URL", "").strip().rstrip("/")
        api_model = os.environ.get("CHAT_API_MODEL", "").strip()
        if not base_url or not api_model:
            logger.error("CHAT_API_KEY is set but CHAT_API_BASE_URL or CHAT_API_MODEL is missing; chat is disabled")
            return
        _remote = {"url": f"{base_url}/chat/completions", "key": api_key, "model": api_model}
        logger.info("Chat uses the hosted model %s at %s", api_model, base_url)
        return

    model_name = os.environ.get("CHAT_MODEL_NAME", DEFAULT_MODEL_NAME)
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        device = "cuda" if torch.cuda.is_available() else "cpu"
        # The checkpoint's bfloat16 runs about half as fast as float32 on CPUs without bf16 support.
        dtype = "auto" if device == "cuda" else torch.float32
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForCausalLM.from_pretrained(model_name, dtype=dtype).to(device)
        model.eval()
    except Exception:
        # The CRM must keep running without the optional AI dependencies or model files.
        logger.exception("Chat model %s could not be loaded; /api/chat is disabled", model_name)
        return
    _model, _tokenizer = model, tokenizer
    logger.info("Chat model %s loaded on %s", model_name, device)


# Release the model's memory when the application shuts down.
def unload_chat_model() -> None:
    """Drop references to the model, tokenizer, and hosted API settings."""
    global _remote, _model, _tokenizer
    _remote = _model = _tokenizer = None


# Combine the assistant's rules with the CRM reference so answers about the product stay accurate.
def _build_system_prompt() -> str:
    """Append the knowledge file to the system prompt, or use the rules alone if it is missing."""
    try:
        knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Knowledge file %s is missing; chat answers without the CRM reference", KNOWLEDGE_FILE)
        return SYSTEM_PROMPT
    # The HTML comment holds editing notes for people, not instructions for the model.
    knowledge = re.sub(r"<!--.*?-->", "", knowledge, flags=re.DOTALL).strip()
    return f"{SYSTEM_PROMPT}\n\n--- Reference ---\n{knowledge}"


# Shared conversation sent to either the hosted API or the local model.
def _messages(message: str) -> list[dict[str, str]]:
    """Wrap the user's message with the CRM system prompt."""
    return [
        {"role": "system", "content": _system_prompt},
        {"role": "user", "content": message.strip()},
    ]


# Refuse chat requests when neither the hosted API nor a local model is ready.
def _require_chat() -> None:
    """Raise 503 when chat is not configured on this server."""
    if _remote is None and (_model is None or _tokenizer is None):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Chat model is not available")


# Open a request to the OpenAI-compatible chat completions endpoint.
def _remote_request(message: str, stream: bool):
    """Send the conversation to the hosted API and return the open HTTP response."""
    body = json.dumps(
        {
            "model": _remote["model"],
            "messages": _messages(message),
            "max_tokens": _max_new_tokens,
            "stream": stream,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _remote["url"],
        data=body,
        headers={"Authorization": f"Bearer {_remote['key']}", "Content-Type": "application/json"},
    )
    try:
        return urllib.request.urlopen(request, timeout=REMOTE_TIMEOUT_SECONDS)
    except (urllib.error.URLError, TimeoutError) as error:
        detail = error.read().decode("utf-8", "replace")[:500] if isinstance(error, urllib.error.HTTPError) else ""
        logger.error("Hosted chat request failed: %s %s", error, detail)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Chat service did not respond") from error


# Yield the text pieces of a streamed (server-sent events) chat completion.
def _remote_pieces(response) -> Iterator[str]:
    """Read "data:" lines from the hosted API and yield each piece of new text."""
    with response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or []
            text = choices[0].get("delta", {}).get("content") if choices else None
            if text:
                yield text


# Turn a user message into model-ready tensors for the local model.
def _prompt_inputs(message: str):
    """Apply the chat template to the message and tokenize it for generation."""
    prompt = _tokenizer.apply_chat_template(_messages(message), tokenize=False, add_generation_prompt=True)
    return _tokenizer([prompt], return_tensors="pt").to(_model.device)


@router.get("/chat/status")
# Let the frontend show the chat button only when chat can answer.
def chat_status() -> dict[str, bool]:
    """Report whether the hosted API or a local model is ready."""
    return {"available": _remote is not None or _model is not None}


@router.post("/chat/stream")
# Stream the answer as plain text so the first words appear while the rest is generated.
def chat_stream(payload: ChatInput) -> StreamingResponse:
    """Send each piece of the answer as soon as the hosted API or local model produces it."""
    _require_chat()
    # Nginx buffers proxied responses by default, which would hold the stream until it ends.
    headers = {"X-Accel-Buffering": "no"}
    if _remote is not None:
        # Open the request before streaming starts so a failure still returns an error status.
        response = _remote_request(payload.message, stream=True)
        return StreamingResponse(_remote_pieces(response), media_type="text/plain; charset=utf-8", headers=headers)

    inputs = _prompt_inputs(payload.message)

    import torch
    from transformers import TextIteratorStreamer

    streamer = TextIteratorStreamer(_tokenizer, skip_prompt=True, skip_special_tokens=True)

    def generate() -> None:
        try:
            with _generation_lock, torch.inference_mode():
                _model.generate(**inputs, max_new_tokens=_max_new_tokens, streamer=streamer)
        except Exception:
            logger.exception("Chat generation failed")
            # generate() only closes the stream on success; close it here so the response ends.
            streamer.end()

    threading.Thread(target=generate, daemon=True).start()
    return StreamingResponse(streamer, media_type="text/plain; charset=utf-8", headers=headers)


@router.post("/chat", response_model=ChatReply)
# Answer a free-form question with the hosted API or the local language model.
def chat(payload: ChatInput) -> ChatReply:
    """Return the whole generated reply at once."""
    _require_chat()
    if _remote is not None:
        with _remote_request(payload.message, stream=False) as response:
            result = json.load(response)
        return ChatReply(reply=result["choices"][0]["message"]["content"].strip())

    inputs = _prompt_inputs(payload.message)

    import torch

    with _generation_lock, torch.inference_mode():
        output_ids = _model.generate(**inputs, max_new_tokens=_max_new_tokens)

    # generate() returns the prompt followed by the answer; keep only the new tokens.
    new_tokens = output_ids[0][inputs["input_ids"].shape[1]:]
    reply = _tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    return ChatReply(reply=reply)
