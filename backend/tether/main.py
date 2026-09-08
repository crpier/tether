"""Tether's FastAPI application and server entry point."""

from argparse import ArgumentParser
from pathlib import Path

from fastapi import FastAPI
from uvicorn import run

app = FastAPI(title="Tether")

if __name__ == "__main__":
    parser = ArgumentParser(description="Run the Tether backend.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    arguments = parser.parse_args()

    run(
        "tether.main:app",
        host=arguments.host,
        port=arguments.port,
        reload=arguments.reload,
        reload_dirs=[str(Path(__file__).resolve().parent)]
        if arguments.reload
        else None,
    )
