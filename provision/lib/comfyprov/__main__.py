"""Command line entry point: ``python -m comfyprov <command>``.

Planners run here; execution happens in the shell. Every command returns a
documented exit code (see ``actions.EXIT_*``) so ``provision.sh`` and the
launcher can react without parsing text.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import auth, checks, models, report
from . import facts as facts_module
from . import plan as plan_module
from . import verify as verify_module
from .actions import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PREREQ,
    EXIT_USAGE,
    EXIT_VERIFY,
    STATUS_BLOCKED,
    emit_stream,
)
from .config import Config, ConfigError, load
from .probe import Probe
from .server import HttpClient, HttpError


def _add_plan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stream", action="store_true", help="line protocol for the bash executor (default)")
    parser.add_argument("--json", action="store_true", help="human/JSON plan dump")
    parser.add_argument("--ref", default=None, help="override COMFYUI_REF (or 'latest')")
    parser.add_argument("--set", dest="models_set", default=None, choices=sorted(models.SETS))
    parser.add_argument("--check", dest="check_only", action="store_true")
    parser.add_argument("--verify", dest="verify_sha", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--force", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comfyprov", description="ComfyUI provisioning logic")
    parser.add_argument("--config", default=None, help="path to provision.env (defaults to the repository copy)")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_cmd = sub.add_parser("plan", help="compute the plan for one step")
    plan_cmd.add_argument("step", choices=sorted(set(plan_module.STEPS) | {"swap"}))
    _add_plan_options(plan_cmd)

    status_cmd = sub.add_parser("status", help="read-only status of every step")
    status_cmd.add_argument("--json", action="store_true")

    preflight_cmd = sub.add_parser("preflight", help="read-only environment checks")
    preflight_cmd.add_argument("--no-network", action="store_true")
    preflight_cmd.add_argument("--json", action="store_true")

    doctor_cmd = sub.add_parser("doctor", help="print diagnostics")
    doctor_cmd.add_argument("--json", action="store_true")

    verify_cmd = sub.add_parser("verify", help="check a running ComfyUI")
    verify_cmd.add_argument("--url", required=True)
    verify_cmd.add_argument("--full", action="store_true")
    verify_cmd.add_argument("--output-dir", default=None)
    verify_cmd.add_argument("--json", action="store_true")

    sub.add_parser("gpucheck", help="run the PyTorch/ROCm check inside the venv")

    auth_cmd = sub.add_parser("auth", help="store a Civitai API token")
    auth_cmd.add_argument("--force", action="store_true")

    verify_file = sub.add_parser("verify-file", help="verify size/sha256 of a file against the manifest")
    verify_file.add_argument("--path", required=True)
    verify_file.add_argument("--size", type=int, default=0)
    verify_file.add_argument("--sha256", default="")

    facts_cmd = sub.add_parser("facts", help="dump the probed facts (debugging)")
    facts_cmd.add_argument("--json", action="store_true")

    start_check = sub.add_parser("start-check", help="shell assignments used by start-comfyui.sh")
    start_check.add_argument("--json", action="store_true")

    summarize = sub.add_parser("models-summary", help="file counts per model folder")
    summarize.add_argument("--json", action="store_true")

    return parser


def _load(args) -> Config:
    return load(args.config)


def _collect(config: Config, probe: Probe | None = None):
    return facts_module.collect(config, probe or Probe())


def _options(args) -> plan_module.Options:
    return plan_module.Options(
        force=args.force,
        ref=args.ref,
        models_set=args.models_set,
        check_only=args.check_only,
        verify_sha=args.verify_sha,
        full=args.full,
    )


def _cmd_plan(args) -> int:
    config = _load(args)
    facts = _collect(config)
    plan = plan_module.Planner(config, facts, Probe(), _options(args)).build(args.step)
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
        return plan.exit_code if plan.status == STATUS_BLOCKED else EXIT_OK
    # The stream carries the status/exit code itself; a non-zero exit here would
    # make the bash executor treat a blocked plan as a broken planner.
    emit_stream(plan, sys.stdout)
    return EXIT_OK


def _cmd_status(args) -> int:
    config = _load(args)
    facts = _collect(config)
    plans = plan_module.status(config, facts, Probe())
    if args.json:
        print(json.dumps([item.to_dict() for item in plans], indent=2))
        return EXIT_OK
    width = max(len(item.step) for item in plans)
    for item in plans:
        pending = len(item.pending)
        print(f"{item.step:<{width}}  {item.status:<12}  {pending:>2} pending  {item.exit_code}")
    reboot = plan_module.reboot_needed(plans)
    print()
    print(f"reboot required: {'yes' if reboot else 'no'}")
    for item in plans:
        for note in item.notes:
            print(f"  {item.step}: {note}")
    return EXIT_OK


def _cmd_preflight(args) -> int:
    config = _load(args)
    facts = _collect(config)
    findings, code = checks.preflight(
        config,
        facts,
        Probe(),
        check_network=not args.no_network,
    )
    if args.json:
        print(json.dumps([item.to_dict() for item in findings], indent=2))
    else:
        print("\n".join(item.render() for item in findings))
        summary = report.counts(findings)
        print(f"\n{summary['ok']} ok, {summary['warn']} warnings, {summary['fail']} failures")
    return code


def _cmd_doctor(args) -> int:
    config = _load(args)
    facts = _collect(config)
    findings = checks.doctor(config, facts, Probe())
    if args.json:
        print(json.dumps([item.to_dict() for item in findings], indent=2))
    else:
        print("\n".join(item.render() for item in findings))
    return EXIT_OK


def _cmd_verify(args) -> int:
    config = _load(args)
    manifest = models.load_manifest()
    findings, code = verify_module.verify_server(
        HttpClient(),
        args.url,
        config,
        manifest,
        Probe(),
        full=args.full,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    if args.json:
        print(json.dumps([item.to_dict() for item in findings], indent=2))
    else:
        print("\n".join(item.render() for item in findings))
    return code


def _cmd_gpucheck(_args) -> int:
    from . import gpucheck

    return gpucheck.main()


def _cmd_auth(args) -> int:
    config = _load(args)
    token_file = config.civitai_token_file
    existing = auth.read_token(token_file)
    if existing and not args.force:
        answer = input(f"A token is already stored in {token_file}. Replace it? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Keeping the existing token.")
            return EXIT_OK

    print("Create an API key at https://civitai.com/user/account (API Keys section).")
    print("Token input is hidden.")
    try:
        token = getpass.getpass("Civitai API token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return EXIT_USAGE
    if not token:
        print("Empty token; nothing saved.", file=sys.stderr)
        return EXIT_USAGE

    print("Validating the token against civitai.com")
    status, detail = auth.validate_token(token)
    if status != 200:
        print(f"Civitai returned {status if status is not None else 'no response'} ({detail}).", file=sys.stderr)
        if not args.force:
            print("Re-run with --force to save it anyway.", file=sys.stderr)
            return EXIT_ERROR
        print("--force given; saving an unverified token.", file=sys.stderr)
    auth.save_token(token_file, token)
    print(f"  OK Token saved to {token_file} (mode 600)")
    return EXIT_OK


def _cmd_verify_file(args) -> int:
    path = Path(args.path)
    try:
        size = path.stat().st_size
    except OSError:
        print(f"missing: {path}", file=sys.stderr)
        return EXIT_VERIFY
    if args.size and size != args.size:
        print(f"size {size} != expected {args.size}: {path}", file=sys.stderr)
        return EXIT_VERIFY
    if args.sha256:
        actual = models.sha256_of(path)
        if actual != args.sha256.lower():
            print(f"sha256 {actual} != expected {args.sha256}: {path}", file=sys.stderr)
            return EXIT_VERIFY
    return EXIT_OK


def _cmd_facts(args) -> int:
    config = _load(args)
    facts = _collect(config)
    if args.json:
        payload = {
            "user": facts.user,
            "uid": facts.uid,
            "os": facts.os_label,
            "kernel": facts.kernel,
            "arch": facts.arch,
            "python312": facts.python312,
            "gfx": facts.gfx,
            "gfx_source": facts.gfx_source,
            "kfd_present": facts.kfd_present,
            "kfd_accessible": facts.kfd_accessible,
            "groups": [
                {"name": g.name, "exists": g.exists, "in_session": g.in_session, "members": list(g.members)}
                for g in facts.groups
            ],
            "big_mounted": facts.big_mounted,
            "models_dir_exists": facts.models_dir_exists,
            "venv_python": facts.venv_python,
            "torch": facts.torch_versions,
            "comfyui": {
                "installed": facts.checkout("comfyui").installed,
                "head": facts.checkout("comfyui").head,
                "clean": facts.checkout("comfyui").clean,
            },
        }
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print(f"user: {facts.user} (uid {facts.uid})")
    print(f"os:   {facts.os_label} / {facts.kernel} / {facts.arch}")
    print(
        f"gpu:  {facts.gfx or 'unknown'} kfd={'present' if facts.kfd_present else 'missing'} "
        f"access={facts.kfd_accessible}"
    )
    print(f"groups: {', '.join(f'{g.name}:{g.in_session}' for g in facts.groups)}")
    print(f"venv: {facts.venv_python or 'missing'}; torch={facts.torch_versions or 'not installed'}")
    return EXIT_OK


def _cmd_models_summary(args) -> int:
    config = _load(args)
    counts = models.model_counts(Probe(), config.models_dir)
    if args.json:
        print(json.dumps(counts))
    else:
        print(" ".join(f"{name}={count}" for name, count in counts.items()))
    return EXIT_OK


def _cmd_start_check(args) -> int:
    """Small shell contract for the launcher: no text parsing in bash."""
    config = _load(args)
    facts = _collect(config)
    render = facts.group("render")
    payload = {
        "kfd_accessible": facts.kfd_accessible,
        "in_db": bool(render and render.in_db(facts.user)),
        "session_groups": sorted(Probe().session_groups),
        "gfx": facts.gfx or "",
        "models_dir": str(config.models_dir),
    }
    if args.json:
        print(json.dumps(payload))
        return EXIT_OK
    groups = " ".join(payload["session_groups"])
    print(f"KFD_ACCESS={1 if payload['kfd_accessible'] else 0}")
    print(f"IN_DB={1 if payload['in_db'] else 0}")
    print(f"GFX={payload['gfx']}")
    print(f"SESSION_GROUPS='{groups}'")
    return EXIT_OK


_HANDLERS = {
    "plan": _cmd_plan,
    "status": _cmd_status,
    "preflight": _cmd_preflight,
    "doctor": _cmd_doctor,
    "verify": _cmd_verify,
    "gpucheck": _cmd_gpucheck,
    "auth": _cmd_auth,
    "verify-file": _cmd_verify_file,
    "facts": _cmd_facts,
    "models-summary": _cmd_models_summary,
    "start-check": _cmd_start_check,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _HANDLERS[args.command](args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_PREREQ
    except models.ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return EXIT_PREREQ
    except HttpError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return EXIT_VERIFY
    except KeyboardInterrupt:
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
