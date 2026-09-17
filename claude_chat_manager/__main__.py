"""Lets the package run as `python -m claude_chat_manager`."""

import sys

from .cli import main

sys.exit(main())
