"""Model runtime seam for optional LLM-backed synthesis."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class ModelRuntimeError(Exception):
    """Raised when a model runtime cannot return structured proposals."""


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

    def __init__(self, timeout: int = 60):
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
                proc = subprocess.run(
                    cmd, input=prompt, text=True, capture_output=True, timeout=self.timeout, check=False
                )
                if proc.returncode != 0:
                    raise ModelRuntimeError((proc.stderr or proc.stdout or "codex exec failed").strip())
                output = out_path.read_text(encoding="utf-8") if out_path.exists() else proc.stdout
                data = _parse_json_array(output)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise ModelRuntimeError(str(exc)) from exc
        return [record for record in data if _valid_record(record, schema)]


def get_runtime(name: str) -> ModelRuntime:
    if name == "stub":
        return StubRuntime()
    if name == "codex":
        return CodexRuntime()
    raise ModelRuntimeError(f"unknown model runtime: {name}")


def _parse_json_array(text: str) -> list[dict]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end < start:
            raise
        parsed = json.loads(text[start:end + 1])
    if isinstance(parsed, dict) and isinstance(parsed.get("records"), list):
        parsed = parsed["records"]
    if not isinstance(parsed, list):
        raise ModelRuntimeError("model output was not a JSON array")
    return parsed


def _valid_record(record: dict, schema: dict) -> bool:
    if not isinstance(record, dict):
        return False
    item_schema = schema.get("items", {})
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
