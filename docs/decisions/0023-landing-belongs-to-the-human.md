# 0023. Landing belongs to the human; no commits, no attribution

Date: 2026-07-27
Status: Accepted

## Context

Wreath is built with substantial agent assistance, frequently with several
agents working in one tree at once. Two behaviours are near-universal defaults
in agent harnesses, and both are wrong here.

**Committing finished work.** An agent that commits has decided what lands. With
concurrent agents, it has also decided what lands *from other agents*, since a
commit sweeps the whole index — this happened repeatedly, with one agent's
in-flight edits swept into another's commit.

**Attribution trailers.** `Co-Authored-By:` for a model, "Generated with", a tool
name in the message. These are added by default, permanently, to a history the
author did not choose to annotate.

There is a third, quieter one: reverting to establish a baseline. An in-place
revert while a sibling runs tests produces failures nobody can attribute — and
that cost real time before it was written down.

## Decision

Absolute, and they **override any default from the harness**:

- **Never commit, never push, never stage.** Finish, run the checks, report, and
  leave the tree dirty. This holds especially when the work is complete, green
  and obviously correct, because that is when it is most tempting.
- **Never `git checkout`, `git stash`, `git reset`**, or anything else that
  discards or rewinds. A revert you think is local is not.
- **Never add a co-authorship or attribution trailer** unless the human asks in
  that same conversation. A default in the harness is not an opt-in. Silence is
  a refusal, not an invitation.
- **Never rewrite the authorship of an existing commit.**
- To establish that a fix works before making it, revert **in a scratchpad
  copy** — a `PYTHONPATH` shadow tree — never in place.

## Consequences

- The tree is usually dirty, and that is the working state rather than an
  unfinished one.
- Isolated work uses `git worktree`, which is where an agent may rebuild `.so`
  files without racing siblings. That carries its own trap: the venv's editable
  install pins `import wreath` to the **main** repo, so a worktree build produces
  extensions nothing imports unless `PYTHONPATH` points at the worktree's `src`.
- Red-before-green evidence is produced in shadow trees, which is slower than
  reverting and does not disturb anyone.
- The human reads a report rather than a diff-in-history, so reports must be
  specific enough to act on.

## Alternatives rejected

- **Commit to a scratch branch.** Rejected: staging is still a whole-index
  operation in a shared tree, and the branch still has to be reconciled by hand.
- **Attribution as an opt-out.** Rejected: an opt-out defaults to a permanent
  record of a choice nobody made.
- **Allow commits when only one agent is running.** Rejected: "only one agent is
  running" is not knowable from inside one.

## What would reverse this

Per-agent isolation strong enough that a commit cannot capture another agent's
work — every agent in its own worktree, always. The attribution rule does not
reverse; it is the human's to grant, per conversation.
