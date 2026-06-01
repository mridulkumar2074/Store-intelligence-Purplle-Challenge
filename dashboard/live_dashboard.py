"""
Live terminal dashboard — polls the Intelligence API every 5 s
and renders a Rich table showing real-time store metrics.

Usage:
    python dashboard/live_dashboard.py [--api-url http://localhost:8000] [--store STORE_BLR_002]
"""
from __future__ import annotations
import argparse
import sys
import time
from datetime import datetime, timezone

try:
    import httpx
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich import box
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

POLL_INTERVAL = 5  # seconds


def _make_metrics_panel(metrics: dict) -> Panel:
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column("Metric",   style="bold cyan",  no_wrap=True)
    t.add_column("Value",    style="bold white",  no_wrap=True)

    cr   = metrics.get("conversion_rate", 0)
    abr  = metrics.get("abandonment_rate", 0)
    vis  = metrics.get("unique_visitors", 0)
    q    = metrics.get("current_queue_depth", 0)
    rev  = metrics.get("total_revenue_inr", 0)
    txns = metrics.get("total_transactions", 0)
    dw   = metrics.get("avg_dwell_ms", 0)

    cr_color  = "green" if cr >= 0.25 else ("yellow" if cr >= 0.15 else "red")
    q_color   = "red" if q >= 8 else ("yellow" if q >= 5 else "green")

    t.add_row("Unique Visitors",       f"[bold white]{vis}[/]")
    t.add_row("Conversion Rate",       f"[{cr_color}]{cr:.1%}[/]")
    t.add_row("Total Transactions",    f"[bold white]{txns}[/]")
    t.add_row("Revenue (INR)",         f"[bold green]₹{rev:,.0f}[/]")
    t.add_row("Avg Dwell",             f"[white]{dw/1000:.0f}s[/]")
    t.add_row("Queue Depth",           f"[{q_color}]{q}[/]")
    t.add_row("Abandonment Rate",      f"[{'red' if abr > 0.3 else 'yellow' if abr > 0.1 else 'green'}]{abr:.1%}[/]")

    return Panel(t, title="[bold blue]Store Metrics[/]", border_style="blue")


def _make_funnel_panel(funnel: dict) -> Panel:
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Stage",         style="cyan")
    t.add_column("Count",         style="bold white", justify="right")
    t.add_column("Drop-off",      style="red",        justify="right")

    for stage in funnel.get("stages", []):
        drop = stage.get("drop_off_pct", 0)
        bar  = "▓" * int(stage.get("count", 0) / max(
            funnel["stages"][0]["count"] if funnel.get("stages") else 1, 1
        ) * 20)
        t.add_row(
            stage.get("stage", ""),
            f"{stage.get('count', 0)}  {bar}",
            f"{drop:.1f}%" if drop > 0 else "—",
        )

    cr = funnel.get("conversion_rate", 0)
    return Panel(
        t,
        title=f"[bold blue]Funnel  [green]CR={cr:.1%}[/][/]",
        border_style="blue",
    )


def _make_heatmap_panel(heatmap: dict) -> Panel:
    zones = sorted(heatmap.get("zones", []), key=lambda z: -z.get("normalised_score", 0))[:8]
    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Zone",       style="cyan",   no_wrap=True)
    t.add_column("Visits",     justify="right")
    t.add_column("Avg Dwell",  justify="right")
    t.add_column("Heat",       no_wrap=True)

    for z in zones:
        score = z.get("normalised_score", 0)
        bar = "█" * int(score / 10) + "░" * (10 - int(score / 10))
        color = "red" if score >= 70 else "yellow" if score >= 40 else "white"
        t.add_row(
            z.get("zone_id", ""),
            str(z.get("visit_frequency", 0)),
            f"{z.get('avg_dwell_ms', 0)/1000:.0f}s",
            f"[{color}]{bar}[/]",
        )

    return Panel(t, title="[bold blue]Zone Heatmap[/]", border_style="blue")


def _make_anomalies_panel(anomalies: dict) -> Panel:
    items = anomalies.get("active_anomalies", [])
    if not items:
        return Panel(
            "[green]No active anomalies[/]",
            title="[bold blue]Anomalies[/]",
            border_style="blue",
        )

    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("Type",     style="bold", no_wrap=True)
    t.add_column("Severity", no_wrap=True)
    t.add_column("Description")

    for a in items:
        sev = a.get("severity", "INFO")
        sev_color = "red" if sev == "CRITICAL" else "yellow" if sev == "WARN" else "blue"
        t.add_row(
            a.get("anomaly_type", ""),
            f"[{sev_color}]{sev}[/]",
            a.get("description", "")[:60],
        )

    return Panel(t, title="[bold blue]Active Anomalies[/]", border_style="blue")


def _build_layout(metrics, funnel, heatmap, anomalies, store_id, last_update, error=None) -> Table:
    root = Table.grid(expand=True)
    root.add_column()

    # Header
    ts_str = last_update.strftime("%H:%M:%S")
    header = Text(
        f" Purplle Store Intelligence  |  {store_id}  |  {ts_str} ",
        style="bold white on dark_blue",
        justify="center",
    )
    root.add_row(header)

    if error:
        root.add_row(f"[bold red]API Error: {error}[/]")
        return root

    row1 = Table.grid(expand=True)
    row1.add_column(ratio=1)
    row1.add_column(ratio=1)
    row1.add_row(
        _make_metrics_panel(metrics or {}),
        _make_funnel_panel(funnel or {}),
    )
    root.add_row(row1)

    row2 = Table.grid(expand=True)
    row2.add_column(ratio=3)
    row2.add_column(ratio=2)
    row2.add_row(
        _make_heatmap_panel(heatmap or {}),
        _make_anomalies_panel(anomalies or {}),
    )
    root.add_row(row2)
    root.add_row(f"[dim]Refreshing every {POLL_INTERVAL}s — Ctrl+C to exit[/]")
    return root


def run_dashboard(api_url: str, store_id: str):
    if not _RICH_AVAILABLE:
        print("Install rich and httpx: pip install rich httpx")
        sys.exit(1)

    console = Console()
    console.clear()

    metrics = funnel = heatmap = anomalies = None
    last_update = datetime.now(timezone.utc)
    error = None

    with httpx.Client(base_url=api_url, timeout=8.0) as client:
        with Live(console=console, refresh_per_second=0.5) as live:
            while True:
                try:
                    metrics   = client.get(f"/stores/{store_id}/metrics").json()
                    funnel    = client.get(f"/stores/{store_id}/funnel").json()
                    heatmap   = client.get(f"/stores/{store_id}/heatmap").json()
                    anomalies = client.get(f"/stores/{store_id}/anomalies").json()
                    last_update = datetime.now(timezone.utc)
                    error = None
                except Exception as exc:
                    error = str(exc)[:80]

                layout = _build_layout(
                    metrics, funnel, heatmap, anomalies,
                    store_id, last_update, error
                )
                live.update(layout)
                time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Purplle Store Intelligence — Live Dashboard")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--store",   default="STORE_BLR_002")
    args = parser.parse_args()
    run_dashboard(args.api_url, args.store)


if __name__ == "__main__":
    main()
