"""python -m camunda_step — the same as python -m camunda_step.run."""

import sys

from .run import main

if __name__ == "__main__":
    sys.exit(main())
