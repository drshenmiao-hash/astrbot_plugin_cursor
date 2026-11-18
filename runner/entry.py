#!/usr/bin/python3
import sys

sys.path.insert(0, "/opt/cursor-runner")
from cursor_plugin.runner import main

raise SystemExit(main())
