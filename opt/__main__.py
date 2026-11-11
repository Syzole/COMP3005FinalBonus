# Enables `python -m opt …` the same way as `python -m sql` / `python -m exec`.
from opt.cli import main

raise SystemExit(main())
