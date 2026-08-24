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

/* Nonzero for the RFC 9110 token characters: ALPHA / DIGIT / !#$%&'*+-.^_`|~
 *
 * Which octets a field name may carry decides whether two parsers in one
 * process agree about where a header ends, so `server.h` already argues this
 * rule has to live in exactly one place: HTTP/1.1 and HTTP/2 once disagreed,
 * and a disagreement is a request-splitting primitive for any downstream that
 * re-serializes to HTTP/1.1.
 *
 * It did not live in one place. `server_common.c` and the `http.c` head parser
 * each carried a byte-identical copy, and `_server` linked both -- so the
 * argument was written down beside one of two tables. Here it is one table,
 * and the file that already owns "every module had grown its own" is where it
 * belongs.
 *
 * `static const` rather than an `extern`: `http.c` is `#include`d into three
 * extensions rather than compiled once, so a single definition would be a
 * duplicate symbol in some links and a missing one in others. */
static const uint8_t wreath_ascii_token[256] = {
    ['!'] = 1, ['#'] = 1, ['$'] = 1, ['%'] = 1, ['&'] = 1, ['\''] = 1,
    ['*'] = 1, ['+'] = 1, ['-'] = 1, ['.'] = 1, ['^'] = 1, ['_'] = 1,
    ['`'] = 1, ['|'] = 1, ['~'] = 1,
    ['0'] = 1, ['1'] = 1, ['2'] = 1, ['3'] = 1, ['4'] = 1,
    ['5'] = 1, ['6'] = 1, ['7'] = 1, ['8'] = 1, ['9'] = 1,
    ['A'] = 1, ['B'] = 1, ['C'] = 1, ['D'] = 1, ['E'] = 1, ['F'] = 1,
    ['G'] = 1, ['H'] = 1, ['I'] = 1, ['J'] = 1, ['K'] = 1, ['L'] = 1,
    ['M'] = 1, ['N'] = 1, ['O'] = 1, ['P'] = 1, ['Q'] = 1, ['R'] = 1,
    ['S'] = 1, ['T'] = 1, ['U'] = 1, ['V'] = 1, ['W'] = 1, ['X'] = 1,
    ['Y'] = 1, ['Z'] = 1,
    ['a'] = 1, ['b'] = 1, ['c'] = 1, ['d'] = 1, ['e'] = 1, ['f'] = 1,
    ['g'] = 1, ['h'] = 1, ['i'] = 1, ['j'] = 1, ['k'] = 1, ['l'] = 1,
    ['m'] = 1, ['n'] = 1, ['o'] = 1, ['p'] = 1, ['q'] = 1, ['r'] = 1,
    ['s'] = 1, ['t'] = 1, ['u'] = 1, ['v'] = 1, ['w'] = 1, ['x'] = 1,
    ['y'] = 1, ['z'] = 1,
};

#endif /* WREATH_ASCII_H */
