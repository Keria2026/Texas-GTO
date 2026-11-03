#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
release_output=$(realpath -m "${1:-artifacts/release}")
mkdir -p "$release_output"
release_stage=$(mktemp -d "$release_output/.build-XXXXXX")
trap 'rm -rf "$release_stage"' EXIT
mkdir -p "$release_stage/Serve/wheels"
uv build --wheel --out-dir "$release_stage/Serve/wheels"
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file "$release_stage/Serve/requirements.txt" >/dev/null
uv venv --seed "$release_stage/downloader"
"$release_stage/downloader/bin/python" -m pip download --disable-pip-version-check --require-hashes --only-binary=:all: \
  -r "$release_stage/Serve/requirements.txt" --dest "$release_stage/Serve/wheels"
cp deploy/install-serve.sh "$release_stage/Serve/install.sh"
cp deploy/README-Serve.md "$release_stage/Serve/README.md"
cp LICENSE "$release_stage/Serve/LICENSE"
npm --prefix Client run build
cp -r Client/dist "$release_stage/Client"
cp deploy/README-Client.md "$release_stage/Client/README.md"
python3 - "$release_stage" "$release_output" <<'PY'
import hashlib, json, pathlib, subprocess, sys, tarfile, zipfile
stage, output = map(pathlib.Path, sys.argv[1:])
manifest = json.loads((stage / 'Client/manifest.json').read_text())
assert manifest['name'] == 'TGTO Research Capture', 'Refusing to package a fixture build'
revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True))
build = {'commit': revision, 'dirty': dirty, 'client_version': manifest['version'],
         'serve_target': 'Ubuntu 24.04 x86_64, Python 3.12', 'quality': 'preview_unvalidated'}
for part in ('Serve', 'Client'):
    directory = stage / part
    (directory / 'BUILD.json').write_text(json.dumps(build, indent=2) + '\n')
    digests = []
    for path in sorted(directory.rglob('*')):
        if path.is_file():
            with path.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            digests.append(f'{digest}  {path.relative_to(directory).as_posix()}\n')
    (directory / 'SHA256SUMS').write_text(''.join(digests))
with tarfile.open(output / 'Serve.tar.gz', 'w:gz') as archive:
    archive.add(stage / 'Serve', arcname='Serve')
with zipfile.ZipFile(output / 'Client.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted((stage / 'Client').rglob('*')):
        if path.is_file():
            archive.write(path, path.relative_to(stage).as_posix())
for name in ('Serve.tar.gz', 'Client.zip'):
    with (output / name).open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    (output / f'{name}.sha256').write_text(f'{digest}  {name}\n')
print(json.dumps(build, indent=2))
PY
