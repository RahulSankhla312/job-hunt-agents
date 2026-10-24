"""LLM adapter.

Two interchangeable backends behind one interface:

``claude_cli``      shells out to the Claude Code binary in headless mode
                    (``claude -p``). Costs nothing beyond the existing
                    subscription, which is why it is the default.
``anthropic_api``   the normal Anthropic SDK, for when subscription auth is
                    unavailable or rate limits bite.

Everything downstream depends only on :func:`complete` / :func:`complete_json`,
so swapping backends is a one-line change in ``.env``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from jobhunt.config import settings

# Default models. Opus 5 for judgement (scoring, tailoring, drafting);
# Haiku 4.5 for bulk normalisation where volume matters more than nuance.
MODEL_JUDGE = "claude-opus-5"
MODEL_BULK = "claude-haiku-4-5"

# The CLI has no reason to touch the filesystem or network for our prompts.
_BLOCKED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "TodoWrite",
]


class LLMError(RuntimeError):
    """Any backend failure. Retried by :func:`complete`."""


class LLMAuthError(LLMError):
    """Backend reachable but not authenticated - retrying will not help."""


class Backend(Protocol):
    name: str

    def complete(self, prompt: str, *, system: str | None, model: str, max_tokens: int) -> str: ...

    def healthcheck(self) -> tuple[bool, str]: ...


# --------------------------------------------------------------------------
# Claude Code CLI (headless)
# --------------------------------------------------------------------------


def discover_claude_cli() -> Path | None:
    """Locate the Claude Code binary.

    The desktop app ships it under a versioned directory, so pick the highest
    version rather than hardcoding one that a future update will invalidate.
    """
    configured = settings().claude_cli_path
    if configured:
        p = Path(configured)
        if p.exists():
            return p

    found = shutil.which("claude")
    if found:
        return Path(found)

    local = Path(os.environ.get("LOCALAPPDATA", ""))
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code",
        local / "Claude" / "claude-code",
        Path.home() / ".claude" / "local",
        # Newer desktop apps keep it per profile: Claude-Data/<profile>/claude-code.
        *((local / "Claude-Data").glob("*/claude-code") if local.parts else []),
    ]
    name = "claude.exe" if os.name == "nt" else "claude"
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            # <version>/claude(.exe), or <version>/<build-id>/claude(.exe).
            exes = [child / name, *child.glob(f"*/{name}")] if child.is_dir() else []
            for exe in exes:
                if not exe.exists():
                    continue
                try:
                    version = tuple(int(x) for x in child.name.split("."))
                except ValueError:
                    version = (0,)
                candidates.append((version, exe))
    if candidates:
        return max(candidates)[1]
    return None


@dataclass
class ClaudeCLIBackend:
    name: str = "claude_cli"
    timeout: int = 300

    def _exe(self) -> Path:
        exe = discover_claude_cli()
        if exe is None:
            raise LLMError(
                "Claude Code CLI not found. Set CLAUDE_CLI_PATH in .env, or switch "
                "JOBHUNT_LLM_BACKEND=anthropic_api."
            )
        return exe

    def complete(self, prompt: str, *, system: str | None, model: str, max_tokens: int) -> str:
        cmd = [
            str(self._exe()),
            "--print",
            "--output-format", "json",
            "--model", model,
            "--strict-mcp-config",
            "--exclude-dynamic-system-prompt-sections",
            "--disallowed-tools", *_BLOCKED_TOOLS,
        ]
        if system:
            cmd += ["--system-prompt", system]

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude CLI timed out after {self.timeout}s") from exc

        combined = f"{proc.stdout}\n{proc.stderr}".lower()
        if any(s in combined for s in _AUTH_FAILURES):
            raise LLMAuthError(
                "Claude Code is not signed in (or its sign-in expired). Run this once in a "
                'real terminal and type /login:\n  "' + str(self._exe()) + '"'
            )
        if proc.returncode != 0:
            # With --output-format json the reason is in stdout, not stderr.
            reason = proc.stderr.strip() or _cli_error_text(proc.stdout)
            raise LLMError(f"claude CLI exited {proc.returncode}: {reason[:400]}")

        return _extract_cli_result(proc.stdout)

    def healthcheck(self) -> tuple[bool, str]:
        try:
            exe = self._exe()
        except LLMError as exc:
            return False, str(exc)
        try:
            out = self.complete(
                "Reply with the single word: OK",
                system="You are a health check. Reply with exactly one word.",
                model=MODEL_BULK,
                max_tokens=16,
            )
        except LLMAuthError as exc:
            return False, str(exc)
        except LLMError as exc:
            return False, f"{exe.name}: {exc}"
        return ("ok" in out.lower()), f"{exe} -> {out.strip()[:60]!r}"


_AUTH_FAILURES = (
    "not logged in",
    "please run /login",
    "failed to authenticate",
    "oauth session expired",
    "invalid api key",
)


def _cli_error_text(stdout: str) -> str:
    """The ``result`` message of a failed ``--output-format json`` run."""
    try:
        data = json.loads(stdout.strip() or "{}")
    except ValueError:
        return stdout.strip()
    return str(data.get("result") or "") if isinstance(data, dict) else ""


def _extract_cli_result(stdout: str) -> str:
    """Pull the assistant text out of ``--output-format json``.

    Falls back to raw stdout so a CLI schema change degrades instead of
    breaking outright.
    """
    stdout = stdout.strip()
    if not stdout:
        raise LLMError("claude CLI returned empty output")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(payload, dict):
        if payload.get("is_error"):
            raise LLMError(f"claude CLI reported an error: {str(payload.get('result'))[:300]}")
        for key in ("result", "text", "content", "response"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return stdout


# --------------------------------------------------------------------------
# Anthropic API
# --------------------------------------------------------------------------


@dataclass
class AnthropicAPIBackend:
    name: str = "anthropic_api"

    def _client(self):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("pip install anthropic to use the anthropic_api backend") from exc
        key = settings().anthropic_api_key
        if not key:
            raise LLMAuthError("ANTHROPIC_API_KEY is not set in .env")
        return anthropic.Anthropic(api_key=key)

    def complete(self, prompt: str, *, system: str | None, model: str, max_tokens: int) -> str:
        client = self._client()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            # Cached prefix: the system prompt is stable across a whole run.
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if model != MODEL_BULK:
            kwargs["thinking"] = {"type": "adaptive"}
        import anthropic

        try:
            msg = client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMAuthError(f"anthropic api rejected the key: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the caller
            raise LLMError(f"anthropic api: {exc}") from exc
        if msg.stop_reason == "refusal":
            raise LLMError("anthropic api declined the request (stop_reason=refusal)")
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    def healthcheck(self) -> tuple[bool, str]:
        try:
            out = self.complete(
                "Reply with the single word: OK", system=None, model=MODEL_BULK, max_tokens=16
            )
        except LLMError as exc:
            return False, str(exc)
        return ("ok" in out.lower()), f"anthropic api -> {out.strip()[:60]!r}"


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------

_BACKENDS: dict[str, type] = {
    "claude_cli": ClaudeCLIBackend,
    "anthropic_api": AnthropicAPIBackend,
}


def get_backend(name: str | None = None) -> Backend:
    key = (name or settings().jobhunt_llm_backend or "claude_cli").strip()
    if key not in _BACKENDS:
        raise LLMError(f"Unknown LLM backend {key!r}. Choose from {sorted(_BACKENDS)}.")
    return _BACKENDS[key]()


@retry(
    # LLMAuthError subclasses LLMError, so it has to be excluded explicitly or
    # a logged-out backend is retried with backoff for no benefit.
    retry=retry_if_exception_type(LLMError) & retry_if_not_exception_type(LLMAuthError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str = MODEL_JUDGE,
    max_tokens: int = 8000,
    backend: Backend | None = None,
) -> str:
    """Single completion, retried on transient failure.

    Auth failures are deliberately not retried - they need human action, and
    hammering a logged-out CLI three times only delays a clear error message.
    """
    be = backend or get_backend()
    return be.complete(prompt, system=system, model=model, max_tokens=max_tokens)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Best-effort JSON recovery from a model response.

    Models wrap JSON in prose or fences often enough that parsing the raw
    string is not good enough for an unattended pipeline.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue

    repaired = _repair_truncated_json(text)
    if repaired is not None:
        return repaired

    raise LLMError(f"Could not parse JSON from model output: {text[:300]!r}")


def _repair_truncated_json(text: str) -> Any | None:
    """Close a JSON document that was cut off mid-structure.

    A long tailoring response can hit ``max_tokens`` partway through, which
    otherwise discards a nearly complete answer.

    Cut points are tried strictest-first. A *strict* cut sits immediately after
    a closed container, so a truncated array of scored postings yields only
    whole postings - recovering a half-written element instead would hand the
    caller an object whose missing fields silently take their defaults, turning
    a truncation into a plausible-looking wrong answer. The *loose* cut, which
    also allows a completed string, is the fallback for a top-level object that
    had not closed any container before it was cut.
    """
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        return None
    body = text[start:]

    strict: list[tuple[int, tuple[str, ...]]] = []
    loose: list[tuple[int, tuple[str, ...]]] = []

    stack: list[str] = []
    in_string = False
    escaped = False

    for i, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                loose.append((i + 1, tuple(stack)))
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            strict.append((i + 1, tuple(stack)))
            loose.append((i + 1, tuple(stack)))
        elif ch == ",":
            # A comma proves the element before it finished, which is the only
            # signal that a bare scalar value is complete ("..., \"b\": 2,").
            loose.append((i, tuple(stack)))

    if not stack:
        return None  # already balanced - a different problem

    for cuts in (strict, loose):
        for cut, open_at_cut in reversed(cuts):
            if not open_at_cut:
                continue  # nothing left open: not a truncation point
            candidate = body[:cut].rstrip().rstrip(",")
            try:
                return json.loads(candidate + "".join(reversed(open_at_cut)))
            except json.JSONDecodeError:
                continue
    return None


def complete_json(
    prompt: str,
    *,
    system: str | None = None,
    model: str = MODEL_JUDGE,
    max_tokens: int = 8000,
    backend: Backend | None = None,
) -> Any:
    """Completion that must return JSON. Appends a format reminder."""
    guard = "\n\nRespond with valid JSON only. No prose, no markdown fences."
    return extract_json(
        complete(
            prompt + guard,
            system=system,
            model=model,
            max_tokens=max_tokens,
            backend=backend,
        )
    )
