"""Command-line entry point."""

from __future__ import annotations

import argparse
import copy
import logging
import os
import webbrowser
from pathlib import Path
from threading import Timer

import uvicorn

from quant_monitor.config import load_settings


def _build_uvicorn_log_config():
    """Return an isolated Uvicorn logging config with timestamps everywhere."""

    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config["formatters"]["default"]["fmt"] = (
        "%(asctime)s %(levelprefix)s %(name)s: %(message)s"
    )
    config["formatters"]["access"]["fmt"] = (
        '%(asctime)s %(levelprefix)s %(name)s: %(client_addr)s - '
        '"%(request_line)s" %(status_code)s'
    )
    return config


def cli() -> None:
    parser = argparse.ArgumentParser(description="启动本地 A 股量化数据监控台")
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--host", help="监听地址；默认仅本机 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    settings = load_settings(args.config)
    host = args.host or settings.host
    port = args.port or settings.port
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.no_browser:
        Timer(1.25, lambda: webbrowser.open("http://{}:{}".format(host, port))).start()
    if args.reload:
        # The reload worker imports the application in a child process, so pass
        # the selected configuration through its environment.
        if args.config:
            os.environ["QUANT_MONITOR_CONFIG_PATH"] = str(Path(args.config).resolve())
        application = "quant_monitor.app:app"
    else:
        from quant_monitor.app import create_app

        application = create_app(settings)
    uvicorn.run(
        application,
        host=host,
        port=port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
        log_config=_build_uvicorn_log_config(),
    )


if __name__ == "__main__":
    cli()
