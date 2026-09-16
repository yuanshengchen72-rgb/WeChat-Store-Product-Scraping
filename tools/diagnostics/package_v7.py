"""Build and verify a whitelist-only portable ZIP; exclude all user state."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import zipfile

sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).resolve().parents[2]
version = sys.argv[1] if len(sys.argv) > 1 else "v7"
if version not in {"v7", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "v16", "v17", "v18", "v19"}:
    raise ValueError("Unsupported release version")
release = root / f"build/desktop-recovery-{version}"
app = release / "微信小店商品列表采集"
release_date = "20260911" if version in {"v15", "v16", "v17", "v18", "v19"} else ("20260910" if version in {"v9", "v10", "v11", "v12", "v13", "v14"} else "20260909")
archive = root / f"dist/微信小店商品采集-{version}-{release_date}-Windows64-完整便携版.zip"
files = [app / "微信小店商品列表采集.exe", *sorted((app / "_internal").rglob("*"))]
files = [path for path in files if path.is_file()]
mapping = {str(Path(app.name) / path.relative_to(app)).replace("\\", "/"): path for path in files}
mapping[f"{app.name}/使用说明.txt"] = release / "使用说明.txt"
if archive.exists():
    raise RuntimeError("Refusing to overwrite an existing release archive")
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as target:
    for name, source in mapping.items():
        target.write(source, name)
check = Path(tempfile.mkdtemp(prefix=f"{version}-zip-selftest-", dir=root / "build"))
with zipfile.ZipFile(archive) as target:
    assert set(target.namelist()) == set(mapping)
    target.extractall(check)
for name, source in mapping.items():
    assert hashlib.sha256((check / name).read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest(), name
print(json.dumps({"archive": str(archive), "entries": len(mapping), "bytes": archive.stat().st_size,
                  "sha256": hashlib.sha256(archive.read_bytes()).hexdigest().upper(),
                  "verified_exe": str(check / app.name / "微信小店商品列表采集.exe")}, ensure_ascii=False))
