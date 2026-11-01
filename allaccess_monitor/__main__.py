"""
Executable module entrypoint for AllAccess Foo Fighters Ticket Monitor.
Allows direct execution: `python -m allaccess_monitor`.
"""
import sys
from allaccess_monitor.cli import main

if __name__ == "__main__":
    sys.exit(main())
