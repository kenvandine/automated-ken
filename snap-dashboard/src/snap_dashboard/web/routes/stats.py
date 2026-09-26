"""Usage statistics / "cool stats" dashboard.

Aggregates counters across the models that already track history (test
runs, agent runs, version-bump PRs, runners) into a single page so users
can see how much autonomous work the platform has done for them over time.
"""

from __future__ import annotations


from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func

from snap_dashboard.auth import get_current_user
from snap_dashboard.db.models import AgentRun, ModelUsage, Runner, TestRun, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.web.templating import templates

router = APIRouter()

_PROMOTED_TEST_STATUSES = ("passed", "promoted")
_FAILED_TEST_STATUSES = ("failed", "error", "cancelled")


def _aggregate_model_usage(session) -> dict:
    """Aggregate ModelUsage rows by model and by provider (local vs. cloud).

    Extracted from stats_page() so the aggregation math is unit-testable
    without going through the HTTP/template layer.
    """
    usage_rows = (
        session.query(
            ModelUsage.provider,
            ModelUsage.model,
            func.count(ModelUsage.id),
            func.sum(ModelUsage.input_tokens),
            func.sum(ModelUsage.output_tokens),
            func.sum(ModelUsage.estimated),
        )
        .group_by(ModelUsage.provider, ModelUsage.model)
        .order_by(ModelUsage.provider, ModelUsage.model)
        .all()
    )
    model_usage = [
        {
            "provider": provider,
            "model": model,
            "calls": calls,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "total_tokens": int(input_tokens or 0) + int(output_tokens or 0),
            "any_estimated": bool(estimated_count),
        }
        for provider, model, calls, input_tokens, output_tokens, estimated_count in usage_rows
    ]

    usage_by_provider: dict[str, dict[str, int]] = {}
    for row in model_usage:
        bucket = usage_by_provider.setdefault(
            row["provider"], {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        )
        bucket["input_tokens"] += row["input_tokens"]
        bucket["output_tokens"] += row["output_tokens"]
        bucket["calls"] += row["calls"]

    local_tokens = sum(
        b["input_tokens"] + b["output_tokens"]
        for provider, b in usage_by_provider.items()
        if provider == "lemonade"
    )
    cloud_tokens = sum(
        b["input_tokens"] + b["output_tokens"]
        for provider, b in usage_by_provider.items()
        if provider != "lemonade"
    )
    total_tokens = local_tokens + cloud_tokens
    local_token_share = round(100 * local_tokens / total_tokens, 1) if total_tokens else None

    return {
        "model_usage": model_usage,
        "usage_by_provider": usage_by_provider,
        "local_tokens": local_tokens,
        "cloud_tokens": cloud_tokens,
        "total_tokens": total_tokens,
        "local_token_share": local_token_share,
    }


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    with get_session() as session:
        total_test_runs = session.query(func.count(TestRun.id)).scalar() or 0
        passed_test_runs = (
            session.query(func.count(TestRun.id))
            .filter(TestRun.status.in_(_PROMOTED_TEST_STATUSES))
            .scalar()
            or 0
        )
        failed_test_runs = (
            session.query(func.count(TestRun.id))
            .filter(TestRun.status.in_(_FAILED_TEST_STATUSES))
            .scalar()
            or 0
        )
        promoted_runs = (
            session.query(func.count(TestRun.id))
            .filter(TestRun.promoted.is_(True))
            .scalar()
            or 0
        )
        pass_rate = (
            round(100 * passed_test_runs / total_test_runs, 1)
            if total_test_runs
            else None
        )

        runs_by_dispatch = dict(
            session.query(TestRun.dispatch_target, func.count(TestRun.id))
            .group_by(TestRun.dispatch_target)
            .all()
        )

        agent_run_counts = dict(
            session.query(AgentRun.agent_type, func.count(AgentRun.id))
            .group_by(AgentRun.agent_type)
            .all()
        )
        total_agent_runs = sum(agent_run_counts.values())

        version_bumps_total = session.query(func.count(VersionBumpPR.id)).scalar() or 0
        version_bumps_merged = (
            session.query(func.count(VersionBumpPR.id))
            .filter(VersionBumpPR.status == "merged")
            .scalar()
            or 0
        )
        version_bumps_needs_review = (
            session.query(func.count(VersionBumpPR.id))
            .filter(VersionBumpPR.status == "needs_review")
            .scalar()
            or 0
        )

        runners = session.query(Runner).order_by(Runner.name).all()
        runner_stats = []
        for r in runners:
            jobs_run = (
                session.query(func.count(TestRun.id))
                .filter(TestRun.runner_id == r.id)
                .scalar()
                or 0
            )
            jobs_passed = (
                session.query(func.count(TestRun.id))
                .filter(
                    TestRun.runner_id == r.id,
                    TestRun.status.in_(_PROMOTED_TEST_STATUSES),
                )
                .scalar()
                or 0
            )
            runner_stats.append(
                {"name": r.name, "status": r.status, "jobs_run": jobs_run, "jobs_passed": jobs_passed}
            )

        recent_runs = (
            session.query(TestRun)
            .order_by(TestRun.started_at.desc())
            .limit(10)
            .all()
        )
        recent_runs_data = [
            {
                "snap_name": tr.snap_name,
                "status": tr.status,
                "dispatch_target": tr.dispatch_target,
                "started_at": tr.started_at,
            }
            for tr in recent_runs
        ]

        usage_stats = _aggregate_model_usage(session)

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "current_user": user,
            "last_run": None,
            "total_test_runs": total_test_runs,
            "passed_test_runs": passed_test_runs,
            "failed_test_runs": failed_test_runs,
            "promoted_runs": promoted_runs,
            "pass_rate": pass_rate,
            "runs_by_dispatch": runs_by_dispatch,
            "agent_run_counts": agent_run_counts,
            "total_agent_runs": total_agent_runs,
            "version_bumps_total": version_bumps_total,
            "version_bumps_merged": version_bumps_merged,
            "version_bumps_needs_review": version_bumps_needs_review,
            "runner_stats": runner_stats,
            "recent_runs": recent_runs_data,
            **usage_stats,
        },
    )
