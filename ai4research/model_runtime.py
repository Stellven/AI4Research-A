"""Model runtime seam for optional LLM-backed synthesis."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class ModelRuntimeError(Exception):
    """Raised when a model runtime cannot return structured proposals."""


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the whole codex process tree (the node wrapper + its grandchild worker) so a
    network-stuck call cannot outlive the timeout as an orphan holding the pipes open. The process
    was started with start_new_session=True, so its pid leads its own group."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)   # reap; bounded so a wedged group cannot re-hang the caller
    except (subprocess.TimeoutExpired, OSError):
        pass


class ModelRuntime(Protocol):
    name: str

    def propose(self, prompt: str, schema: dict) -> list[dict]:
        """Return JSON-parsed records matching the requested schema."""


class StubRuntime:
    name = "stub"

    def __init__(self, records=None, error: Exception | None = None):
        self.records = records if callable(records) else list(records or [])
        self.error = error

    def propose(self, prompt: str, schema: dict) -> list[dict]:
        if self.error:
            raise ModelRuntimeError(str(self.error))
        if callable(self.records):
            return list(self.records(prompt))
        return list(self.records)


class CodexRuntime:
    name = "codex"

    def __init__(self, timeout: int = 300):   # synthesis (a long dossier) needs more than 60s
        self.timeout = timeout

    def propose(self, prompt: str, schema: dict) -> list[dict]:
        if shutil.which("codex") is None:
            raise ModelRuntimeError("codex binary not found")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                schema_path = Path(tmp) / "schema.json"
                out_path = Path(tmp) / "last_message.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                # Headless/stateless invocation: prompt via stdin, ephemeral session, read-only sandbox,
                # structured output schema, final model message written to a temp file.
                cmd = [
                    "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                    "--output-schema", str(schema_path), "--output-last-message", str(out_path), "-",
                ]
                # `codex` is a node wrapper that spawns a grandchild worker. subprocess.run(timeout=)
                # only SIGKILLs the direct child and then blocks in communicate() on the grandchild's
                # inherited pipes — so a network-stuck codex call ignores the timeout for hours and
                # leaks orphans. Run it in its own process group and SIGKILL the WHOLE group on timeout.
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True,
                )
                try:
                    out, err = proc.communicate(input=prompt, timeout=self.timeout)
                except subprocess.TimeoutExpired as exc:
                    _kill_process_group(proc)
                    raise ModelRuntimeError(f"codex exec timed out after {self.timeout}s") from exc
                if proc.returncode != 0:
                    raise ModelRuntimeError((err or out or "codex exec failed").strip())
                output = out_path.read_text(encoding="utf-8") if out_path.exists() else out
                data = _parse_records(output)
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelRuntimeError(str(exc)) from exc
        return [record for record in data if _valid_record(record, schema)]


def get_runtime(name: str) -> ModelRuntime:
    if name == "stub":
        return StubRuntime()
    if name == "codex":
        return CodexRuntime()
    raise ModelRuntimeError(f"unknown model runtime: {name}")


def _parse_records(text: str) -> list[dict]:
    """Normalize a model's output to a list of records, accepting either an array schema's
    `[{...}]` or an object schema's `{...}` (codex's --output-schema requires a top-level
    object, so single-object replies are wrapped)."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
        for open_c, close_c in (("{", "}"), ("[", "]")):
            start, end = text.find(open_c), text.rfind(close_c)
            if 0 <= start < end:
                try:
                    parsed = json.loads(text[start:end + 1])
                    break
                except json.JSONDecodeError:
                    continue
        if parsed is None:
            raise
    if isinstance(parsed, dict) and isinstance(parsed.get("records"), list):
        return parsed["records"]
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    raise ModelRuntimeError("model output was not a JSON object or array")


def _valid_record(record: dict, schema: dict) -> bool:
    if not isinstance(record, dict):
        return False
    item_schema = schema.get("items", schema)  # array schema -> items; object schema -> itself
    required = item_schema.get("required", [])
    properties = item_schema.get("properties", {})
    for key in required:
        if key not in record:
            return False
    for key, value in record.items():
        expected = properties.get(key, {}).get("type")
        if expected == "string" and not isinstance(value, str):
            return False
        if expected == "array" and not isinstance(value, list):
            return False
    return True
