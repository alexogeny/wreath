/* Trusted-proxy address matching for ProxyHeadersMiddleware.
 *
 * The allow-list is compiled once into a flat array of packed prefixes, so a
 * request never touches a Python object inside a matching loop: parsing a hop,
 * testing it against every network, and rendering the winner are all C.
 *
 * The parsers are deliberately strict. Overriding scheme/host/client on a
 * forged header is a privilege escalation, so anything ambiguous is rejected
 * rather than guessed: leading zeros in IPv4 octets (which some resolvers read
 * as octal), zone identifiers, and any trailing junk all fail the parse.
 */
#include "wreathcore.h"

typedef struct {
    uint8_t prefix[16];
    uint8_t size; /* address width in bytes: 4 (IPv4) or 16 (IPv6) */
    uint8_t bits; /* significant prefix bits */
} WreathNetwork;

typedef struct {
    PyObject_HEAD
    WreathNetwork *nets;
    Py_ssize_t count;
} WreathTrustedNetworks;

/* Parse a dotted-quad IPv4 literal. Rejects leading zeros ("010.0.0.1"), short
 * forms ("10.1"), and trailing junk. Returns 1 on success. */
static int
parse_ipv4(const char *s, Py_ssize_t len, uint8_t out[4])
{
    Py_ssize_t i = 0;
    for (int part = 0; part < 4; part++) {
        int value = 0;
        int digits = 0;
        int leading_zero;
        if (part > 0) {
            if (i >= len || s[i] != '.') {
                return 0;
            }
            i++;
        }
        if (i >= len || s[i] < '0' || s[i] > '9') {
            return 0;
        }
        leading_zero = (s[i] == '0');
        while (i < len && s[i] >= '0' && s[i] <= '9') {
            value = value * 10 + (s[i] - '0');
            if (++digits > 3 || value > 255) {
                return 0;
            }
            i++;
        }
        if (leading_zero && digits > 1) {
            return 0;
        }
        out[part] = (uint8_t)value;
    }
    return i == len;
}

static int
hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Parse an IPv6 literal, including "::" compression and an embedded IPv4 tail
 * ("::ffff:192.0.2.1"). Zone identifiers ("fe80::1%eth0") are rejected: they
 * are meaningless for a forwarded hop and only add parser surface. */
static int
parse_ipv6(const char *s, Py_ssize_t len, uint8_t out[16])
{
    uint8_t buf[16];
    int filled = 0; /* bytes written so far, "::" not yet expanded */
    int gap = -1;   /* byte offset where "::" appeared, -1 when absent */
    Py_ssize_t i = 0;

    memset(buf, 0, sizeof(buf));
    if (len == 0) {
        return 0;
    }
    if (s[0] == ':') {
        if (len < 2 || s[1] != ':') {
            return 0;
        }
        gap = 0;
        i = 2;
        if (i == len) { /* "::" */
            memcpy(out, buf, 16);
            return 1;
        }
    }

    for (;;) {
        int value = 0;
        int digits = 0;
        Py_ssize_t start = i;
        while (i < len && hex_value(s[i]) >= 0) {
            value = value * 16 + hex_value(s[i]);
            if (++digits > 4) {
                return 0;
            }
            i++;
        }
        if (digits == 0) {
            return 0;
        }
        if (i < len && s[i] == '.') {
            /* An embedded IPv4 tail closes the address: re-parse the group we
             * just consumed as the tail's first octet. */
            uint8_t v4[4];
            if (filled > 12 || !parse_ipv4(s + start, len - start, v4)) {
                return 0;
            }
            memcpy(buf + filled, v4, 4);
            filled += 4;
            break;
        }
        if (filled > 14) {
            return 0;
        }
        wreath_store_u16_be(buf + filled, (uint16_t)value);
        filled += 2;
        if (i == len) {
            break;
        }
        if (s[i] != ':') {
            return 0;
        }
        i++;
        if (i == len) { /* a trailing single ':' is not an address */
            return 0;
        }
        if (s[i] == ':') {
            if (gap >= 0) { /* only one "::" may appear */
                return 0;
            }
            gap = filled;
            i++;
            if (i == len) {
                break;
            }
        }
    }

    if (gap >= 0) {
        int move = filled - gap;
        if (filled == 16) { /* "::" must stand for at least one zero group */
            return 0;
        }
        memmove(buf + 16 - move, buf + gap, (size_t)move);
        memset(buf + gap, 0, (size_t)(16 - move - gap));
    }
    else if (filled != 16) {
        return 0;
    }
    memcpy(out, buf, 16);
    return 1;
}

static const uint8_t V4_MAPPED_PREFIX[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xFF};

/* Parse an address literal into packed bytes. Returns the width (4 or 16), or
 * 0 when the text is not an address. An IPv4-mapped IPv6 address collapses to
 * its IPv4 form, so a dual-stack listener reporting "::ffff:10.0.0.1" matches
 * a configured 10.0.0.0/8 and keys a rate limiter identically to "10.0.0.1". */
static int
parse_ip(const char *s, Py_ssize_t len, uint8_t out[16])
{
    if (memchr(s, ':', (size_t)len) == NULL) {
        return parse_ipv4(s, len, out) ? 4 : 0;
    }
    if (!parse_ipv6(s, len, out)) {
        return 0;
    }
    if (memcmp(out, V4_MAPPED_PREFIX, sizeof(V4_MAPPED_PREFIX)) == 0) {
        memmove(out, out + 12, 4);
        return 4;
    }
    return 16;
}

/* Render packed bytes back to text: dotted quad, or RFC 5952 canonical IPv6
 * (lowercase, no leading zeros, longest zero run compressed). Callers hand the
 * result to logs and rate-limit keys, so a stable spelling matters. */
static PyObject *
render_ip(const uint8_t *ip, int size)
{
    char buf[46];
    char *p = buf;
    uint16_t groups[8];
    int best = -1;
    int best_len = 0;
    int run = -1;
    int run_len = 0;

    if (size == 4) {
        int n = snprintf(buf, sizeof(buf), "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
        return PyUnicode_DecodeASCII(buf, n, "strict");
    }
    for (int i = 0; i < 8; i++) {
        groups[i] = wreath_load_u16_be(ip + 2 * i);
    }
    for (int i = 0; i < 8; i++) {
        if (groups[i] != 0) {
            run = -1;
            run_len = 0;
            continue;
        }
        if (run < 0) {
            run = i;
            run_len = 1;
        }
        else {
            run_len++;
        }
        if (run_len > best_len) {
            best = run;
            best_len = run_len;
        }
    }
    if (best_len < 2) { /* a single zero group is written out, not compressed */
        best = -1;
    }
    for (int i = 0; i < 8; i++) {
        if (best >= 0 && i >= best && i < best + best_len) {
            if (i == best) {
                *p++ = ':';
            }
            if (i == best + best_len - 1) {
                *p++ = ':';
            }
            continue;
        }
        if (i > 0 && !(best >= 0 && i == best + best_len)) {
            *p++ = ':';
        }
        p += snprintf(p, 5, "%x", groups[i]);
    }
    return PyUnicode_DecodeASCII(buf, p - buf, "strict");
}

static int
prefix_match(const uint8_t *a, const uint8_t *b, int bits)
{
    int whole = bits >> 3;
    int rest = bits & 7;
    if (whole > 0 && memcmp(a, b, (size_t)whole) != 0) {
        return 0;
    }
    if (rest > 0) {
        uint8_t mask = (uint8_t)(0xFF << (8 - rest));
        if (((a[whole] ^ b[whole]) & mask) != 0) {
            return 0;
        }
    }
    return 1;
}

static int
networks_contain(const WreathTrustedNetworks *self, const uint8_t *ip, int size)
{
    for (Py_ssize_t i = 0; i < self->count; i++) {
        const WreathNetwork *net = &self->nets[i];
        if (net->size == size && prefix_match(ip, net->prefix, net->bits)) {
            return 1;
        }
    }
    return 0;
}

/* Parse "10.0.0.0/8", "2001:db8::/32", or a bare address (an implicit host
 * route). Returns 0 and sets an exception on bad input. */
static int
parse_cidr(const char *text, Py_ssize_t len, WreathNetwork *out)
{
    const char *slash = memchr(text, '/', (size_t)len);
    Py_ssize_t addr_len = slash != NULL ? slash - text : len;
    uint8_t packed[16];
    int size = parse_ip(text, addr_len, packed);
    int bits;

    if (size == 0) {
        PyErr_Format(PyExc_ValueError, "invalid trusted proxy network: %.*s", (int)len, text);
        return 0;
    }
    bits = size * 8;
    if (slash != NULL) {
        const char *cursor = slash + 1;
        const char *end = text + len;
        int value = 0;
        int digits = 0;
        while (cursor < end && *cursor >= '0' && *cursor <= '9') {
            value = value * 10 + (*cursor - '0');
            if (++digits > 3 || value > bits) {
                PyErr_Format(PyExc_ValueError, "invalid prefix length: %.*s", (int)len, text);
                return 0;
            }
            cursor++;
        }
        if (digits == 0 || cursor != end) {
            PyErr_Format(PyExc_ValueError, "invalid prefix length: %.*s", (int)len, text);
            return 0;
        }
        bits = value;
    }
    /* Reject host bits set outside the prefix ("10.1.2.3/8"): it is always a
     * configuration mistake, and silently masking it hides the intent. */
    for (int i = 0; i < size; i++) {
        int keep = bits - i * 8;
        uint8_t mask = keep >= 8 ? 0xFF : (keep <= 0 ? 0x00 : (uint8_t)(0xFF << (8 - keep)));
        if ((packed[i] & ~mask) != 0) {
            PyErr_Format(
                PyExc_ValueError, "network has host bits set: %.*s", (int)len, text);
            return 0;
        }
    }
    memcpy(out->prefix, packed, (size_t)size);
    out->size = (uint8_t)size;
    out->bits = (uint8_t)bits;
    return 1;
}

/* Strip an optional port and IPv6 brackets from one forwarded hop.
 * "[2001:db8::1]:443" -> "2001:db8::1", "192.0.2.1:443" -> "192.0.2.1".
 * A bare IPv6 hop carries multiple colons and never a port, so only a single
 * trailing colon group is treated as one. */
static int
hop_address(const uint8_t *hop, Py_ssize_t len, const char **start, Py_ssize_t *size)
{
    const char *text = (const char *)hop;
    if (len > 0 && text[0] == '[') {
        const char *close = memchr(text, ']', (size_t)len);
        Py_ssize_t rest;
        if (close == NULL) {
            return 0;
        }
        /* Only an optional ":port" may follow the bracket. Ignoring trailing
         * junk instead would let "[::1]anything" through. */
        rest = len - (close - text) - 1;
        if (rest > 0) {
            if (close[1] != ':' || rest < 2) {
                return 0;
            }
            for (Py_ssize_t i = 2; i < rest; i++) {
                if (close[i] < '0' || close[i] > '9') {
                    return 0;
                }
            }
        }
        *start = text + 1;
        *size = close - text - 1;
        return 1;
    }
    *start = text;
    *size = len;
    const void *first = memchr(text, ':', (size_t)len);
    if (first != NULL) {
        const char *colon = (const char *)first;
        if (memchr(colon + 1, ':', (size_t)(len - (colon - text) - 1)) == NULL) {
            *size = colon - text; /* exactly one colon: IPv4 with a port */
        }
    }
    return 1;
}

/*[clinic]
 * TrustedNetworks.contains(address: str) -> bool
 */
static PyObject *
networks_contains(WreathTrustedNetworks *self, PyObject *arg)
{
    uint8_t packed[16];
    Py_ssize_t len;
    const char *text;
    int size;

    if (!PyUnicode_Check(arg)) {
        PyErr_SetString(PyExc_TypeError, "address must be a string");
        return NULL;
    }
    text = PyUnicode_AsUTF8AndSize(arg, &len);
    if (text == NULL) {
        return NULL;
    }
    size = parse_ip(text, len, packed);
    if (size == 0) {
        Py_RETURN_FALSE;
    }
    return PyBool_FromLong(networks_contain(self, packed, size));
}

/* Resolve the client address from an X-Forwarded-For chain.
 *
 * The list reads left to right as client, proxy, proxy: each hop appends the
 * peer it heard from. Only the rightmost entries -- those written by proxies
 * we trust -- are believable, so we walk right to left and stop at the first
 * hop that is not a trusted proxy. That hop is the furthest-left address our
 * own infrastructure vouched for; everything left of it is client-supplied and
 * may be forged.
 *
 * Returns None when any hop fails to parse: a malformed chain means we cannot
 * tell where the trusted segment ends, and the caller keeps the real peer.
 */
static PyObject *
networks_forwarded_client(WreathTrustedNetworks *self, PyObject *arg)
{
    Py_buffer view;
    const uint8_t *data;
    Py_ssize_t end;
    uint8_t last[16];
    int last_size = 0;
    PyObject *result = NULL;

    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    data = view.buf;
    end = view.len;
    for (;;) {
        Py_ssize_t start = end;
        Py_ssize_t hs;
        Py_ssize_t he;
        const char *text;
        Py_ssize_t size;
        uint8_t packed[16];
        int width;

        while (start > 0 && data[start - 1] != ',') {
            start--;
        }
        hs = start;
        he = end;
        while (hs < he && (data[hs] == ' ' || data[hs] == '\t')) {
            hs++;
        }
        while (he > hs && (data[he - 1] == ' ' || data[he - 1] == '\t')) {
            he--;
        }
        if (hs == he || !hop_address(data + hs, he - hs, &text, &size)) {
            break; /* empty or unbracketed hop: refuse the whole chain */
        }
        width = parse_ip(text, size, packed);
        if (width == 0) {
            break; /* "unknown", an obfuscated token, or junk */
        }
        if (!networks_contain(self, packed, width)) {
            result = render_ip(packed, width);
            break;
        }
        memcpy(last, packed, (size_t)width);
        last_size = width;
        if (start == 0) {
            /* Every hop was a trusted proxy, so the leftmost one is itself the
             * client -- traffic that originated inside the trusted network. */
            result = render_ip(last, last_size);
            break;
        }
        end = start - 1;
    }
    PyBuffer_Release(&view);
    if (result == NULL && !PyErr_Occurred()) {
        Py_RETURN_NONE;
    }
    return result;
}

static void
networks_dealloc(WreathTrustedNetworks *self)
{
    PyMem_Free(self->nets);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
networks_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"networks", NULL};
    PyObject *networks;
    PyObject *sequence;
    WreathTrustedNetworks *self;
    Py_ssize_t count;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "O:TrustedNetworks", keywords, &networks)) {
        return NULL;
    }
    sequence = PySequence_Fast(networks, "networks must be an iterable of strings");
    if (sequence == NULL) {
        return NULL;
    }
    count = PySequence_Fast_GET_SIZE(sequence);
    self = (WreathTrustedNetworks *)type->tp_alloc(type, 0);
    if (self == NULL) {
        Py_DECREF(sequence);
        return NULL;
    }
    self->count = 0;
    self->nets = count > 0 ? PyMem_Calloc((size_t)count, sizeof(WreathNetwork)) : NULL;
    if (count > 0 && self->nets == NULL) {
        Py_DECREF(sequence);
        Py_DECREF(self);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *item = PySequence_Fast_GET_ITEM(sequence, i);
        Py_ssize_t len;
        const char *text;
        if (!PyUnicode_Check(item)) {
            PyErr_SetString(PyExc_TypeError, "networks must be an iterable of strings");
            Py_DECREF(sequence);
            Py_DECREF(self);
            return NULL;
        }
        text = PyUnicode_AsUTF8AndSize(item, &len);
        if (text == NULL || !parse_cidr(text, len, &self->nets[i])) {
            Py_DECREF(sequence);
            Py_DECREF(self);
            return NULL;
        }
        self->count = i + 1;
    }
    Py_DECREF(sequence);
    return (PyObject *)self;
}

static PyObject *
networks_get_count(WreathTrustedNetworks *self, void *closure)
{
    (void)closure;
    return PyLong_FromSsize_t(self->count);
}

static PyMethodDef networks_methods[] = {
    {"contains", (PyCFunction)networks_contains, METH_O,
     "contains(address) -> bool\n"
     "True when the textual address falls inside a trusted network."},
    {"forwarded_client", (PyCFunction)networks_forwarded_client, METH_O,
     "forwarded_client(value) -> str | None\n"
     "Rightmost X-Forwarded-For hop that is not a trusted proxy."},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef networks_getset[] = {
    {"count", (getter)networks_get_count, NULL, "number of compiled networks", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject TrustedNetworksType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.TrustedNetworks",
    .tp_doc = "Compiled trusted-proxy network allow-list (immutable).",
    .tp_basicsize = sizeof(WreathTrustedNetworks),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = networks_new,
    .tp_dealloc = (destructor)networks_dealloc,
    .tp_methods = networks_methods,
    .tp_getset = networks_getset,
};

int
wreath_register_proxy(PyObject *module)
{
    if (PyType_Ready(&TrustedNetworksType) < 0) {
        return -1;
    }
    return PyModule_AddObjectRef(module, "TrustedNetworks", (PyObject *)&TrustedNetworksType);
}
