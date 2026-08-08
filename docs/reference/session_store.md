# `wreath.session_store`

Server-side session storage, so a session can be revoked and can outgrow a
cookie. Pass a store to `SessionPolicy`.

`delete_for` is the one method that reads *inside* a session payload, so it is
the one that has to know which key holds the principal. Set `session_key=` to
whatever `user_router(session_key=...)` and
`SessionIdentityBackend(session_key=...)` use; it defaults to `principal`, and
`wreath.users` passes its own key per call so a password reset is right even
when the store was not told.

::: wreath.session_store
