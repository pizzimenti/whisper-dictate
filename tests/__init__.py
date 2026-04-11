"""Test package for kdictate.

Side effect at import time: install a NullHandler on the
``kdictate.tests`` logger and force ``propagate = False``.

**Why:** Tests that construct real daemon components (controllers,
bridges, DictationDaemon, etc.) with a test-side logger named
``kdictate.tests`` would otherwise let log records propagate up
through the ``kdictate`` ancestor chain. If any code path in the
test process has previously attached a ``FileHandler`` to
``kdictate`` (for example, a test that calls real ``daemon.main()``
without mocking ``configure_logging`` first), those propagated
records would leak into the production ``~/.local/state/kdictate/
daemon.log`` file — poisoning the developer's real daemon log with
test output.

PR #6 (commit b1cc382) fixed the specific test that was creating
the leaked FileHandler by patching ``configure_logging`` in that
test. PR #6 (the commit that added this file) additionally narrowed
the production namespace to ``kdictate.daemon`` so the daemon's
``FileHandler`` lives under a sibling subtree rather than the root.
This NullHandler on ``kdictate.tests`` is the third layer of
defense: even if a future test accidentally creates a FileHandler
on ``kdictate`` (or on ``kdictate.tests`` itself), the NullHandler
and ``propagate=False`` combination means records under
``kdictate.tests.*`` have a terminal handler and cannot bubble up
to leak anywhere else.
"""

from __future__ import annotations

import logging

_tests_logger = logging.getLogger("kdictate.tests")
_tests_logger.propagate = False
if not any(isinstance(h, logging.NullHandler) for h in _tests_logger.handlers):
    _tests_logger.addHandler(logging.NullHandler())
