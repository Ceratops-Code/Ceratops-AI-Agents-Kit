# Deploy Managed Skills Action

## Goal

Deploy the exact manifest-managed skill batch without invoking repository
deployment operations or the independent bootstrap installer.

## Context

### Script Bundle

- Bind `<skill-root>` to the directory containing this action's parent
  `SKILL.md`; require `<skill-root>/scripts/runtime/install-managed-skills.py`
  before the first call. Invoke that exact path with the working directory
  equal to its `--repo-root` value; stop if absent and never resolve it relative
  to that repository.
- (D) Managed runtime transaction: `python
  "<skill-root>/scripts/runtime/install-managed-skills.py"
  --repo-root <repo-root> [--install-root <skills-root>]
  [--skill <name>...] [--remove-skill <name>...]
  [--base-revision <full-sha>] [--previous-runtime-source-id <id>]`
  from the installed or source lifecycle bundle.

### Inputs To Capture

- Source checkout, installation destination, and deployment mode; the source
  manifest supplies its profile.
- Exact selected or removed skills when the mode is not all-managed.
- Full base revision only for an explicitly requested affected-set deployment.
- For an authorized source-identity rename, bind `<id>` to the exact previous
  owner recorded in installed runtime manifests; the new owner comes from the
  source section manifest.

## Constraints

- Enter through `$ceratops-repo-lifecycle` for promotion or shipping; run its
  repository `deploy` operation first when declared.
- Never invoke `sdlc/sdlc.yml` or `scripts/deploy-skills.py` from
  this action.
- For SDLC, inspect the named skill's returned package prerequisites and
  verify the exact required artifact before installation. A package build
  action is separate from this managed skill transaction and is never inferred
  from the handoff alone.
- Bootstrap version metadata is not a managed-deployment input.

## Workflow

1. Resolve the requested source, destination and selection. Use all-managed by
   default, explicit selected/removed names, or one full base revision for an
   explicitly requested affected set. Pass a previous source identity only for
   the authorized ownership change described above.
2. Invoke the installer once through the shared runtime command. It owns request
   checks, the exact source-check scope, installation and its own runtime cleanup;
   do not run a separate source-check command first. Return its actual installation
   result to the caller. Selection or ownership decisions return to that caller
   without choosing wider scope.
3. Repository lifecycle owns its promotion report and completion cleanup. The
   installer takes no promotion report or repository-helper path, adds no report
   identity to its result and never calls back into repository lifecycle. Remaining
   repo work uses the normal repo entrypoint, without repeating installation.

## Done When

### Completion Gate

- The installer returned successful completion for the requested installation,
  removal or no-op; its source gate passed when installation was needed.
- Any retained retired folders or decision-required affected set is reported.

### Output Contract

Report only the deployment mode, deployed and removed skills, retained cleanup
debt, or the exact blocker.
