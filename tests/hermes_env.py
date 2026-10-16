"""Shared test settings: the scratch directory and the optional Hermes source checkout.

Native contract tests run snippets under the Hermes interpreter. Point HERMES_AGENT_SRC at a
Hermes Agent checkout with its venv (default ~/.hermes/hermes-agent); they skip when absent.
"""
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
(ROOT / 'scratch').mkdir(exist_ok=True)

HERMES_SOURCE = Path(os.environ.get('HERMES_AGENT_SRC') or '~/.hermes/hermes-agent').expanduser()
HERMES_PYTHON = HERMES_SOURCE / 'venv/bin/python'
requires_hermes = unittest.skipUnless(
    HERMES_PYTHON.exists(), f'Hermes source not found at {HERMES_SOURCE} (set HERMES_AGENT_SRC)')
