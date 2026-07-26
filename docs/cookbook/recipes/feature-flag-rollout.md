# Roll a feature out to a percentage of users

You want `new_checkout` in front of a quarter of your users — and you want each
user to stay on the *same* side of the line every request, not flicker between
the old and new path as they click around. Register the flag with a percentage
and gate your code on it:

```python
app.flags(new_checkout="25%")

flags = app.state.flags      # the registered FeatureFlags provider

if flags.enabled("new_checkout", {"id": user.id}):
    return new_checkout(request)
return old_checkout(request)
```

`app.flags(...)` registers a provider on `app.state.flags`. The bucket is
computed from the flag name and the subject in the context (`id`, `key`, `user`,
or `subject`) with blake2s — not a coin flip — so the same user lands the same
way across requests and across every worker process. Raise the percentage later
and the users already inside stay inside; you only ever add to the cohort.

The value is a small rule language: booleans as well as percentages.

```python
app.flags(new_checkout="25%", beta_ui=True, legacy_export="off")
```

`on`/`true`/`1`/`yes` enable, `off`/`false`/`0`/`""` disable, and `"25%"` is the
deterministic rollout. Pass no context and a percentage flag has no subject to
bucket, so it stays off — always give percentage flags an identifying context.

Call `app.flags()` with no arguments to build the provider from the environment
instead (`WREATH_FLAG_NEW_CHECKOUT=25%`), so ops can dial a rollout without a
deploy. Either way, `app.state.flags.enabled(name, context)` is how you read it.
