"""Archy CLI."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import typer
from loguru import logger

from .config import get_settings
from .contracts.messages import DriftEvent
from .core.storage import get_repository
from .gemini.client import get_gemini_client
from .assistant.manager import Archy

app = typer.Typer(no_args_is_help=True, help="Archy backend CLI.")


def _fmt_local(dt: datetime | None) -> str:
    """Format a datetime in local time for display."""
    if dt is None:
        return "(none)"
    try:
        from tzlocal import get_localzone
        local_tz = get_localzone()
        local_dt = dt.astimezone(local_tz)
        return local_dt.strftime("%Y-%m-%d %I:%M %p (%Z)")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M UTC")


@app.command()
def serve() -> None:
    """Run the FastAPI + WebSocket server."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "archy.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


@app.command()
def ingest_text(text: str) -> None:
    """Ingest a text prompt through the pipeline."""
    async def _run():
        s = get_settings()
        repo = get_repository(s)
        gemini = get_gemini_client(s)
        assistant = Archy(s, repo, gemini)
        t = await assistant.ingest_text(text)
        # Print friendly summary with local time
        print("\n" + "=" * 60)
        print(f"  Task created: {t.title}")
        print(f"  Category: {t.category}")
        print(f"  Priority: {t.priority}/100 ({t.priority_label.value})")
        print(f"  Complexity: {t.complexity}/100")
        print(f"  Duration: {t.duration_minutes} min")
        if t.deadline:
            print(f"  Deadline: {_fmt_local(t.deadline)}")
        if t.scheduled_start:
            print(f"  Scheduled: {_fmt_local(t.scheduled_start)} → {_fmt_local(t.scheduled_end)}")
        print("=" * 60 + "\n")
        repo.close()

    asyncio.run(_run())


@app.command()
def ingest_audio(path: Path) -> None:
    """Ingest an audio file through the full pipeline (Gemini native audio)."""
    if not path.exists():
        typer.echo(f"File not found: {path}", err=True)
        raise typer.Exit(1)

    async def _run():
        s = get_settings()
        repo = get_repository(s)
        gemini = get_gemini_client(s)
        assistant = Archy(s, repo, gemini)
        audio = path.read_bytes()
        mime = _guess_mime(path.suffix)
        t = await assistant.ingest_audio(audio, path.name, mime)
        print(json.dumps(t.model_dump(mode="json"), indent=2, default=str))
        repo.close()

    asyncio.run(_run())


@app.command(name="tasks")
def list_tasks() -> None:
    """List all stored tasks with local time."""
    repo = get_repository()
    tasks = repo.list_tasks()
    if not tasks:
        print("(no tasks)")
        return
    print("\n" + "=" * 80)
    for t in tasks:
        print(f"\n  [{t.status.value:>11}] {t.title}")
        print(f"    ID: {t.id[:8]}...  Priority: {t.priority}/100  Complexity: {t.complexity}/100")
        if t.deadline:
            print(f"    Deadline: {_fmt_local(t.deadline)}")
        if t.scheduled_start:
            print(f"    Scheduled: {_fmt_local(t.scheduled_start)} → {_fmt_local(t.scheduled_end)}")
    print("\n" + "=" * 80)


@app.command()
def mood() -> None:
    """Print the latest mood snapshot."""
    repo = get_repository()
    snap = repo.latest_mood()
    if not snap:
        print("(no mood data)")
        return
    score, label, reasons = snap
    print(f"\nScore: {score}/100  Label: {label}")
    for r in reasons:
        print(f"  - {r}")
    print()


@app.command()
def energy() -> None:
    """Print the latest energy snapshot (v2.1)."""
    repo = get_repository()
    snap = repo.latest_energy()
    if not snap:
        print("(no energy data)")
        return
    print(f"\nEnergy: {snap.score}/100  Label: {snap.label.value}")
    for r in snap.causes:
        print(f"  - {r}")
    if snap.recommendations:
        print("Recommendations:")
        for r in snap.recommendations:
            print(f"  → {r}")
    print()


@app.command()
def audit(limit: int = typer.Option(20, help="Number of logs to show")) -> None:
    """Show recent audit logs (v2.1)."""
    repo = get_repository()
    logs = repo.list_audit_logs(limit=limit)
    if not logs:
        print("(no audit logs)")
        return
    print("\n" + "=" * 80)
    for log in logs:
        print(f"  [{log['timestamp']}] {log['event_type']}")
        print(f"    Actor: {log['actor']}  Action: {log['action']}  Target: {log.get('target', '-')}")
    print("=" * 80 + "\n")


@app.command()
def router_logs(limit: int = typer.Option(20, help="Number of logs to show")) -> None:
    """Show recent router decision logs (v2.1)."""
    repo = get_repository()
    logs = repo.list_router_logs(limit=limit)
    if not logs:
        print("(no router logs)")
        return
    print("\n" + "=" * 80)
    for log in logs:
        status = "✓" if log["success"] else "✗"
        esc = " [ESCALATED]" if log["escalated"] else ""
        print(f"  {status} [{log['timestamp']}] model={log['model_selected']}")
        print(f"    complexity={log['complexity_score']} risk={log['risk_score']} "
              f"confidence={log['confidence']} latency={log['latency_ms']}ms{esc}")
    print("=" * 80 + "\n")


@app.command()
def conflicts() -> None:
    """Show recent conflict events (Archy vs agents)."""
    repo = get_repository()
    cs = repo.list_conflicts(limit=20)
    if not cs:
        print("(no conflicts)")
        return
    for c in cs:
        print(f"  [{c['resolution']:>10}] {c['agent_name']} / {c['proposal_type']}  "
              f"rounds={c['rounds']}  outcome: {c['final_outcome']}")


@app.command()
def drift(
    kind: str = typer.Option("manual"),
    minutes: int = typer.Option(60),
    description: str = typer.Option(""),
    task_id: str = typer.Option(""),
) -> None:
    """Inject a drift event."""
    async def _run():
        s = get_settings()
        repo = get_repository(s)
        gemini = get_gemini_client(s)
        assistant = Archy(s, repo, gemini)
        event = DriftEvent(
            kind=kind,  # type: ignore[arg-type]
            description=description or f"Drift injected via CLI ({kind})",
            minutes_lost=minutes,
            task_id=task_id or None,
        )
        result = await assistant.report_drift(event)
        print(json.dumps(result, indent=2, default=str))
        repo.close()

    asyncio.run(_run())


@app.command()
def auth(port: int = typer.Option(8765, help="Local port for OAuth callback")) -> None:
    """Run the Google OAuth consent flow.

    Opens a browser, asks for Calendar + Docs permissions, stores tokens
    at ~/.archy/google_tokens.json.

    Before running this, you need GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
    set in .env. See README for how to create them in Google Cloud Console.
    """
    from .integrations.auth import GoogleAuth

    settings = get_settings()
    auth = GoogleAuth(settings)
    if not auth.is_configured():
        typer.echo(
            "ERROR: Google OAuth not configured.\n"
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env.\n"
            "See README for setup instructions.",
            err=True,
        )
        raise typer.Exit(1)
    if auth.is_authenticated():
        typer.echo("Already authenticated. To re-auth, delete "
                   f"{settings.google_token_path} first.")
        return
    typer.echo("Starting OAuth flow. A browser will open...")
    auth.run_flow(port=port)
    typer.echo("Authentication successful. Calendar + Docs integration ready.")


@app.command()
def sync_calendar() -> None:
    """Manually push all scheduled tasks to Google Calendar."""
    async def _run():
        s = get_settings()
        if not s.calendar_sync_enabled:
            typer.echo("Calendar sync is disabled. Set ARCHY_CALENDAR_SYNC_ENABLED=true in .env")
            return
        repo = get_repository(s)
        gemini = get_gemini_client(s)
        assistant = Archy(s, repo, gemini)
        # Trigger a replan, which will fire plan.ready → Calendar subscriber
        result = await assistant.replan()
        print(f"Synced {len(result['slots'])} slots to Calendar.")
        repo.close()

    asyncio.run(_run())


@app.command(name="daily-brief")
def daily_brief() -> None:
    """Generate a daily brief as a Google Doc."""
    async def _run():
        s = get_settings()
        repo = get_repository(s)
        gemini = get_gemini_client(s)
        assistant = Archy(s, repo, gemini)
        result = await assistant.generate_daily_brief()
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            print(f"\nDaily brief created!")
            print(f"Title: {result['title']}")
            print(f"URL: {result['doc_url']}\n")
        repo.close()

    asyncio.run(_run())


def _guess_mime(suffix: str) -> str:
    s = suffix.lower().lstrip(".")
    return {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "ogg": "audio/ogg",
        "flac": "audio/flac",
    }.get(s, "application/octet-stream")


if __name__ == "__main__":
    app()
