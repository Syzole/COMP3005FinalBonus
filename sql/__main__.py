# Enables `python -m sql ...` → same entry as the CLI
from sql.cli import main

raise SystemExit(main())
