#!/usr/bin/env python
"""Job-hunt agent team - run from a checkout without installing.

    python run.py web       # dashboard: setup, filters, runs, the queue
    python run.py --help    # every command

Equivalent to ``python -m jobhunt`` or, after ``pip install -e .``, ``jobhunt``.
"""

from jobhunt.cli import main

if __name__ == "__main__":
    main()
