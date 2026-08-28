# hello-world

Minimal **command + tool** bundle for smoke-testing deploy and hot reload.

## Manifest

See **`manifest.yaml`** — `description` documents the intended use (simple `hello_world`-style command).

## Layout

- **`commands/`**: example distributed command(s)
- **`tools/`**: bundle-registered tool module(s)

## Typical use

Lint and deploy against a dev stack:

```bash
motet-cli bundle lint hello-world
motet-cli bundle hot-deploy ./hello-world
```

## Related

- Other examples: **`../README.md`**
