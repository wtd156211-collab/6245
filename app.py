#!/usr/bin/env python3
"""viewkeep 唯一入口: build / apply / dump / query / serve."""

import sys

from viewkeep.cli import main

if __name__ == "__main__":
    sys.exit(main())
