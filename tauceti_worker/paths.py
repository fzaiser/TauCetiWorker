"""tauceti_worker.paths — install-location detection and self-invocation helpers.

`HERE` is the single source of truth for where the worker's bundled assets (prompts/, scripts/)
live: the repo root in a source checkout, and the package dir in an installed wheel. We detect
which by asking whether `prompts/` sits beside the modules — only true in the wheel (where
pyproject force-includes it into the package); in a checkout prompts/ stays at the repo root, one
level up.

`RUNTIME_ROOT` is where the runtime dirs (state/, checkouts/, logs/) live. A source checkout keeps
them beside the code, which is what the Docker image mounts; an installed wheel keeps them in the
user's state directory, where `uv tool upgrade` cannot delete them.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path

_pkg = Path(__file__).resolve().parent  # …/tauceti_worker
HERE = _pkg if (_pkg / "prompts").is_dir() else _pkg.parent


def _login_home() -> Path:
    """The login user's home via the password database, not $HOME: an isolated worker's $HOME is
    itself under the runtime root, and every process in a fleet must resolve the same root."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return Path(os.path.expanduser("~"))


def _runtime_root(env: Mapping[str, str], here: Path, pkg: Path, platform: str, home: Path) -> Path:
    override = env.get("TAUCETI_RUNTIME_ROOT")
    if override:
        return Path(override).expanduser()
    if here != pkg:
        return here  # a source checkout
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "tauceti"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "tauceti"
    return home / ".local" / "state" / "tauceti"


RUNTIME_ROOT = _runtime_root(os.environ, HERE, _pkg, sys.platform, _login_home())

# The branch-lease helper the agents run on PATH inside a round. Overridable for tests.
CLAIM_SH = os.environ.get("TAUCETI_CLAIM_SH") or str(HERE / "scripts" / "claim.sh")

# CPython builds do not agree on the default CA-bundle path. In particular, uv's standalone
# CPython uses OpenSSL's /etc/ssl/cert.pem default on NixOS, while NixOS exposes the system bundle
# at /etc/ssl/certs/ca-certificates.crt. A worker service has no login shell to supply
# SSL_CERT_FILE, so quota telemetry otherwise fails closed before the worker can select any work.
_COMMON_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, NixOS
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # macOS, Alpine
)


def _ssl_cert_candidates(env: Mapping[str, str]) -> tuple[str | None, ...]:
    # get_default_verify_paths().cafile is shadowed by the process's SSL_CERT_FILE, which may
    # differ from *env*. openssl_cafile is the interpreter's environment-independent default.
    import ssl

    openssl_default = ssl.get_default_verify_paths().openssl_cafile
    return (env.get("NIX_SSL_CERT_FILE"), openssl_default, *_COMMON_CA_BUNDLES)


def ensure_ssl_cert_file(env: MutableMapping[str, str] | None = None) -> str | None:
    """Put a usable system CA bundle in *env* when its Python has no working default.

    Preserve an explicit ``SSL_CERT_FILE`` even when it is unusual: operator configuration wins.
    Otherwise prefer Nix's advertised bundle, the interpreter's existing default, then common
    system locations. Return the selected path, or ``None`` when no usable bundle can be found.
    """
    base = os.environ if env is None else env
    configured = base.get("SSL_CERT_FILE")
    if configured:
        return configured
    for raw in _ssl_cert_candidates(base):
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser()
            if candidate.is_file():
                base["SSL_CERT_FILE"] = str(candidate)
                return str(candidate)
        except (OSError, RuntimeError):
            continue
    return None


def self_argv(*tail) -> list[str]:
    """argv to re-invoke this worker as a child (a `_round` or `_heartbeat` subprocess). `-m`
    works from a source checkout (paired with self_env's PYTHONPATH) and from an installed wheel
    alike — unlike the old `[sys.executable, __file__]`, which pointed at a now-unrunnable module."""
    return [sys.executable, "-m", "tauceti_worker", *(str(a) for a in tail)]


def self_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a self_argv child: ensure the package is importable. In a source checkout
    HERE is the repo root (which contains tauceti_worker/), so putting it on PYTHONPATH lets the
    child `python -m tauceti_worker`; in a wheel the package is already importable and this is a
    harmless no-op."""
    base = dict(os.environ if env is None else env)
    existing = base.get("PYTHONPATH")
    base["PYTHONPATH"] = os.pathsep.join([str(HERE)] + ([existing] if existing else []))
    base.setdefault("TAUCETI_RUNTIME_ROOT", str(RUNTIME_ROOT))  # one root per fleet, whatever $HOME
    ensure_ssl_cert_file(base)
    return base


def entry_cmd() -> list[str]:
    """The user-facing command that runs this worker, for the TUI's copy/launch helpers. A source
    checkout has the executable `./tauceti` shim beside HERE; an installed wheel puts `tauceti` on
    PATH as a console script."""
    shim = HERE / "tauceti"
    return [str(shim)] if shim.exists() else ["tauceti"]
