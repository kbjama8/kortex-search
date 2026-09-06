"""Console entry point: `kortex-search serve|doctor|check|version|warm`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import threading
from collections.abc import Sequence

from . import __version__, health
from .config import MCP_HOST, MCP_PORT
from .log import configure_logging


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import main

    def _graceful(_signum, _frame):
        raise KeyboardInterrupt

    # Map SIGTERM (systemd stop) to the same clean unwind FastMCP uses for
    # SIGINT, so the running loop cancels tasks and exits without corruption.
    signal.signal(signal.SIGTERM, _graceful)

    # Self-warm: preload the models in-process off the serving path. After
    # a restart the first search would otherwise pay the full cold-load
    # cost (imports + weights) inside the request budget — with --warm a
    # background thread loads them once the server is up (models run on the
    # shared inference executor, never the event loop).
    if getattr(args, "warm", False):

        def _warm_worker() -> None:
            import asyncio as _asyncio
            import time as _time

            from . import embeddings, rerank
            from .inference import run_inference
            _time.sleep(5)  # let initialize/tools-list settle first

            async def _load() -> None:
                try:
                    await run_inference(rerank._get_model, kind="rerank")
                    await embeddings.encode_async(["warmup"])
                    # v0.9: preload the CJK embed too — a mid-burst
                    # CJK-dominant run must never stall the queue on a
                    # cold bge-m3 load.
                    await run_inference(embeddings._get_cjk_model, kind="embed")
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger("kortex_search.cli").warning(
                        "serve warm-up failed: %s", exc)

            try:
                _asyncio.run(_load())
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("kortex_search.cli").warning(
                    "serve warm-up thread failed: %s", exc)

        threading.Thread(target=_warm_worker, daemon=True,
                         name="ks-warmup").start()

    # `--transport/--host/--port` live on the `serve` subparser, so the bare
    # `kortex-search` command (no subcommand) — which also routes here — does
    # not have them. Fall back to defaults.
    transport = getattr(args, "transport", "stdio")
    if transport in ("http", "sse"):
        main(transport=transport,
             host=getattr(args, "host", MCP_HOST),
             port=getattr(args, "port", MCP_PORT))
    else:
        main()
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    report = asyncio.run(health.report())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("redis", {}).get("ok") else 1


def _cmd_check(_args: argparse.Namespace) -> int:
    ok, report = asyncio.run(health.check())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("llm", {}).get("available"):
        print("warning: DEEPSEEK_API_KEY not set — answer synthesis disabled",
              file=sys.stderr)
    return 0 if ok else 1


def _cmd_version(_args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def _cmd_vault(args: argparse.Namespace) -> int:
    from .extract import vault

    if args.vault_command == "migrate":
        rows = vault.migrate(dry_run=args.dry_run)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0 if all(r["status"] in ("noop", "migrated", "empty")
                        for r in rows) else 1
    # status
    print(json.dumps(vault.status(), ensure_ascii=False, indent=2))
    return 0


def _cmd_harden(args: argparse.Namespace) -> int:
    from .extract import harden

    if args.harden_action == "install":
        out = harden.install(sudo=args.sudo, dry_run=args.dry_run,
                             for_unit=args.for_unit)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    if args.harden_action == "uninstall":
        out = harden.uninstall()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    if args.harden_action == "mark-installed":
        # receipt for a successful privileged load (the unprivileged gateway
        # cannot probe the kernel table — the loader's success is the proof)
        out = harden.mark_installed()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    st = harden.status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    if args.harden_action == "check":
        # `check` reports enforceability (exit 0 even when not enforced —
        # the unit logs the report; in-process enforce() is the real gate)
        enforceable = st["installed"] and st["systemd_run"]
        detail = "; ".join(st["problems"]) or "permissive"
        print(f"browser-tier enforceability: "
              f"{'yes' if enforceable else 'no — ' + detail}")
        return 0
    return 0 if st["installed"] else 1


def _cmd_warm(_args: argparse.Namespace) -> int:
    from . import embeddings, rerank

    rerank._get_model()          # same-package preload (private, intentional)
    embeddings._get_model()      # same-package preload (private, intentional)
    print(json.dumps({"rerank": rerank.status(), "embed": embeddings.status()},
                     ensure_ascii=False, indent=2))
    return 0


def _cmd_farm(args: argparse.Namespace) -> int:
    """Managed profile farm: status | login <platform> [--headed] | reap."""
    from .extract import browserfarm
    from .extract.profiles import default_profile, store

    if args.farm_command == "status":
        profs = store.all() or [default_profile(p) for p in ("reddit", "twitter")]
        out = asyncio.run(browserfarm.status(profs))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.farm_command == "reap":
        profs = store.all() or [default_profile(p) for p in ("reddit", "twitter")]
        reaped = asyncio.run(browserfarm.reap_idle(profs))
        print(json.dumps({"reaped": reaped}, ensure_ascii=False, indent=2))
        return 0

    if args.farm_command == "login":
        if not args.platform:
            print("farm login needs <platform>", file=sys.stderr)
            return 1
        import os

        from .extract.browserfarm import (
            _clean_stale_scope,
            _cmd,
            _profile_dir,
            _scoped,
        )
        prof = (store.all() or [default_profile(args.platform)])[0]
        pdir = _profile_dir(prof)
        os.makedirs(pdir, exist_ok=True)
        target = ("https://www.reddit.com/" if prof.platform == "reddit"
                  else "https://x.com/")
        desktop = os.environ.get("DISPLAY")
        if not desktop:
            print("no DISPLAY in environment — cannot open a headed window",
                  file=sys.stderr)
            return 1
        env = {k: v for k, v in os.environ.items()
               if k in {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG",
                        "LC_ALL", "LC_CTYPE", "DISPLAY", "XAUTHORITY",
                        "XDG_RUNTIME_DIR", "TMPDIR",
                        "AGENT_BROWSER_ENCRYPTION_KEY"}}
        env["DISPLAY"] = desktop  # never the farm display for login

        async def _run(argv: list[str]) -> int:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, env=env)
            return await proc.wait()

        async def _daemon_free() -> bool:
            proc = await asyncio.create_subprocess_exec(
                *_cmd(["doctor"]), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            return b"No active daemons" in out

        async def _stop_daemon() -> bool:
            # agent-browser keeps ONE daemon per user; launch flags are
            # honored only by a fresh daemon. Stop it (settling until it is
            # really gone) so the login window opens headed on THIS desktop
            # rather than attaching to the farm's invisible :99 daemon.
            for _ in range(8):
                await _run(_cmd(["close", "--all"]))
                if await _daemon_free():
                    return True
                await asyncio.sleep(1.5)
            return False

        async def _login() -> int:
            if not await _stop_daemon():
                print("could not stop the running browser daemon",
                      file=sys.stderr)
                return 1
            argv = _scoped(
                _cmd(["--headed", "--profile", pdir, "open", target]),
                prof.name, extra_env=env)
            code = await _run(argv)
            if code != 0:
                await _clean_stale_scope(prof.name)  # stale scope race
                code = await _run(argv)
            if code != 0:
                print(f"browser launch failed (rc={code})", file=sys.stderr)
                return code
            return 0  # browser up; human interaction happens outside

        code = asyncio.run(_login())
        if code != 0:
            return code
        print(f"Log in to {prof.platform} in the opened window, then "
              f"press Enter to finish (profile: {pdir})", file=sys.stderr)
        with contextlib.suppress(EOFError):  # non-interactive stdin
            input()
        # stop the daemon: the farm relaunches headed on its own display
        # with the login state persisted in the profile dir
        code = asyncio.run(_run(_cmd(["close", "--all"])))
        print("login window closed — session state saved.", file=sys.stderr)
        return code
    return 1
    from . import embeddings, rerank

    rerank._get_model()          # same-package preload (private, intentional)
    embeddings._get_model()      # same-package preload (private, intentional)
    print(json.dumps({"rerank": rerank.status(), "embed": embeddings.status()},
                     ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kortex-search",
        description="Unified web-search & research MCP server.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the MCP server (default: stdio)")
    serve.add_argument("--transport", choices=["stdio", "http", "sse"],
                       default="stdio", help="stdio (default) | http | sse")
    serve.add_argument("--host", default=MCP_HOST,
                       help=f"bind host for http/sse (default: {MCP_HOST})")
    serve.add_argument("--port", type=int, default=MCP_PORT,
                       help=f"bind port for http/sse (default: {MCP_PORT})")
    serve.add_argument("--warm", action="store_true",
                       help="preload the rerank+embed models in-process "
                            "after startup (gateway deployments)")
    sub.add_parser("doctor", help="print the full health report as JSON")
    sub.add_parser("check", help="gate: 18 sources + Redis reachable (non-zero on failure)")
    sub.add_parser("version", help="print the package version")
    sub.add_parser("warm", help="preload the rerank + embed models")
    farm = sub.add_parser("farm", help="managed profile farm control")
    fsub = farm.add_subparsers(dest="farm_command")
    fsub.add_parser("status", help="per-profile browser state (CDP, idle)")
    flogin = fsub.add_parser("login", help="one-time interactive login for a profile")
    flogin.add_argument("platform", help="platform name (reddit|twitter)")
    flogin.add_argument("--headed", action="store_true",
                        help="force a visible window (desktop login)")
    fsub.add_parser("reap", help="shut down profiles idle beyond FARM_IDLE_TTL")
    vault = sub.add_parser("vault", help="per-persona secrets vault management")
    vsub = vault.add_subparsers(dest="vault_command")
    vmigrate = vsub.add_parser("migrate",
                               help="move legacy flat env files into the persona vault (D7.3)")
    vmigrate.add_argument("--dry-run", action="store_true",
                          help="report what would move without touching files")
    vsub.add_parser("status", help="vault layout + hygiene findings")
    harden = sub.add_parser("harden", help="L3 kernel egress filter (nftables cgroupv2)")
    harden.add_argument("--install", dest="harden_action", action="store_const",
                        const="install", help="install the egress filter (derive "
                        "the cgroup from the current process; run inside the "
                        "scope or the unit — see docs/deployment.md)")
    harden.add_argument("--dry-run", action="store_true",
                        help="print the ruleset without touching the kernel")
    harden.add_argument("--status", dest="harden_action", action="store_const",
                        const="status", help="report install/coverage state")
    harden.add_argument("--uninstall", dest="harden_action", action="store_const",
                        const="uninstall", help="delete the ks_egress table")
    harden.add_argument("--check", dest="harden_action", action="store_const",
                        const="check", help="report browser-tier enforceability")
    harden.add_argument("--mark-installed", dest="harden_action",
                        action="store_const", const="mark-installed",
                        help="record a successful privileged load (run after "
                             "'sudo nft -f' — the unprivileged probe cannot "
                             "read the kernel table)")
    harden.add_argument("--for", dest="for_unit", metavar="UNIT",
                        default=None,
                        help="target a running user unit's cgroup instead of "
                             "the ks-egress wrapper scope (companion-loader "
                             "deployments)")
    harden.add_argument("--sudo", action="store_true",
                        help="load the ruleset with elevation (root already: "
                        "auto); without it, rules are written for manual load")
    harden.set_defaults(harden_action="status", for_unit=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    handlers = {
        "serve": _cmd_serve,
        "doctor": _cmd_doctor,
        "check": _cmd_check,
        "version": _cmd_version,
        "warm": _cmd_warm,
        "farm": _cmd_farm,
        "vault": _cmd_vault,
        "harden": _cmd_harden,
    }
    return handlers[args.command or "serve"](args)


if __name__ == "__main__":
    sys.exit(main())
