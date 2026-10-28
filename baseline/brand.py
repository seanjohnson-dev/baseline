"""The single place where the product name lives.

Everything else derives the name from here (``scripts/rename.py`` rewrites this
file, the package directory and the launcher when the product is renamed).
"""

NAME = "baseline"

# Command shown in usage/help and the launcher file name (bin/<CLI_NAME>).
CLI_NAME = NAME
# Prefix for environment variables the tool reads (e.g. <PREFIX>RUNS).
ENV_PREFIX = NAME.upper() + "_"
# Directory inside .git/ holding last.json, the base-result cache, pyc files.
CACHE_DIR_NAME = NAME
# Prefix for temp dirs (base worktrees, report files).
TMP_PREFIX = NAME + "-"
