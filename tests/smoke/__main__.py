# SPDX-License-Identifier: MPL-2.0
"""Run the native smoke scenario: python3 -m tests.smoke [--controls|--capture] [--state DIR]."""

import sys

from workspace.cli import parser

from .scenario import run_smoke


def main():
    arguments = parser()
    arguments.description = __doc__
    arguments.add_argument("--controls", action="store_true",
                           help="check only the pane menus and sort controls")
    arguments.add_argument("--capture", action="store_true",
                           help="capture the Commander view and exit")
    args = arguments.parse_args()
    args.smoke = True          # Window reads this to size itself and expose a paintable
    args.maximized = True
    return run_smoke(args, controls_only=args.controls, capture_only=args.capture)


if __name__ == "__main__":
    sys.exit(main())
