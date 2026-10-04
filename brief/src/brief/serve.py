"""Start one of the two web servers: `python -m brief.serve http` or `python -m brief.serve https`."""

import sys

import uvicorn

from brief.jsonlog import log, uvicorn_log_config
from brief.netinfo import network_info, with_detected_address
from brief.settings import Settings
from brief.tlscert import resolve_certificate


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("http", "https"):
        print("usage: python -m brief.serve http|https", file=sys.stderr)
        return 2
    mode = args[0]
    settings = Settings.from_env()
    options: dict = {}
    if mode == "https":
        net = network_info(settings)
        log("network", **net)
        settings = with_detected_address(settings, net)  # the certificate then covers the LAN address
        cert, key, generated = resolve_certificate(settings)
        options = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        log(
            "tls",
            certificate=str(cert),
            generated=generated,
            user_supplied=not generated and "tls" in str(cert.parent),
        )
    port = settings.https_port if mode == "https" else settings.http_port
    log("web_starting", mode=mode, port=port)
    uvicorn.run(
        f"brief.web:create_{mode}_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - the container's own network namespace; Docker decides who can reach it
        port=port,
        server_header=False,
        date_header=False,
        access_log=False,
        proxy_headers=False,
        log_config=uvicorn_log_config(),
        **options,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
