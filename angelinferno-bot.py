"""Render-compatible entrypoint wrapper for the main bot application."""

import os
import sys
import subprocess


def _venv_paths():
    root = os.path.dirname(os.path.abspath(__file__))
    return {
        'python': os.path.join(root, '.venv', 'bin', 'python'),
        'python_windows': os.path.join(root, '.venv', 'Scripts', 'python.exe'),
        'requirements': os.path.join(root, 'requirements.txt'),
    }


def _find_venv_python():
    paths = _venv_paths()
    for path in (paths['python'], paths['python_windows']):
        if os.path.isfile(path):
            return path
    return None


def _create_venv():
    venv_python = _find_venv_python()
    if venv_python:
        return venv_python

    root = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(root, '.venv')
    print('[startup] Creating local virtual environment .venv', file=sys.stderr)
    subprocess.check_call([sys.executable, '-m', 'venv', venv_dir])

    venv_python = _find_venv_python()
    if not venv_python:
        raise RuntimeError('Failed to create venv interpreter in .venv')

    return venv_python


def _install_requirements(venv_python):
    requirements_path = _venv_paths()['requirements']
    if not os.path.isfile(requirements_path):
        raise FileNotFoundError(
            f"Missing requirements file: {requirements_path}. Render deploy must include requirements.txt."
        )

    print('[startup] Installing Python packages into .venv', file=sys.stderr)
    subprocess.check_call([venv_python, '-m', 'pip', 'install', '--upgrade', 'pip'])
    subprocess.check_call([venv_python, '-m', 'pip', 'install', '--no-cache-dir', '-r', requirements_path])


def _ensure_venv_python():
    venv_python = _find_venv_python()
    if not venv_python:
        venv_python = _create_venv()

    _install_requirements(venv_python)

    current_python = os.path.realpath(sys.executable)
    target_python = os.path.realpath(venv_python)
    if current_python != target_python:
        os.execv(target_python, [target_python] + sys.argv)


if __name__ == "__main__":
    _ensure_venv_python()
    from fernotest import main
    main()
