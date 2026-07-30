# Find the controls your tests do not watch

Your suite is green and your authorization tests pass. That tells you the checks
work. It does not tell you whether the tests would go red if somebody deleted
one — and for a control, deletion makes requests *succeed*, so a test written
around the happy path will keep passing either way.

One command asks the other question:

```bash
wreath mutant
```

It mutates whatever your project ships in `src/` (or your top-level packages)
and runs whatever `pytest` would have collected from `tests/`. No configuration,
no plugin, nothing written to your tree.

## Start narrow

A first whole-project run is slow to read, not just slow to finish. Point it at
the file you actually care about:

```bash
wreath mutant --path src/shop/policies.py --verbose
```

```
SURVIVED -- the control was removed and every test still passed

  src/shop/policies.py
    :34    the Cedar clause `when { principal.tenant == resource.tenant }`
           cedar.drop-condition in <module>; 22 test(s) ran it and none objected

KILLED -- the suite noticed

  src/shop/policies.py:31  a Cedar `forbid` turned into a `permit`
           caught by tests/test_policies.py::test_a_suspended_account_is_refused

wreath mutant: 6 killed, 1 survived, 0 unreached, 1 provably equivalent, …
```

That survivor is the finding. Twenty-two tests exercise the policy; none of them
puts a principal from one tenant in front of another tenant's resource, so
deleting the tenant clause changes nothing they assert. The fix is one test, not
one line of policy.

## Read the two lists differently

`SURVIVED` means the control ran and nothing objected — write the test.

`UNREACHED` means nothing in the suite ever executed the control. That is worse
and cheaper to fix: a refusal that has never fired is a refusal nobody has
watched work. Often the first test you write there fails immediately, because
the refusal was never right.

## Narrow further while you iterate

```bash
wreath mutant --operators cedar                 # policies only
wreath mutant --operators declaration           # what your call sites declare
wreath mutant --only 'orders.py:88'             # one mutant, after you fix it
wreath mutant --path src/shop --pytest-arg -x   # stop each mutant at first failure
```

`--only` takes any substring of a mutant's id, which is `operator@path:line`.
Copy one out of the report to re-run it after writing the test that should now
kill it.

## Keep a module clear once you have cleared it

The default is a report and exits 0, because a survivor is a question and most
projects have legitimate ones. When a module has no survivors you want kept, a
scoped run makes a fine CI step:

```bash
wreath mutant --path src/shop/policies.py --quiet --fail-on-survivor
```

Scope it to what you have actually cleared. A repository-wide
`--fail-on-survivor` fails on the first legitimate survivor and teaches everyone
to skip the step.

## What it will not find

`wreath mutant` mutates what your repository declares. A role name that lives in
a database row, a limit in an environment variable, a `.cedar` file read at
startup, a control enforced in Wreath's C ingress: none of those have a source
construct to remove, so none of them appear. See
[the guide](../../guides/mutant.md) for the full list, and read a clean report as
"none of the controls this tool can remove went unnoticed" rather than as
"every control is watched".
