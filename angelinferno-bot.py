"""Render-compatible entrypoint wrapper for the main bot application."""

import os
import sys
import subprocess


def _ensure_python_dependencies():
    try:
        import requests  # noqa: F401
    except ModuleNotFoundError:
        requirements_path = os.path.join(os.path.dirname(__file__), "requirements.txt")
        if not os.path.exists(requirements_path):
            raise FileNotFoundError(
                f"Missing requirements file: {requirements_path}. "
                "Render deploy must include requirements.txt."
            )

        print("[startup] Installing Python dependencies from requirements.txt...", file=sys.stderr)
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "-r",
            requirements_path,
        ])

        # Re-import to verify installation succeeded.
        import requests  # noqa: F401


def main_wrapper():
    _ensure_python_dependencies()
    from fernotest import main
    return main()


if __name__ == "__main__":
    main_wrapper()
