import os
import stat
from pathlib import Path


MAX_PROMPT_BYTES = 8 * 1024
_BUNDLED_DIR = Path(__file__).with_name("prompts")


class PromptFileError(RuntimeError):
    pass


def load_prompt(env_name, bundled_name):
    """Read one bounded UTF-8 prompt on demand without disclosing failure details."""
    configured = os.getenv(env_name)
    path = Path(configured) if configured else _BUNDLED_DIR / bundled_name
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError
            chunks = []
            remaining = MAX_PROMPT_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > MAX_PROMPT_BYTES:
                raise ValueError
            text = raw.decode("utf-8", errors="strict")
            if not text.strip() or "\x00" in text:
                raise ValueError
            # Tracked text files end with the conventional newline; the original
            # embedded defaults did not, so omit only that bundled file terminator.
            if not configured and text.endswith("\n"):
                text = text[:-1]
            return text
        finally:
            os.close(fd)
    except (OSError, UnicodeError, ValueError):
        raise PromptFileError from None
