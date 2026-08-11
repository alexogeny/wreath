"""`httpx.AsyncClient` and the requests made through one, into wreath's
`HTTPClient`."""

from __future__ import annotations

import ast

from .state import _EmitterState


class _HttpxRewrite(_EmitterState):
    def _rewrite_httpx_client(self, node: ast.Call) -> None:
        key = self._http_client_calls.get(id(node))
        if key is None:
            return
        parent = self._parents.get(id(node))
        if not isinstance(parent, ast.withitem):
            return
        name, _default_headers = self._http_clients[key]
        by_name = {keyword.arg: keyword.value for keyword in node.keywords}
        base_url = by_name.get("base_url")
        if base_url is None:
            dynamic = self._http_dynamic_clients.get(key)
            if dynamic is None:
                return
            statement = self._parents.get(id(parent))
            if not isinstance(statement, (ast.With, ast.AsyncWith)):
                return
            parts = self._fresh_name(f"_{name}_url")
            self._http_url_parts[key] = parts
            indent = self.buf.line_indent(statement.lineno)
            self.buf.insert_before_line(
                statement.lineno,
                f"{indent}{parts} = urlsplit(str({self._seg(dynamic)}))",
            )
            options = [
                "base_url=urlunsplit("
                f"({parts}.scheme, {parts}.netloc, '', '', ''))"
            ]
        else:
            options = [f"base_url={self._seg(base_url)}"]
        timeout = self._http_request_timeouts.get(key, by_name.get("timeout"))
        if timeout is not None:
            if isinstance(timeout, ast.Name) and timeout.id in self._http_timeout_constants:
                options.append(f"timeout={timeout.id}")
            else:
                total = self._http_timeout_value(timeout)
                if total is None:
                    return
                self.needs.add("ClientTimeout")
                options.append(f"timeout=ClientTimeout(total={total})")
        retries = self._http_retries.get(key)
        if retries is not None:
            self.needs.add("RetryPolicy")
            options.append(
                "retry=RetryPolicy("
                f"attempts=1 + ({self._seg(retries)}), "
                "idempotent_only=False, statuses=frozenset())"
            )
        self.needs.add("HTTPClient")
        self._resolve(node.lineno, "ext.httpx")
        self._replace_all_of(
            node,
            f"HTTPClient({name.replace('_', '-')!r}, {', '.join(options)})",
        )

    def _http_timeout_value(self, timeout: ast.expr) -> str | None:
        if not (
            isinstance(timeout, ast.Call)
            and self.imports.origin(timeout.func) == "httpx.Timeout"
        ):
            return self._seg(timeout)
        if timeout.args:
            return self._seg(timeout.args[0])
        value = next(
            (keyword.value for keyword in timeout.keywords if keyword.arg == "timeout"),
            None,
        )
        return self._seg(value) if value is not None else None

    def _http_timeout_total(self, call: ast.Call) -> str | None:
        """The single total deadline of an exact ``httpx.Timeout`` call."""
        if self.imports.origin(call.func) != "httpx.Timeout":
            return None
        if len(call.args) == 1 and not call.keywords:
            return self._seg(call.args[0])
        if call.args or len(call.keywords) != 1 or call.keywords[0].arg != "timeout":
            return None
        return self._seg(call.keywords[0].value)

    def _http_headers(self, value: ast.expr) -> str:
        source = self._seg(value)
        return (
            "tuple((str(_name).lower().encode('ascii'), "
            "str(_value).encode('latin-1')) "
            f"for _name, _value in ({source}).items())"
        )

    def _rewrite_httpx_request(self, node: ast.Call, key: int, method: str) -> None:
        client, default_headers = self._http_clients[key]
        if not node.args:
            return
        allowed = {"headers", "json", "content", "data", "params", "timeout"}
        if any(keyword.arg not in allowed for keyword in node.keywords):
            return
        target_index = 1 if method == "request" else 0
        if len(node.args) <= target_index:
            return
        args = [self._seg(argument) for argument in node.args[: target_index + 1]]
        if key in self._http_dynamic_clients:
            parts = self._http_url_parts.get(key)
            if parts is None:
                return
            args[target_index] = (
                "urlunsplit(('', '', "
                f"{parts}.path or '/', {parts}.query, ''))"
            )
        by_name = {keyword.arg: keyword.value for keyword in node.keywords}
        params = by_name.get("params")
        if params is not None:
            self.needs_urlencode = True
            args[target_index] = f"f'{{{args[target_index]}}}?{{urlencode({self._seg(params)})}}'"
        headers: list[str] = []
        if default_headers is not None:
            headers.append(f"*{self._http_headers(default_headers)}")
        request_headers = by_name.get("headers")
        if request_headers is not None:
            headers.append(f"*{self._http_headers(request_headers)}")
        body = None
        if (value := by_name.get("json")) is not None:
            self.needs.add("dumps")
            body = f"dumps({self._seg(value)})"
            headers.insert(0, "(b'content-type', b'application/json')")
        elif (value := by_name.get("data")) is not None:
            self.needs_urlencode = True
            body = f"urlencode({self._seg(value)}).encode('ascii')"
            headers.insert(0, "(b'content-type', b'application/x-www-form-urlencoded')")
        elif (value := by_name.get("content")) is not None:
            body = self._seg(value)
        keywords = []
        if headers:
            keywords.append(f"headers=({', '.join(headers)},)")
        if body is not None:
            keywords.append(f"body={body}")
        suffix = "" if not keywords else ", " + ", ".join(keywords)
        if method in {"get", "post", "request"}:
            call = f"{client}.{method}({', '.join(args)}{suffix})"
        else:
            target = args[target_index]
            call_args = [repr(method.upper()), target]
            call = f"{client}.request({', '.join(call_args)}{suffix})"
        self._replace_all_of(node, call)
