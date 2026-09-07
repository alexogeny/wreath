# Parallel-work guidance

Read this before using Git worktrees or transferring patches between concurrent workers.


## Working in parallel worktrees

- **A worktree forks from `HEAD`, not from the working tree.** Uncommitted work
  in the main checkout is invisible to a new worktree, however green it is. If a
  directive claims a subsystem is present, verify it before building on it, and
  import what you need with a `diff` first to confirm nothing unrelated rode
  along.
- **`.plans/` is excluded from git**, so it does not exist in a fresh worktree.
  Copy in the plan you are working from.
- **`git apply` is atomic.** A failure on one file aborts the whole patch — the
  per-file "Applied patch to X cleanly" lines are the 3-way merge reporting
  progress, not a record of what survived. Re-check the tree rather than trusting
  the log.
