"""Prompt loading for Kombucha v2.

Loads system prompts from the prompts/ directory. Future: SQLite-backed
prompt registry with versioning.
"""

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("kombucha.prompts")


def load_prompt(name: str, prompts_dir: Optional[str] = None) -> str:
    """Load a prompt file by name from the prompts directory.

    Args:
        name: Filename to load (e.g. 'system.md', 'compress.md')
        prompts_dir: Directory to search. Defaults to 'prompts/' relative
                     to the project root.

    Returns:
        The prompt text content.

    Raises:
        FileNotFoundError: If the prompt file doesn't exist.
    """
    if prompts_dir:
        path = Path(prompts_dir) / name
    else:
        # Try relative to this file's parent (kombucha/), then CWD
        candidates = [
            Path(__file__).parent.parent / "prompts" / name,
            Path("prompts") / name,
        ]
        path = None
        for c in candidates:
            if c.exists():
                path = c
                break
        if path is None:
            path = candidates[0]  # will raise FileNotFoundError

    return path.read_text(encoding="utf-8")


def make_prompt_loader(prompts_dir: Optional[str] = None):
    """Create a prompt loader function bound to a specific directory."""
    def _load(name: str) -> str:
        return load_prompt(name, prompts_dir)
    return _load
