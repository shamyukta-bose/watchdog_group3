"""Top-level orchestrator: scrape -> forecast -> calculate -> report."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import forecasting, rop_calculator, endgame, reporter, scraper

log = logging.getLogger("main")


def _load_config(path):
    cfg_path = Path(path)
    if not cfg_path.exists() and os.environ.get("SC_TEAM_ID"):
        cfg = {
            "game": {"base_url": os.environ.get("SC_BASE_URL", "https://op.responsive.net/SupplyChain"), "team_id": "", "password": "", "institution": os.environ.get("SC_INSTITUTION", "")},
            "schedule": {"scrape_interval_minutes": 30, "report_min_interval_minutes": 60},
            "policy": {"service_level": 0.95, "production_lead_time_days": 5, "shipping_lead_time_truck_days": 7, "shipping_lead_time_mail_days": 1, "truck_capacity_drums": 200, "truck_fixed_cost": 15000, "mail_cost_per_drum": 150, "batch_setup_cost": 1500, "batch_per_drum_cost": 1000, "capacity_cost_per_drum_per_day": 50000, "capacity_lead_time_days": 90, "holding_cost_per_drum_per_year": 100, "price_per_drum": 1450, "fulfillment_cost_per_drum": 150, "discount_rate_annual": 0.10, "end_day": 1460, "assumed_current_day": 731},
            "email": {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_use_ssl": True, "smtp_user": "", "smtp_password": "", "from_address": "", "recipients": [], "subject_prefix": "[Group3 Watchdog]"},
            "files": {"demand_history_csv": "data/demand_history.csv", "output_dir": "outputs", "log_dir": "logs"},
        }
    else:
        with open(cfg_path, "r") as f:
            cfg = json.load(f)
    if os.environ.get("SC_TEAM_ID"): cfg["game"]["team_id"] = os.environ["SC_TEAM_ID"]
    if os.environ.get("SC_PASSWORD"): cfg["game"]["password"] = os.environ["SC_PASSWORD"]
    if os.environ.get("SC_INSTITUTION"): cfg["game"]["institution"] = os.environ["SC_INSTITUTION"]
    if os.environ.get("SMTP_HOST"): cfg["email"]["smtp_host"] = os.environ["SMTP_HOST"]
    if os.environ.get("SMTP_PORT"): cfg["email"]["smtp_port"] = int(os.environ["SMTP_PORT"])
    if os.environ.get("SMTP_USER"): cfg["email"]["smtp_user"] = os.environ["SMTP_USER"]
    if os.environ.get("SMTP_PASSWORD"): cfg["email"]["smtp_password"] = os.environ["SMTP_PASSWORD"]
    if os.environ.get("SMTP_FROM"): cfg["email"]["from_address"] = os.environ["SMTP_FROM"]
    if os.environ.get("EMAIL_RECIPIENTS"): cfg["email"]["recipients"] = [r.strip() for r in os.environ["EMAIL_RECIPIENTS"].split(",") if r.strip()]
    if os.environ.get("EMAIL_ENABLED"): cfg["email"]["enabled"] = os.environ["EMAIL_ENABLED"].lower() in ("1", "true", "yes")
    return cfg


def _setup_logging(log_dir):
    log_dir = Path(log_dir); log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"watchdog_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)])


def run_once(cfg, *, dry_run=False, no_scrape=False):
    pol = cfg["policy"]; files = cfg["files"]; snap = None
    if no_scrape:
        snap = scraper.Snapshot(timestamp=datetime.now().isoformat(timespec="seconds"))
        snap.current_day = pol.get("assumed_current_day", 731); snap.warehouse_inventory = 200
        snap.factory_wip = 50; snap.factory_capacity = 20; snap.in_transit = 0
    else:
        try:
            gs = scraper.GameScraper(cfg); gs.login(); snap = gs.scrape()
            ts_label = datetime.now().strftime("%Y%m%d_%H%M%S")
            snap_dir = Path(files["output_dir"]) / "raw"
            scraper.save_snapshot_json(snap, snap_dir / f"snapshot_{ts_label}.json")
            scraper.save_snapshot_csvs(snap, snap_dir, ts_label)
        except Exception as e:
            log.exception("Scrape failed: %s", e); return {"ok": False, "reason": f"scrape_failed: {e}"}
    current_day = int(snap.current_day or pol.get("assumed_current_day", 731))
    if snap.demand_series:
        ld, lq = snap.demand_series[-1]
        forecasting.append_observation(files["demand_history_csv"], int(ld), float(lq))
    fc = forecasting.build_forecast(history_csv=files["demand_history_csv"], horizon_start_day=current_day, horizon_end_day=pol["end_day"])
    lead_time = pol["production_lead_time_days"] + pol["shipping_lead_time_truck_days"]
    days_remaining = pol["end_day"] - current_day
    policy_rec = rop_calculator.recommend_policy(forecast=fc, current_day=current_day, lead_time_days=lead_time, service_level=pol["service_level"], current_capacity=float(snap.factory_capacity or 20), days_remaining=days_remaining, policy_cfg=pol)
    pipeline = float((snap.factory_wip or 0) + (snap.in_transit or 0))
    fwd_30 = fc.window(current_day, min(current_day + 30, pol["end_day"]))
    fwd_avg = float(fwd_30.mean.mean()) if len(fwd_30.mean) else fc.level
    eg = endgame.recommend_endgame(forecast=fc, current_day=current_day, end_day=pol["end_day"], warehouse_inventory=float(snap.warehouse_inventory or 0), pipeline_inventory=pipeline, avg_daily_demand_forward=fwd_avg, production_lead_time=pol["production_lead_time_days"], shipping_lead_time_truck=pol["shipping_lead_time_truck_days"], shipping_lead_time_mail=pol["shipping_lead_time_mail_days"], capacity_lead_time=pol["capacity_lead_time_days"])
    final_rop, final_q, use_truck, eg_note = endgame.merge_with_baseline(eg, policy_rec.reorder_point, policy_rec.batch_size, policy_rec.use_truck)
    ts_label = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(files["output_dir"]) / f"watchdog_report_{ts_label}.xlsx"
    reporter.write_report_xlsx(out_path=out_path, snapshot=snap, forecast=fc, policy=policy_rec, endgame_rec=eg, final_rop=final_rop, final_q=final_q, use_truck=use_truck, extra_notes=[eg_note])
    latest = Path(files["output_dir"]) / "watchdog_report_latest.xlsx"
    try:
        if latest.exists() or latest.is_symlink(): latest.unlink()
        latest.symlink_to(out_path.name)
    except Exception:
        import shutil; shutil.copy2(out_path, latest)
    sent = {"sent": False}
    if not dry_run and cfg.get("email", {}).get("enabled"):
        body = reporter.build_email_body(snap, final_rop, final_q, use_truck, eg, policy_rec, extra_notes=[eg_note])
        subject = f"{cfg['email'].get('subject_prefix', '[Group3]')} day {current_day} | ROP={final_rop:.0f} Q={final_q:.0f} {'Truck' if use_truck else 'Mail'} | {eg.phase.value}"
        try: sent = reporter.send_email(cfg["email"], subject, body, attachment_path=out_path)
        except Exception as e: log.exception("Email send failed: %s", e); sent = {"sent": False, "reason": str(e)}
    return {"ok": True, "day": current_day, "phase": eg.phase.value, "final_rop": final_rop, "final_q": final_q, "use_truck": use_truck, "report": str(out_path), "email": sent}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.json"); p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--no-scrape", action="store_true")
    args = p.parse_args(argv)
    cfg = _load_config(args.config); _setup_logging(cfg["files"]["log_dir"])
    if args.once:
        s = run_once(cfg, dry_run=args.dry_run, no_scrape=args.no_scrape)
        log.info("Run summary: %s", s); return 0 if s.get("ok") else 1
    interval = max(60, int(cfg["schedule"]["scrape_interval_minutes"]) * 60)
    last_report = 0.0; min_email_gap = max(60, int(cfg["schedule"]["report_min_interval_minutes"]) * 60)
    while True:
        try:
            now = time.time(); should_email = (now - last_report) >= min_email_gap
            s = run_once(cfg, dry_run=args.dry_run or not should_email, no_scrape=args.no_scrape)
            log.info("Cycle: %s", s)
            if s.get("email", {}).get("sent"): last_report = now
        except KeyboardInterrupt: return 0
        except Exception as e: log.exception("Cycle failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
