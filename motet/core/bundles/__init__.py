"""
Motet - Bundle Lifecycle

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Everything that happens to a bundle between "someone pushed code" and "a
    worker can run it": fetch, validate, publish, deploy, propagate, hot-reload,
    roll back, unload, and (for exec artifacts) OCI image build.

    deploy.py the pipeline, deployer-worker side
    bundle_reload.py load/unload on AI workers, plus the SDK bridge
    bundle_image_build.py deployer-side OCI builds for exec artifacts

    Bundle lifecycle is its own package: these commands do not sequence turns,
    gather, or dispatch. They fetch, validate, publish, deploy, and reload
    bundles so workers can run them.

Dependencies:
    - motet.core.commands: the command framework (capabilities, data, responses)
    - motet.core.commands.decorator: `@distributed_command` and
      MotetContext. The decorator is framework, not orchestration.
    - motet.core.skills, motet.core.agents, motet.core.tools: registries that a
      bundle populates on load and clears on unload
    - motet.core.execution.image_stacks: base image resolution for OCI builds

Usage:
    Import the submodule you need; this package deliberately re-exports nothing.

        from motet.core.bundles.deploy import deploy_bundle, DeployBundleData
        from motet.core.bundles.bundle_reload import load_bundles_on_startup

Notes:
    - Kept import-light on purpose. `deploy` alone pulls in skills, agents, and
      the scheduling manager; re-exporting it here would mean every touch of
      `motet.core.bundles` paid for all of it.
    - Registration is explicit, not a side effect. `@distributed_command`
      registers on import, and these modules are imported by
      `DistributedCommand._ensure_commands_registered()`. Adding a new command
      module here without adding it there means workers reject the command type
      at runtime with "Unknown command type" — and no unit test will catch it.
"""
