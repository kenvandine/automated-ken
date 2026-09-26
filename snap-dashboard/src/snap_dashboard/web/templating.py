"""Single shared Jinja2 template environment for the whole web app.

Every route module used to instantiate its own ``Jinja2Templates(...)``
pointed at the same templates directory — harmless for rendering, but it
meant registering a Jinja global (like ``app_version()``, used in
base.html's footer) on just one of those instances left it undefined in
every other router, since each had its own separate ``jinja2.Environment``.
Importing this single instance everywhere fixes that for good.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from snap_dashboard.version import get_app_version

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["app_version"] = get_app_version
