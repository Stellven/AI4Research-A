"""CodexRuntime timeout enforcement. `codex` is a node wrapper that spawns a grandchild worker;
subprocess.run(timeout=) only SIGKILLs the direct child and then blocks in communicate() on the
grandchild's inherited pipes — so a network-stuck codex call ignored the 300s timeout for hours and
leaked orphan processes (observed: an 8.5h "300s" timeout). The fix runs codex in its own process
group and SIGKILLs the whole group on timeout. These tests pin that behavior."""
import os
import subprocess
import time
import unittest
from unittest import mock

from ai4research import model_runtime
from ai4research.model_runtime import CodexRuntime, ModelRuntimeError, _kill_process_group


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class KillProcessGroupTest(unittest.TestCase):
    def test_kills_wrapper_and_grandchild_promptly(self):
        # a shell (group leader) that backgrounds a grandchild sleeper and waits — mirrors codex's
        # node-wrapper -> worker shape. _kill_process_group must take down the WHOLE group fast.
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 600 & echo $!; wait"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        grandchild = int(proc.stdout.readline().strip())
        self.assertTrue(_alive(grandchild))

        start = time.monotonic()
        _kill_process_group(proc)
        self.assertLess(time.monotonic() - start, 11)      # bounded — not 600s
        self.assertIsNotNone(proc.poll())                  # wrapper reaped

        deadline = time.monotonic() + 3                    # grandchild gone (group SIGKILL), allow reap
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild), "grandchild survived the process-group kill")


class CodexTimeoutControlFlowTest(unittest.TestCase):
    def test_timeout_raises_modelruntimeerror_and_kills_group(self):
        fake = mock.Mock()
        fake.communicate.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=1)
        with mock.patch.object(model_runtime.shutil, "which", return_value="/usr/bin/codex"), \
             mock.patch.object(model_runtime.subprocess, "Popen", return_value=fake) as popen, \
             mock.patch.object(model_runtime, "_kill_process_group") as killer:
            with self.assertRaises(ModelRuntimeError) as cm:
                CodexRuntime(timeout=1).propose("hi", {"type": "object", "properties": {}})
        self.assertIn("timed out", str(cm.exception))
        killer.assert_called_once_with(fake)               # the whole tree is killed, not left orphaned
        self.assertTrue(popen.call_args.kwargs.get("start_new_session"))  # own group so the kill works


if __name__ == "__main__":
    unittest.main()
