"""Render-compatible entrypoint wrapper for the main bot application."""

import os
import sys


def _ensure_venv_python():
    root = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(root, ".venv", "bin", "python"),
        os.path.join(root, ".venv", "Scripts", "python.exe"),
    ]
    for python_path in candidates:
        if os.path.isfile(python_path):
            current_python = os.path.realpath(sys.executable)
            target_python = os.path.realpath(python_path)
            if current_python != target_python:
                os.execv(target_python, [target_python] + sys.argv)


if __name__ == "__main__":
    _ensure_venv_python()
    from fernotest import main
    main()
