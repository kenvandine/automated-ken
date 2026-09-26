"""Regression test for a Jinja2 dict/attribute-shadowing bug in snap_detail.html.

``review_report_data`` (built in web/routes/snaps.py's ``snap_detail``) used
to store the parsed review items under the key ``"items"``. Since it's a
plain ``dict``, Jinja's ``.`` operator tries attribute access before item
access — and ``dict.items`` resolves to the builtin bound method, not the
``"items"`` key, so ``{% for it in review_report.items %}`` blew up with
``TypeError: 'builtin_function_or_method' object is not iterable`` on any
snap that had ever been reviewed (see agents/issue_pr_reviewer.py). The fix
renamed the key to ``"review_items"``; this test renders the template
directly with the same shape of context the route builds, so any future
regression fails fast instead of only showing up as a live 500.
"""

from __future__ import annotations

from datetime import datetime, timezone

from snap_dashboard.web.templating import templates


class _FakeQueryParams:
    def get(self, key, default=None):
        return default


class _FakeRequest:
    query_params = _FakeQueryParams()


def _base_context(review_report):
    return {
        "request": _FakeRequest(),
        "snap": {
            "id": 1,
            "name": "duck-ai",
            "publisher": "kenvandine",
            "manually_added": False,
            "packaging_repo": "https://github.com/kenvandine/duck-ai",
            "upstream_repo": "https://github.com/kenvandine/duck-ai",
            "notes": "",
            "is_console_app": False,
            "is_service": False,
            "created_at": None,
            "updated_at": None,
            "packaging_repo_suggested": None,
            "upstream_repo_suggested": None,
            "same_repo": True,
        },
        "arch_map": {},
        "cm_rows": [],
        "issues": [],
        "test_runs": [],
        "rebuild_triggers": [],
        "review_report": review_report,
        "last_run": None,
        "channels": ["stable", "candidate", "beta", "edge"],
        "current_user": {"id": 1, "github_login": "kenvandine"},
    }


def test_renders_with_review_items_without_dict_items_shadowing_bug() -> None:
    review_report = {
        "summary": "1 open issue, 0 open pull requests.",
        "review_items": [
            {
                "owner_repo": "kenvandine/duck-ai",
                "number": 5,
                "type": "issue",
                "title": "Crash on launch",
                "url": "https://github.com/kenvandine/duck-ai/issues/5",
                "age_days": 3,
                "comments": 1,
                "body": "steps to reproduce",
            }
        ],
        "error_msg": None,
        "updated_at": datetime.now(timezone.utc),
    }
    html = templates.env.get_template("snap_detail.html").render(_base_context(review_report))
    assert "Crash on launch" in html
    assert "Assign an agent" in html


def test_renders_with_no_review_report() -> None:
    html = templates.env.get_template("snap_detail.html").render(_base_context(None))
    assert "duck-ai" in html


def test_renders_with_review_error_and_no_items() -> None:
    review_report = {
        "summary": "",
        "review_items": [],
        "error_msg": "No packaging or upstream repo configured.",
        "updated_at": datetime.now(timezone.utc),
    }
    html = templates.env.get_template("snap_detail.html").render(_base_context(review_report))
    assert "No packaging or upstream repo configured." in html
