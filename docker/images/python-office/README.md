# python-office image stack

Platform-managed base image for Motet’s **`python-office`** stack: LibreOffice,
Poppler, Ghostscript, pandoc, Node `docx`, and common Python document/PDF wheels.

## Use a prebuilt image (when available)

Releases may publish this stack as a convenience image. If your registry has
one, skip the build and pin it directly (prefer the digest):

```bash
MOTET_IMAGE_STACK_PYTHON_OFFICE=ghcr.io/motet-ai/python-office@sha256:...
```

## Build

From the repo root:

```bash
docker build -f docker/images/python-office/Dockerfile \
  -t motet/python-office:dev docker/images/python-office
```

The first build downloads LibreOffice apt layers and takes several minutes.

## Pin in Motet

Workers and the API resolve the stack from env:

```bash
MOTET_IMAGE_STACK_PYTHON_OFFICE=motet/python-office:dev
```

Add that to your local `.env` (or compose environment) for **worker** and **motet-api** services, then recreate those containers so they pick up the pin. The Docker daemon used for workspace / worker-exec must be able to see the same image tag (local `docker build` on the host is enough when the worker mounts the host Docker socket).

## Use with skills-demo

```bash
docker build -f docker/images/python-office/Dockerfile \
  -t motet/python-office:dev docker/images/python-office
# set MOTET_IMAGE_STACK_PYTHON_OFFICE=… and restart workers/API
./motet-sdk/examples/bundles/skills-demo/scripts/fetch-skills.sh
# set config/exec.yaml base_image_stack: python-office (see skills-demo README)
motet-cli deploy dir-deploy motet-sdk/examples/bundles/skills-demo
```

## Contents (summary)

| Layer | Packages |
|-------|----------|
| Apt | `libreoffice-{writer,calc,impress}`, `poppler-utils`, `ghostscript`, `pandoc`, `nodejs`/`npm`, `zip`/`unzip`, fonts |
| npm | `docx` under `/opt/motet/npm` (`NODE_PATH` set) |
| pip | see `requirements.txt` (`pypdf`, `python-docx`, `openpyxl`, `python-pptx`, …) |

Image size is large (LibreOffice). Prefer digest-pinned registry refs in production.

## Third-party notices

The image contains no Motet code. It aggregates unmodified Debian, PyPI, and
npm packages, including copyleft components (Ghostscript AGPL-3.0, Poppler
GPL-2.0, pandoc GPL-2.0+, LibreOffice MPL-2.0). Notices and exact package
manifests are baked into the image under `/usr/share/motet/` (see
[`NOTICES`](./NOTICES)); corresponding Debian source is available from
[snapshot.debian.org](https://snapshot.debian.org/).
