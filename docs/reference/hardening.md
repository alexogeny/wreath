# `wreath.hardening`

The application's own defects, refused at startup instead of found later.

Wreath already knows how to recognise the defect classes a framework cannot
close on its author's behalf — that is the catalog behind
[`wreath audit code`](audit.md). What this module adds is that something
*runs* it: the audit happens when the application starts, in front of the person
who can fix it, and under `hardening="block"` a process carrying an error-level
finding does not come up at all.

The guide is [Hardening](../guides/hardening.md), which covers the three
policies and how to move a deployment from `warn` to `block`.

::: wreath.hardening
