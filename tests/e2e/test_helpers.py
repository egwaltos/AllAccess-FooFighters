"""Helper mocks, context managers, and assertions for E2E testing."""
import io
import os
import sys
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from unittest.mock import MagicMock

from tests.fixtures import load_fixture
from tests.e2e.fixtures import build_allaccess_html


class MockResponse:
    """Mock requests.Response for deterministic HTTP test simulation."""
    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        url: str = "https://www.allaccess.com.ar/event/foo-fighters"
    ):
        self.status_code = status_code
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")
        self.content = self.text.encode("utf-8")
        self._json_data = json_data
        self.headers = headers or {}
        self.url = url

    def json(self) -> Dict[str, Any]:
        if self._json_data is not None:
            return self._json_data
        if self.text:
            return json.loads(self.text)
        raise ValueError("No JSON data in response")

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            import requests
            raise requests.exceptions.HTTPError(
                f"HTTP {self.status_code} Error",
                response=self
            )


@contextmanager
def capture_output():
    """Capture stdout and stderr during test execution."""
    new_out = io.StringIO()
    new_err = io.StringIO()
    old_out = sys.stdout
    old_err = sys.stderr
    try:
        sys.stdout = new_out
        sys.stderr = new_err
        yield new_out, new_err
    finally:
        sys.stdout = old_out
        sys.stderr = old_err


@contextmanager
def temporary_env(env_vars: Dict[str, str]):
    """Temporarily override environment variables."""
    old_env = {}
    for k, v in env_vars.items():
        old_env[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_cli_in_process(cli_main_func, argv: List[str]) -> Tuple[int, str, str]:
    """
    Run the CLI main entrypoint in-process with intercepted argv and captured output.
    Returns (exit_code, stdout_str, stderr_str).
    """
    exit_code = 0
    with capture_output() as (out, err):
        old_argv = sys.argv
        sys.argv = ["allaccess_monitor"] + argv
        try:
            res = cli_main_func(argv)
            if isinstance(res, int):
                exit_code = res
        except SystemExit as se:
            exit_code = se.code if isinstance(se.code, int) else (0 if se.code is None else 1)
        finally:
            sys.argv = old_argv
    return exit_code, out.getvalue(), err.getvalue()
