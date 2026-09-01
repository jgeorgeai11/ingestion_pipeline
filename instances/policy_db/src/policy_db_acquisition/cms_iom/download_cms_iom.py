"""The ``download-cms-iom`` console script.

The whole of the acquisition mechanics -- fetching, retries, the manifest, the
skip decision, the run summary, the non-zero exit -- lives in the
``ingpipe_acquisition`` engine. All this entry point supplies is the one genuinely
source-specific piece: the discoverer that scrapes the CMS index and each
manual page for chapter PDFs.

This file is what an instance-supplied discoverer costs: three lines and an
import. A source whose targets can be computed instead (``usc_titles``) has no
file here at all -- its console script points straight at
``ingpipe_acquisition.runner:main``.

Usage:
    uv run download-cms-iom \\
        --config instances/policy_db/config/ingpipe_acquisition/cms_iom/download_cms_iom.toml
"""

from ingpipe_acquisition.runner import main as run_main

from policy_db_acquisition.cms_iom.discover import discover


def main() -> None:
    """Run an acquisition config with the cms_iom discoverer."""
    run_main(discover=discover)


if __name__ == "__main__":
    main()
