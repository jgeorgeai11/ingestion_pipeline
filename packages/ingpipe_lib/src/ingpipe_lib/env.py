"""Shared environment loader for the workspace's entry-point scripts.

Every entry point that connects to PostgreSQL reads its credentials from the
four ``POSTGRES_*`` environment variables. Before this module each script
called ``load_dotenv()`` at module scope, which bound a run's credentials to
the process's working directory: the same command resolved to a different
server depending on where it was invoked from.

``load_env`` backs the required ``--env-file`` flag those scripts expose, so
every run names the credentials it is for. There is no default dotenv search:
with the engine/instance split there is no repo-wide default database, so
there is nothing correct for an unnamed search to find — production runs name
an instance file (``instances/<instance>/.env[.role]``) and the test suite
names the engine's own ``.env.test``. The named file always wins over the
ambient environment (``override=True``), because naming a file is an explicit
statement of intent and should beat a stale ``POSTGRES_HOST`` left over in
the shell.

A named path that does not exist is a hard error. ``load_dotenv`` merely
returns False for a missing file, so a typo would otherwise fall through
silently to whatever credentials the environment already held -- the exact
class of bug this module exists to remove.
"""

from pathlib import Path

from dotenv import load_dotenv

from ingpipe_lib.logconfig import get_logger

__all__ = ["load_env"]

logger = get_logger(__name__)


def load_env(env_file: str | Path) -> None:
    """Load environment variables from an explicitly named dotenv file.

    Args:
        env_file: Path to a dotenv file supplying the ``POSTGRES_*``
            variables. The file's values override any already present in
            the environment.

    Raises:
        FileNotFoundError: If `env_file` names a path that does not exist.
            Failing here is intentional: a silent fallthrough to the ambient
            environment would connect the run to the wrong database.
    """
    env_path = Path(env_file)
    if not env_path.exists():
        logger.error(f"Env file not found: {env_path}")
        raise FileNotFoundError(f"Env file not found: {env_path}")

    # override=True: an explicitly named file outranks the ambient environment.
    load_dotenv(env_path, override=True)
    logger.info(f"Loaded environment from: {env_path}")
