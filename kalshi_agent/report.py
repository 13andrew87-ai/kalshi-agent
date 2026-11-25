"""Status reporting: terminal text, Markdown, and a self-contained HTML dashboard.

The HTML uses inline CSS and inline SVG only (no external resources), so it
renders inside sandboxed previews and offline.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig
from .portfolio import Portfolio


def _esc(s: Any) -> str:
    return html.escape(str(s))


def sparkline_svg(values: list[float], width: int = 640, height: int = 120) -> str:
    if len(values) < 2:
        values = (values * 2) or [0, 0]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(values):
        x = 8 + (width - 16) * i / (len(values) - 1)
        y = height - 10 - (height - 20) * (v - lo) / span
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0f172a" rx="8"/>'
        f'<polyline points="{poly}" fill="none" stroke="#38bdf8" stroke-width="2"/>'
        f'<text x="10" y="16" fill="#94a3b8" font-size="11" font-family="monospace">'
        f'equity {values[0]:.0f}c &rarr; {values[-1]:.0f}c</text></svg>'
    )


def build_markdown(cfg: AppConfig, portfolio: Portfolio, risk_summary: dict[str, Any],
                   title: str = "Kalshi Agent Status") -> str:
    snap = portfolio.snapshot({})
    wins, total = portfolio.win_rate()
    lines = [
        f"# {title}",
        f"_generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_",
        "",
        f"- mode: **{cfg.mode}**  (demo env: {cfg.use_demo_env})",
        f"- cash: **{snap['cash_cents'] / 100:,.2f} USD**",
        f"- equity: **{snap['equity_cents'] / 100:,.2f} USD** ({snap['return_pct']:+.2f}%)",
        f"- open positions: {len(portfolio.positions)}  | closed trades: {total} | win rate: {wins}/{total or 1}",
        f"- risk: halted={risk_summary.get('halted')} consecutive_losses={risk_summary.get('consecutive_losses')}",
        "",
        "## Open positions",
        "",
        "| ticker | yes | no | avg yes | avg no |",
        "|---|---|---|---|---|",
    ]
    for t, p in sorted(portfolio.positions.items()):
        lines.append(f"| {t} | {p.yes_count} | {p.no_count} | {p.avg_yes_price_cents:.1f}c | {p.avg_no_price_cents:.1f}c |")
    if not portfolio.positions:
        lines.append("| _(none)_ | | | | |")
    lines += ["", "## Recent closed trades", "",
              "| ticker | side | n | entry | exit | pnl | why |", "|---|---|---|---|---|---|---|"]
    for t in portfolio.closed[-15:]:
        lines.append(f"| {t.ticker} | {t.side} | {t.count} | {t.entry_price_cents:.1f}c | "
                     f"{t.exit_price_cents:.1f}c | {t.pnl_cents / 100:+.2f} USD | {t.why} |")
    return "\n".join(lines) + "\n"


def build_html(cfg: AppConfig, portfolio: Portfolio, risk_summary: dict[str, Any],
               title: str = "Kalshi Agent", extra_panels: str = "") -> str:
    snap = portfolio.snapshot({})
    wins, total = portfolio.win_rate()
    curve = [c for _, c in portfolio.equity_history] or [snap["equity_cents"]]
    svg = sparkline_svg(curve[-400:])
    color = "#22c55e" if snap["return_pct"] >= 0 else "#ef4444"

    pos_rows = "".join(
        f"<tr><td>{_esc(t)}</td><td>{p.yes_count}</td><td>{p.no_count}</td>"
        f"<td>{p.avg_yes_price_cents:.1f}c</td><td>{p.avg_no_price_cents:.1f}c</td>"
        f"<td>{p.basis_cents() / 100:.2f}$</td></tr>"
        for t, p in sorted(portfolio.positions.items())
    ) or "<tr><td colspan=6><i>none</i></td></tr>"

    closed_rows = "".join(
        f"<tr><td>{_esc(t.ticker)}</td><td>{_esc(t.side)}</td><td>{t.count}</td>"
        f"<td>{t.entry_price_cents:.1f}c</td><td>{t.exit_price_cents:.1f}c</td>"
        f"<td style='color:{'#22c55e' if t.pnl_cents >= 0 else '#ef4444'}'>{t.pnl_cents / 100:+.2f}$</td>"
        f"<td>{_esc(t.why or t.kind)}</td></tr>"
        for t in reversed(portfolio.closed[-20:])
    ) or "<tr><td colspan=7><i>none yet</i></td></tr>"

    risk_rows = "".join(f"<tr><td>{_esc(k)}</td><td><b>{_esc(v)}</b></td></tr>"
                        for k, v in risk_summary.items())

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_esc(title)}</title></head>
<body style="margin:0;background:#0b1220;color:#e2e8f0;font-family:ui-monospace,Menlo,Consolas,monospace;">
<div style="max-width:900px;margin:0 auto;padding:24px;">
<h1 style="font-size:22px;margin:0 0 4px;">{_esc(title)}</h1>
<div style="color:#64748b;font-size:12px;margin-bottom:18px;">
mode: {cfg.mode} | demo env: {cfg.use_demo_env} | kelly {cfg.kelly_fraction} |
min edge {cfg.min_edge_cents}c | generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC</div>

<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;">
  <div style="flex:1;min-width:180px;background:#101a2e;border:1px solid #1e293b;border-radius:10px;padding:14px;">
    <div style="color:#64748b;font-size:11px;">EQUITY</div>
    <div style="font-size:24px;">{snap['equity_cents'] / 100:,.2f}$</div>
    <div style="font-size:14px;color:{color};">{snap['return_pct']:+.2f}%</div></div>
  <div style="flex:1;min-width:180px;background:#101a2e;border:1px solid #1e293b;border-radius:10px;padding:14px;">
    <div style="color:#64748b;font-size:11px;">CASH</div>
    <div style="font-size:24px;">{snap['cash_cents'] / 100:,.2f}$</div>
    <div style="font-size:14px;color:#64748b;">reserve {cfg.min_cash_reserve_pct:.0f}%</div></div>
  <div style="flex:1;min-width:180px;background:#101a2e;border:1px solid #1e293b;border-radius:10px;padding:14px;">
    <div style="color:#64748b;font-size:11px;">CLOSED TRADES</div>
    <div style="font-size:24px;">{total}</div>
    <div style="font-size:14px;color:#64748b;">win rate {wins}/{total or 1}</div></div>
  <div style="flex:1;min-width:180px;background:#101a2e;border:1px solid #1e293b;border-radius:10px;padding:14px;">
    <div style="color:#64748b;font-size:11px;">RISK ENGINE</div>
    <div style="font-size:16px;color:{'#ef4444' if risk_summary.get('halted') else '#22c55e'};">
      {'HALTED: ' + _esc(risk_summary.get('halt_reason', '')) if risk_summary.get('halted') else 'OK'}</div>
    <div style="font-size:12px;color:#64748b;">loss streak {risk_summary.get('consecutive_losses')}/{cfg.consecutive_loss_kill}</div></div>
</div>

<div style="margin-bottom:16px;">{svg}</div>

<div style="background:#101a2e;border:1px solid #1e293b;border-radius:10px;padding:14px;margin-bottom:16px;">
<div style="color:#64748b;font-size:11px;margin-bottom:8px;">OPEN POSITIONS ({len(portfolio.positions)})</div>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr style="color:#64748b;text-align:left;"><th>ticker</th><th>yes</th><th>no</th><th>avg yes</th><th>avg no</th><th>basis</th></tr>
{pos_rows}</table></div>

<div style="background:#101a2e;border:1px solid #1e293b;border-radius:10px;padding:14px;margin-bottom:16px;">
<div style="color:#64748b;font-size:11px;margin-bottom:8px;">RECENT CLOSED TRADES</div>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<tr style="color:#64748b;text-align:left;"><th>ticker</th><th>side</th><th>n</th><th>entry</th><th>exit</th><th>pnl</th><th>why</th></tr>
{closed_rows}</table></div>

<div style="background:#101a2e;border:1px solid #1e293b;border-radius:10px;padding:14px;margin-bottom:16px;">
<div style="color:#64748b;font-size:11px;margin-bottom:8px;">RISK STATE</div>
<table style="width:100%;border-collapse:collapse;font-size:13px;">{risk_rows}</table></div>

{extra_panels}

<div style="color:#475569;font-size:11px;margin-top:20px;">
Not financial advice.  Simulated results do not guarantee real-world performance.
Trade at your own risk; start in paper mode.</div>
</div></body></html>"""


def write_reports(cfg: AppConfig, portfolio: Portfolio, risk_summary: dict[str, Any],
                  title: str, extra_panels: str = "") -> tuple[str, str]:
    os.makedirs(cfg.reports_dir, exist_ok=True)
    md = build_markdown(cfg, portfolio, risk_summary, title=title)
    htm = build_html(cfg, portfolio, risk_summary, title=title, extra_panels=extra_panels)
    md_path = os.path.join(cfg.reports_dir, "status.md")
    htm_path = os.path.join(cfg.reports_dir, "status.html")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(htm_path, "w", encoding="utf-8") as fh:
        fh.write(htm)
    return md_path, htm_path


def dump_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
