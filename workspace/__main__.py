# SPDX-License-Identifier: MPL-2.0
"""`python3 -m workspace` entry point."""

import sys

if "--safe-mode" in sys.argv[1:]:
    from .recovery import main
else:
    from .cli import main

raise SystemExit(main())
