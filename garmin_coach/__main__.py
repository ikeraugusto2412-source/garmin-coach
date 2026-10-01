"""CLI: python -m garmin_coach {login,probe,sync,status,web,web-password,daily,schedule}"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from .api import RateLimited
from .auth import AuthError, get_client, tokenstore_path


def _daily() -> int:
    """Sincroniza, genera la web cifrada y la publica. Pensado para la tarea programada (sin MFA)."""
    from .sync import run_sync
    from .web import WebError, build_site, origin_url, publish_site

    def log(msg: str) -> None:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] {msg}", flush=True)

    code = 0
    try:
        summary = run_sync(get_client(interactive=False), full=False, progress=lambda m: None)
        log(f"Sincronización OK: {summary.activities_new} actividades, {summary.days_training} días"
            + (f", {len(set(summary.failures))} endpoints con error" if summary.failures else ""))
    except (AuthError, RateLimited) as e:
        log(f"Sincronización fallida: {e}")
        code = 1
    except Exception as e:  # noqa: BLE001 - la web se regenera igualmente con lo que haya
        log(f"Sincronización fallida ({type(e).__name__}): {e}")
        code = 1
    try:
        build_site()
        if origin_url():
            log(f"Web publicada: {publish_site()}")
        else:
            log("Web generada en site/ (sin repositorio en GitHub todavía: no se publica)")
    except WebError as e:
        log(f"Web: {e}")
        code = code or 2
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="garmin_coach", description="Garmin Connect → SQLite → Claude")
    parser.add_argument("-v", "--verbose", action="store_true", help="log detallado")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="inicia sesión (pide MFA si hace falta) y guarda los tokens")
    p_probe = sub.add_parser("probe", help="consulta los últimos días y muestra qué datos llegan")
    p_probe.add_argument("--days", type=int, default=7)
    p_sync = sub.add_parser("sync", help="sincronización incremental a garmin.db")
    p_sync.add_argument("--full", action="store_true", help="descarga todo el histórico")
    sub.add_parser("status", help="muestra el estado de entrenamiento y alertas desde garmin.db")
    p_web = sub.add_parser("web", help="genera la web cifrada en site/")
    p_web.add_argument("--publish", action="store_true", help="y la publica en GitHub Pages (rama gh-pages)")
    sub.add_parser("web-password", help="define la contraseña de la web (se guarda en .env)")
    sub.add_parser("daily", help="sincroniza + genera + publica (lo usa la tarea programada)")
    p_sch = sub.add_parser("schedule", help="gestiona la tarea diaria de macOS")
    p_sch.add_argument("action", choices=["install", "uninstall", "status"])
    p_sch.add_argument("--hora", default=None,
                       help="horas de ejecución separadas por comas, p. ej. 7:30,9:30,21:30 "
                            "(por defecto 7:30,9:30,15:00,21:30)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "status":
        from contextlib import closing

        from . import db, queries

        with closing(db.connect_readonly()) as conn:
            print(queries.get_training_status(conn))
        return 0

    if args.cmd == "web-password":
        import getpass

        from .web import MIN_PASSWORD_LEN, set_web_password

        pw = getpass.getpass("Nueva contraseña para la web: ")
        if len(pw) < MIN_PASSWORD_LEN:
            print(f"✗ Usa al menos {MIN_PASSWORD_LEN} caracteres: la página cifrada es pública.", file=sys.stderr)
            return 1
        if getpass.getpass("Repítela: ") != pw:
            print("✗ Las contraseñas no coinciden.", file=sys.stderr)
            return 1
        set_web_password(pw)
        print("✓ Contraseña guardada en .env. Genera la web con: python -m garmin_coach web --publish")
        return 0

    if args.cmd == "web":
        from .web import WebError, build_site, publish_site

        try:
            out = build_site()
            print(f"✓ Web cifrada generada en {out}")
            if args.publish:
                print(f"✓ Publicada: {publish_site()} (GitHub Pages tarda 1-2 min en actualizarse)")
        except WebError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "daily":
        return _daily()

    if args.cmd == "schedule":
        from . import schedule

        if args.action == "install":
            try:
                print(schedule.install(args.hora or schedule.DEFAULT_TIMES))
            except ValueError as e:
                print(f"✗ {e}", file=sys.stderr)
                return 1
        elif args.action == "uninstall":
            print(schedule.uninstall())
        else:
            print(schedule.status())
        return 0

    try:
        client = get_client(interactive=True)
        if args.cmd == "login":
            print(f"✓ Sesión iniciada como {client.get_full_name()}. Tokens en {tokenstore_path()}")
        elif args.cmd == "probe":
            from .probe import run_probe

            run_probe(client, days=args.days)
        elif args.cmd == "sync":
            from .sync import run_sync

            summary = run_sync(client, full=args.full, progress=print)
            print("\n" + summary.text())
    except AuthError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except RateLimited as e:
        print(f"✗ {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nInterrumpido. El progreso guardado se reanudará en la próxima sincronización.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
