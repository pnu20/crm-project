from __future__ import annotations

import logging
import os
import threading

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


DEFAULT_MODEL_NAME = "Qwen/Qwen2.5-1.5B-Instruct"
SYSTEM_PROMPT = (
    "You are a helpful assistant inside the Withaq CRM. "
    "Answer clearly and reply in the same language the user writes in. "
    "Keep answers short, a few sentences, unless the user asks for detail."
)
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])

# Filled by load_chat_model() during startup; stays None if the model is unavailable.
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


# Load the model once at startup so requests do not pay the loading cost.
def load_chat_model() -> None:
    """Load the tokenizer and model, leaving chat disabled if that fails."""
    global _model, _tokenizer, _max_new_tokens
    # Read settings here, not at import time, so values from .env are already loaded.
    model_name = os.environ.get("CHAT_MODEL_NAME", DEFAULT_MODEL_NAME)
    _max_new_tokens = int(os.environ.get("CHAT_MAX_NEW_TOKENS", "512"))
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
    """Drop references to the model and tokenizer."""
    global _model, _tokenizer
    _model = _tokenizer = None


# Turn a user message into model-ready tensors, or refuse when no model is loaded.
def _prompt_inputs(message: str):
    """Apply the chat template to the message and tokenize it for generation."""
    if _model is None or _tokenizer is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Chat model is not available")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": message.strip()},
    ]
    prompt = _tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return _tokenizer([prompt], return_tensors="pt").to(_model.device)


@router.post("/chat/stream")
# Stream the answer as plain text so the first words appear while the rest is generated.
def chat_stream(payload: ChatInput) -> StreamingResponse:
    """Generate in a background thread and send each decoded piece as soon as it is ready."""
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
    # Nginx buffers proxied responses by default, which would hold the stream until it ends.
    return StreamingResponse(
        streamer, media_type="text/plain; charset=utf-8", headers={"X-Accel-Buffering": "no"}
    )


@router.post("/chat", response_model=ChatReply)
# Answer a free-form question with the local language model.
def chat(payload: ChatInput) -> ChatReply:
    """Run the message through the chat template and return the generated reply."""
    inputs = _prompt_inputs(payload.message)

    import torch

    with _generation_lock, torch.inference_mode():
        output_ids = _model.generate(**inputs, max_new_tokens=_max_new_tokens)

    # generate() returns the prompt followed by the answer; keep only the new tokens.
    new_tokens = output_ids[0][inputs["input_ids"].shape[1]:]
    reply = _tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    return ChatReply(reply=reply)
