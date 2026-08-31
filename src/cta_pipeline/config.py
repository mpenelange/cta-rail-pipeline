import os
import re
from pathlib import Path

_NAME=re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def load_dotenv(path=".env",environ=None):
    """Load simple KEY=VALUE pairs without overriding the process environment."""
    environ=os.environ if environ is None else environ; path=Path(path)
    if not path.exists(): return False
    for number,line in enumerate(path.read_text(encoding="utf-8").splitlines(),1):
        stripped=line.strip()
        if not stripped or stripped.startswith("#"): continue
        if stripped.startswith("export "): stripped=stripped[7:].lstrip()
        if "=" not in stripped: raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        name,value=stripped.split("=",1); name=name.strip(); value=value.strip()
        if not _NAME.fullmatch(name): raise ValueError(f"{path}:{number}: invalid variable name")
        if value[:1] in ('\"',"'"):
            quote=value[0]
            if len(value)<2 or value[-1]!=quote: raise ValueError(f"{path}:{number}: unmatched quote")
            value=value[1:-1]
        environ.setdefault(name,value)
    return True
