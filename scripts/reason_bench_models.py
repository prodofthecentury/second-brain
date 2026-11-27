"""Raw model clients for the reasoning benchmark.

These clients do not call ``chat_with_fallback``. They do not merge hardware
options, redact PII, rewrite think blocks, or swap the requested model.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx


class ModelClientError(RuntimeError):
    """Identity, endpoint, or contract failure. The CLI turns this into exit 2."""


FORMAT_SUFFIX_MARK = "ANSWER: <answer>"
ANSWER_LINE = __import__("re").compile(r"^\s*ANSWER\s*:", __import__("re").MULTILINE)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assert_prompt_sent(prompt: str, serialized: str) -> str:
    """Hash the message content of the serialized request body.

    The check parses the bytes that would be sent. A body whose message
    content is not the intended prompt fails, including when a test passes
    a tampered body.
    """
    try:
        body = json.loads(serialized)
        sent = str(body["messages"][0]["content"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ModelClientError("serialized request body has no message content") from exc
    sent_hash = sha256_text(sent)
    if sent_hash != sha256_text(prompt):
        raise ModelClientError("sha256(prompt sent) does not equal sha256(prompt intended)")
    return sent_hash


def _digest_for_exact_name(payload: dict[str, Any], model: str) -> str:
    """Digest from /api/tags for the exact requested name. No architecture fallback."""
    wanted = str(model or "").strip()
    for row in payload.get("models") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        model_field = str(row.get("model") or "")
        if wanted and wanted in {name, model_field}:
            digest = str(row.get("digest") or "")
            if digest:
                return digest
            raise ModelClientError(f"Ollama /api/tags lists {wanted} without a digest")
    raise ModelClientError(f"Ollama /api/tags has no digest for the exact model name {wanted!r}")


def physical_core_count() -> int:
    """Physical cores, not logical CPUs. Falls back only when topology is unreadable."""
    root = __import__("pathlib").Path("/sys/devices/system/cpu")
    found: set[tuple[str, str]] = set()
    if root.is_dir():
        for cpu in root.glob("cpu[0-9]*"):
            package = cpu / "topology" / "physical_package_id"
            core = cpu / "topology" / "core_id"
            try:
                found.add((package.read_text(encoding="utf-8").strip(), core.read_text(encoding="utf-8").strip()))
            except OSError:
                continue
    if found:
        return len(found)
    return os.cpu_count() or 1


def canonical_options(options: dict[str, Any]) -> str:
    return json.dumps(options, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _host_is_local(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def assert_model_env(requested: str) -> None:
    override = (os.environ.get("BRAIN_GEN_MODEL") or "").strip()
    if override and override != requested:
        raise ModelClientError(
            f"BRAIN_GEN_MODEL={override!r} would override the requested model {requested!r}"
        )


@dataclass
class Generation:
    text: str
    raw_output: str
    prompt: str
    sha256_intended: str
    sha256_sent: str
    options_effective: dict[str, Any]
    completion_tokens: int | None
    prompt_tokens: int | None
    eval_ms: float | None
    prompt_eval_ms: float | None
    token_counts_derived: bool
    early_stopped: bool
    truncated: bool
    model: str
    tokens_estimated: bool = False


@dataclass
class MockScript:
    """Deterministic scripted backend. Records every prompt it is shown."""

    responses: dict[str, str] = field(default_factory=dict)
    default: str = "ANSWER: UNKNOWN"
    prompts: list[str] = field(default_factory=list)
    sleep_s: float = 0.0
    completion_tokens: int = 8

    def reply(self, prompt: str, item_id: str | None = None) -> str:
        self.prompts.append(prompt)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if item_id and item_id in self.responses:
            return self.responses[item_id]
        if "Return one JSON object" in prompt and "__plan__" in self.responses:
            return self.responses["__plan__"]
        for key, value in self.responses.items():
            if key and key in prompt:
                return value
        return self.default


class RawChatClient:
    def __init__(
        self,
        *,
        backend: str,
        model: str,
        base_url: str,
        allow_remote: bool = False,
        api_key_env: str | None = None,
        num_thread: int | None = None,
        num_ctx: int = 4096,
        temperature: float = 0.0,
        seed: int = 1234,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self.backend = backend
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.allow_remote = allow_remote
        self.api_key_env = api_key_env
        self.num_thread = num_thread
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.seed = seed
        self.timeout = timeout or httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
        self.client = httpx.Client(timeout=self.timeout)
        self.digest: str | None = None
        self.quantization: str | None = None
        self.parameter_size: str | None = None
        self.server_version: str | None = None
        assert_model_env(model)
        if not allow_remote and not _host_is_local(self.base_url if "://" in self.base_url else f"http://{self.base_url}"):
            raise ModelClientError(f"refusing non-local endpoint {self.base_url}")

    def close(self) -> None:
        self.client.close()

    def _options(self, *, num_predict: int, temperature: float | None, seed: int | None, top_p: float | None, early_stop: bool) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": self.temperature if temperature is None else temperature,
            "seed": self.seed if seed is None else seed,
            "num_predict": num_predict,
            "num_ctx": self.num_ctx,
            "num_thread": self.num_thread,
            "early_stop": early_stop,
        }
        if top_p is not None:
            options["top_p"] = top_p
        return options

    def generate(
        self,
        prompt: str,
        *,
        num_predict: int,
        temperature: float | None = None,
        seed: int | None = None,
        top_p: float | None = None,
    ) -> Generation:
        early_stop = FORMAT_SUFFIX_MARK in prompt or "ANSWER: UNKNOWN" in prompt
        options = self._options(
            num_predict=num_predict,
            temperature=temperature,
            seed=seed,
            top_p=top_p,
            early_stop=early_stop,
        )
        intended = sha256_text(prompt)
        if self.backend == "ollama":
            return self._ollama(prompt, options, intended, early_stop)
        if self.backend == "openai":
            return self._openai(prompt, options, intended, early_stop)
        raise ModelClientError(f"unknown backend {self.backend}")

    def warm(self) -> dict[str, Any]:
        if self.backend != "ollama":
            self.digest = "openai"
            return {"digest": self.digest, "model": self.model}
        response = self.client.get(f"{self.base_url}/api/tags")
        response.raise_for_status()
        self.digest = _digest_for_exact_name(response.json(), self.model)
        show = self.client.post(f"{self.base_url}/api/show", json={"model": self.model})
        body = show.json() if show.status_code == 200 else {}
        details = body.get("details") or {}
        self.quantization = str(details.get("quantization_level") or "")
        self.parameter_size = str(details.get("parameter_size") or "")
        version = self.client.get(f"{self.base_url}/api/version")
        if version.status_code == 200:
            self.server_version = str((version.json() or {}).get("version") or "")
        if not self.digest:
            raise ModelClientError("Ollama /api/tags did not return a digest for the exact model name")
        return {
            "digest": self.digest,
            "quantization": self.quantization,
            "parameter_size": self.parameter_size,
            "server_version": self.server_version,
            "model": self.model,
        }

    def assert_digest_unchanged(self) -> None:
        if self.backend != "ollama" or not self.digest:
            return
        previous = self.digest
        self.warm()
        if self.digest != previous:
            raise ModelClientError(f"model digest changed during the run ({previous} -> {self.digest})")

    def _ollama(self, prompt: str, options: dict[str, Any], intended: str, early_stop: bool) -> Generation:
        body_options = {
            "temperature": options["temperature"],
            "seed": options["seed"],
            "num_predict": options["num_predict"],
            "num_ctx": options["num_ctx"],
        }
        if options.get("num_thread") is not None:
            body_options["num_thread"] = options["num_thread"]
        if options.get("top_p") is not None:
            body_options["top_p"] = options["top_p"]
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "options": body_options,
            "keep_alive": "30m",
        }
        serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        sent_hash = assert_prompt_sent(prompt, serialized)
        text_parts: list[str] = []
        tokens = 0
        prompt_tokens = None
        prompt_eval_ms = None
        eval_ms = None
        derived = False
        stopped_early = False
        truncated = False
        response_model = None
        first_token_at = None
        last_token_at = None
        with self.client.stream("POST", f"{self.base_url}/api/chat", json=body) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                response_model = event.get("model") or response_model
                message = event.get("message") or {}
                piece = str(message.get("content") or "")
                if piece:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    last_token_at = time.perf_counter()
                    text_parts.append(piece)
                    tokens += 1
                if event.get("prompt_eval_count") is not None:
                    prompt_tokens = int(event["prompt_eval_count"])
                if event.get("prompt_eval_duration") is not None:
                    prompt_eval_ms = float(event["prompt_eval_duration"]) / 1_000_000.0
                if event.get("done"):
                    if event.get("eval_count") is not None and not stopped_early:
                        tokens = int(event["eval_count"])
                        derived = False
                    if event.get("eval_duration") is not None and not stopped_early:
                        eval_ms = float(event["eval_duration"]) / 1_000_000.0
                    if tokens >= int(options["num_predict"]) and not _has_answer_line("".join(text_parts)):
                        truncated = True
                    break
                if early_stop and _answer_line_closed("".join(text_parts)):
                    stopped_early = True
                    derived = True
                    response.close()
                    break
                if tokens >= int(options["num_predict"]):
                    if not _has_answer_line("".join(text_parts)):
                        truncated = True
                    if early_stop:
                        stopped_early = True
                        derived = True
                        response.close()
                        break
        if response_model and response_model != self.model:
            raise ModelClientError(f"response model {response_model!r} does not equal requested {self.model!r}")
        if derived and first_token_at is not None and last_token_at is not None:
            eval_ms = (last_token_at - first_token_at) * 1000.0
            prompt_tokens = None if prompt_tokens is None else prompt_tokens
        text = "".join(text_parts)
        return Generation(
            text=text,
            raw_output=text,
            prompt=prompt,
            sha256_intended=intended,
            sha256_sent=sent_hash,
            options_effective=options,
            completion_tokens=tokens,
            prompt_tokens=prompt_tokens,
            eval_ms=eval_ms,
            prompt_eval_ms=prompt_eval_ms,
            token_counts_derived=derived,
            early_stopped=stopped_early,
            truncated=truncated,
            model=self.model,
        )

    def _openai(self, prompt: str, options: dict[str, Any], intended: str, early_stop: bool) -> Generation:
        key = ""
        if self.api_key_env:
            key = os.environ.get(self.api_key_env) or ""
            if not key:
                raise ModelClientError(f"environment variable {self.api_key_env} is empty")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "temperature": options["temperature"],
            "seed": options["seed"],
            "max_tokens": options["num_predict"],
        }
        serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        sent_hash = assert_prompt_sent(prompt, serialized)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        text_parts: list[str] = []
        tokens = 0
        usage_completion = None
        usage_prompt = None
        stopped_early = False
        truncated = False
        response_model = None
        estimated = False
        first_token_at = None
        last_token_at = None
        with self.client.stream("POST", f"{self.base_url}/v1/chat/completions", json=body, headers=headers) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                response_model = event.get("model") or response_model
                choices = event.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        last_token_at = time.perf_counter()
                        text_parts.append(str(delta))
                        tokens += 1
                usage = event.get("usage") or {}
                if usage.get("completion_tokens") is not None:
                    usage_completion = int(usage["completion_tokens"])
                if usage.get("prompt_tokens") is not None:
                    usage_prompt = int(usage["prompt_tokens"])
                if early_stop and _answer_line_closed("".join(text_parts)):
                    stopped_early = True
                    response.close()
                    break
                if tokens >= int(options["num_predict"]):
                    if not _has_answer_line("".join(text_parts)):
                        truncated = True
                    break
        if response_model and response_model != self.model:
            raise ModelClientError(f"response model {response_model!r} does not equal requested {self.model!r}")
        if usage_completion is None:
            estimated = True
            usage_completion = max(tokens, len("".join(text_parts)) // 4)
        derived = stopped_early or estimated
        eval_ms = None
        if first_token_at is not None and last_token_at is not None:
            eval_ms = (last_token_at - first_token_at) * 1000.0
        text = "".join(text_parts)
        return Generation(
            text=text,
            raw_output=text,
            prompt=prompt,
            sha256_intended=intended,
            sha256_sent=sent_hash,
            options_effective=options,
            completion_tokens=usage_completion,
            prompt_tokens=usage_prompt,
            eval_ms=eval_ms,
            prompt_eval_ms=None,
            token_counts_derived=derived,
            early_stopped=stopped_early,
            truncated=truncated,
            model=self.model,
            tokens_estimated=estimated,
        )


def _has_answer_line(text: str) -> bool:
    return ANSWER_LINE.search(text) is not None


def _answer_line_closed(text: str) -> bool:
    """True once an ANSWER line has been terminated by a newline."""
    match = None
    for match in ANSWER_LINE.finditer(text):
        pass
    if match is None:
        return False
    return "\n" in text[match.end():]


def iter_stream_lines(chunks: Iterator[bytes]) -> Iterator[str]:
    buffer = ""
    for chunk in chunks:
        buffer += chunk.decode("utf-8")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer


def ollama_base_from_utils() -> str:
    """Reuse only the loopback/LAN host validation in ``ollama_base_url``."""
    from scripts.ollama_utils import ollama_base_url

    return ollama_base_url()
