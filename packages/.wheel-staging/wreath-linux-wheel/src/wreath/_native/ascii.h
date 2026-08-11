/* ASCII case folding, and comparison under it.
 *
 * HTTP is case-insensitive in the places that matter -- header names, schemes,
 * hosts, transfer codings, `Cache-Control` directives -- and every module that
 * parses one of those had grown its own fold. `webpolicy.c`, `server_http2.c`
 * and `http3_asgi.c` each carried the same eleven-line compare under a different
 * name (`ascii_equal_ci`, `response_header_is`, `h3_response_header_is`), which
 * is three places to fix the next mistake in.
 *
 * They had already drifted once in a way that mattered: `normalize_origin` in
 * `webpolicy.c` folded with `<ctype.h>`'s `tolower`, which is **locale
 * dependent**. Under a Turkish locale `tolower('I')` is a dotless i, not `'i'`,
 * so a scheme or host would normalise differently depending on the environment
 * the process happened to start in -- while the copies beside it, doing the same
 * job, were locale-independent. `wreath_ascii_lower` is ASCII by construction
 * and cannot acquire a locale.
 *
 * Deliberately free of `Python.h`, like `simd.h` and `byteorder.h`, so any
 * translation unit can take it; lengths are `ptrdiff_t`, which is what
 * `Py_ssize_t` is.
 */
#ifndef WREATH_ASCII_H
#define WREATH_ASCII_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* `c | 0x20` would be shorter and is wrong: it also folds `@` to a backtick and
 * `[` to `{`, so a header name containing one would compare equal to a
 * different one. The range test is the whole reason this is a function. */
static inline uint8_t
wreath_ascii_lower(uint8_t c)
{
    return c >= 'A' && c <= 'Z' ? (uint8_t)(c + ('a' - 'A')) : c;
}

/* Whether two byte runs are equal once both are ASCII-folded. */
static inline int
wreath_ascii_equal_ci(const char *left, ptrdiff_t left_length,
                      const char *right, ptrdiff_t right_length)
{
    if (left_length != right_length) return 0;
    for (ptrdiff_t i = 0; i < left_length; i++) {
        if (wreath_ascii_lower((uint8_t)left[i]) !=
            wreath_ascii_lower((uint8_t)right[i])) return 0;
    }
    return 1;
}

/* The same, where the right side is a NUL-terminated literal. `strlen` on one is
 * constant-folded, so this costs a call site nothing and reads as what it is. */
static inline int
wreath_ascii_equal_ci_str(const char *data, ptrdiff_t length, const char *literal)
{
    return wreath_ascii_equal_ci(data, length, literal, (ptrdiff_t)strlen(literal));
}

#endif /* WREATH_ASCII_H */
