"""Single-worker run loop. Default execution is paper; live must be explicit."""

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from .config import D, Settings


def _acquire_worker_lock(path):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0,  # No sharing: only one worker may own this file.
            None,
            4,  # OPEN_ALWAYS
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if error in (32, 33):  # ERROR_SHARING_VIOLATION/LOCK_VIOLATION
                raise BlockingIOError(error, "Worker lock is already held")
            raise ctypes.WinError(error)
        return handle

    import fcntl

    lock = path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        lock.close()
        raise
    return lock


def _release_worker_lock(lock):
    if os.name == "nt":
        import ctypes

        ctypes.WinDLL("kernel32").CloseHandle(lock)
    else:
        lock.close()


def main():
    p = argparse.ArgumentParser(prog="stonkfly")
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--reuse-doomfly", type=Path)
    sub.add_parser("verify")
    run = sub.add_parser("run")
    run.add_argument("--live", action="store_true")
    run.add_argument(
        "--preflight-only",
        action="store_true",
        help="Read-only exchange checks; never submit an order",
    )
    run.add_argument(
        "--resume-reviewed",
        action="store_true",
        help="After manual review, clear a transient halt only after successful reconciliation",
    )
    run.add_argument(
        "--fixture",
        action="store_true",
        help="Synthetic offline market input; paper only",
    )
    run.add_argument("--steps", type=int, default=0, help="0 keeps running")
    run.add_argument(
        "--fast",
        action="store_true",
        help="Skip waiting in paper mode; execution cooldown still applies",
    )
    run.add_argument(
        "--frozen",
        action="store_true",
        help="Freeze all memory efficacies for a control run",
    )
    run.add_argument("--out", type=Path)
    run.add_argument(
        "--products",
        nargs="+",
        default=["BTC-USDC"],
        choices=["BTC-USDC", "ETH-USDC", "SOL-USDC"],
    )
    run.add_argument("--neural-ms", type=float, default=500)
    status = sub.add_parser("status")
    status.add_argument("--out", type=Path, default=Path("runs/paper"))
    a = p.parse_args()
    from dotenv import load_dotenv

    # Never search parent projects for unrelated account credentials.
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    if a.command in ("prepare", "verify"):
        from .data import prepare, verify

        if a.command == "prepare":
            prepare(a.reuse_doomfly)
        else:
            print(json.dumps(verify()))
        return
    if a.command == "status":
        import sqlite3

        db = sqlite3.connect(f"file:{a.out / 'ledger.sqlite'}?mode=ro", uri=True)
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
        print(
            json.dumps(
                {
                    k: meta.get(k)
                    for k in [
                        "mode",
                        "tick",
                        "cash",
                        "positions",
                        "initial_cash",
                        "anchor",
                        "halted",
                    ]
                },
                indent=2,
            )
        )
        return
    if a.live and (a.fixture or a.fast):
        p.error("Live mode forbids fixtures and fast replay")
    if a.steps < 0:
        p.error("steps cannot be negative")
    settings = Settings(
        products=tuple(a.products),
        learning=not a.frozen,
        neural_ms=a.neural_ms,
        pulse_ms=min(200, a.neural_ms),
    )
    out = a.out or Path("runs/live" if a.live else "runs/paper")
    out.mkdir(parents=True, exist_ok=True)
    try:
        lock = _acquire_worker_lock(out / "worker.lock")
    except BlockingIOError:
        raise SystemExit("A worker already owns this run directory")
    from .broker import CoinbaseBroker, PaperBroker
    from .ledger import Ledger

    ledger = Ledger(out / "ledger.sqlite", settings, "live" if a.live else "paper")
    try:
        broker = (
            CoinbaseBroker.from_env(settings, ledger)
            if a.live
            else PaperBroker(settings, ledger)
        )
        result = broker.preflight()
        print(json.dumps(result), flush=True)
        if a.resume_reviewed:
            if (out / "STOP").exists() or ledger.pending():
                raise RuntimeError(
                    "Remove STOP only after review; unresolved orders cannot resume"
                )
            reason = ledger.get("halted")
            if reason and ("Loss stop" in reason or "fee exceeded" in reason):
                raise RuntimeError("A financial stop cannot be cleared by this flag")
            ledger.put("halted", None)
        if a.preflight_only:
            return
        from .data import verify

        verified = verify()
        from PIL import Image

        from .actions import StonkflyActions
        from .display import market_frame
        from .market import CoinbaseMarket, FixtureMarket
        from .neural.controller import FlyController
        from .reinforcement import reinforcement
        from .risk import Guard, Veto

        market = (
            FixtureMarket(settings.products)
            if a.fixture
            else CoinbaseMarket(settings.products)
        )
        previous = ledger.get("observation")
        if previous:
            market.history = previous["market_history"]
            if a.fixture:
                market.tick = previous["fixture_tick"]
        controller = FlyController(settings)
        cp = ledger.get("checkpoint")
        if cp:
            path = out / cp["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != cp["sha256"]:
                raise RuntimeError("Checkpoint integrity mismatch")
            controller.restore(path)
        provenance = {
            "settings": dataclasses.asdict(settings),
            "dataset": verified,
            "circuit": controller.brain.circuit["report"],
            "vision": controller.brain.visual_report,
            "mode": broker.mode,
            "feed": "fixture" if a.fixture else "coinbase-public",
            "decoder": "DNp20 mean R-L: buy/sell; DNpe017 spike gate; otherwise hold. Engineered fixed mapping.",
            "learning_validated": False,
            "pain_receptors_modeled": False,
            "timing": "Each observation advances configured neural_ms regardless of wall-market time; no claim of real-time fly physiology.",
            "source_sha256": {
                str(path.relative_to(Path(__file__).parent)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in Path(__file__).parent.rglob("*")
                if path.suffix in (".py", ".cpp")
            },
        }
        signature = hashlib.sha256(
            json.dumps(provenance, sort_keys=True).encode()
        ).hexdigest()
        if ledger.get("provenance_sha256") not in (None, signature):
            raise RuntimeError(
                "Run source/protocol changed; use a separate paper run or explicitly review migration"
            )
        ledger.put("provenance_sha256", signature)
        (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        guard = Guard(settings, ledger, out / "STOP")
        provider = StonkflyActions(guard, broker)
        action = provider.get_actions()[0]
        count = 0
        while not a.steps or count < a.steps:
            started = time.monotonic()
            stop_requested = (out / "STOP").exists()
            halted = ledger.get("halted")
            if stop_requested or halted:
                reason = "STOP file exists" if stop_requested else f"run is halted: {halted}"
                print(f"Run stopped before tick: {reason}", file=sys.stderr, flush=True)
                break
            broker.reconcile()
            broker.verify_balances()
            quotes = market.snapshot()
            guard.check(quotes, time.time())
            market.record(quotes)
            product = settings.products[ledger.get("tick") % len(settings.products)]
            q = quotes[product]
            equity = ledger.equity(quotes)
            kind, delta = reinforcement(
                equity, ledger.get("anchor"), settings.reward_deadband
            )
            frame = market_frame(product, market.history[product], q.bid, q.ask)
            neural = controller.observe(frame, kind)
            # Checkpoint + accounting anchor are committed before any trade.
            # Two slots keep the last committed snapshot safe during a crash.
            slot = ledger.get("tick") % 2
            checkpoint = out / f"brain-{slot}.npz"
            controller.save(checkpoint)
            checkpoint_info = {
                "file": checkpoint.name,
                "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            }
            observation = {
                "neural": neural,
                "product": product,
                "quote": q.json(),
                "pnl_delta_usdc": str(delta),
                "market_history": market.history,
                "fixture_tick": getattr(market, "tick", None),
            }
            ledger.commit_tick(equity, checkpoint_info, observation)
            order = {"status": "HOLD"}
            if neural["side"] != "HOLD":
                try:
                    # Neural integration can be slow; use a fresh execution book.
                    fresh = market.snapshot()
                    latest = fresh[product]
                    if abs(latest.bid - q.bid) / q.bid > D(settings.slippage):
                        raise Veto("Price moved beyond neural observation tolerance")
                    provider.quotes = fresh
                    order = action.invoke({"product": product, "side": neural["side"]})
                except Veto as e:
                    order = {"status": "VETO", "reason": str(e)}
            row = {
                "tick": ledger.get("tick"),
                "wall_time": time.time(),
                "product": product,
                "mode": broker.mode,
                "quote": q.json(),
                "equity_usdc": str(equity),
                "pnl_delta_usdc": str(delta),
                "neural": neural,
                "execution": order,
            }
            with (out / "events.jsonl").open("a") as f:
                f.write(json.dumps(row, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            Image.fromarray(frame).save(out / "latest-input.png")
            (out / "latest.json").write_text(json.dumps(row, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "tick": row["tick"],
                        "side": neural["side"],
                        "execution": order["status"],
                        "equity": str(equity),
                        "stimulus": kind,
                        "plastic_edges_changed": neural["memory"]["changed_edges"],
                    }
                ),
                flush=True,
            )
            count += 1
            if not a.fast and (not a.steps or count < a.steps):
                until = started + settings.interval_seconds
                while time.monotonic() < until and not (out / "STOP").exists():
                    time.sleep(min(1, until - time.monotonic()))
    except KeyboardInterrupt:
        print("Stopped; run state preserved.", flush=True)
    except Exception as e:
        # Never print SDK exception text: it may contain account/request details.
        if not ledger.get("halted"):
            ledger.halt(type(e).__name__)
        frames = traceback.extract_tb(e.__traceback__)
        origin = frames[-1] if frames else None
        internal = origin and Path(origin.filename).is_relative_to(
            Path(__file__).parent
        )
        diagnostic = {
            "type": type(e).__name__,
            "reason": str(e)
            if internal
            else "External dependency error; review connection and account state.",
            "locations": [
                f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames
            ],
        }
        (out / "error.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        print(
            f"Stopped safely: {type(e).__name__}. Inspect local state and reconcile before restarting.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    finally:
        ledger.close()
        _release_worker_lock(lock)


if __name__ == "__main__":
    main()
