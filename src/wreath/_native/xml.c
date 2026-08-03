/* Strict XML parsing and exclusive canonicalization for wreath.xml.
 *
 * The profile, the refusal reasons, the refusal *messages* and the byte
 * offsets here are held byte for byte to the pure-Python twin in
 * wreath/_pure/xml.py by tests/test_xml_parity.py, over a corpus that includes
 * every exploit in tests/test_xml_refusals.py. Two implementations of one
 * parser is the shape that produces a signature-wrapping bug -- a verifier
 * running one and a consumer running the other -- so any divergence here is a
 * defect even when this side is the more permissive of the two.
 *
 * The tree is built directly as Python objects rather than into a private C
 * representation and converted after: the shapes are small (a SAML assertion
 * is kilobytes), and a second representation would be a third thing to keep in
 * step with the other two.
 */
#include "wreathcore.h"

/* Recursion is bounded by limits.max_depth, which wreath.xml.Limits caps at
 * this value precisely so a caller cannot ask for a nesting depth that
 * exhausts the C stack. Keep the two in step. */
#define XML_MAX_DEPTH_CEILING 1000

#define XML_NS_URI "http://www.w3.org/XML/1998/namespace"

typedef struct {
    Py_ssize_t max_bytes;
    Py_ssize_t max_depth;
    Py_ssize_t max_elements;
    Py_ssize_t max_attributes;
    Py_ssize_t max_attribute_bytes;
} XmlLimits;

typedef struct {
    const uint8_t *data;
    Py_ssize_t len;
    Py_ssize_t pos;
    Py_ssize_t elements;
    XmlLimits limits;
} XmlParser;

/* The wreath.xml.XMLRefusal class, handed over by xml_configure so this module
 * raises the same type the pure twin does rather than inventing a second one. */
static PyObject *xml_refusal_type = NULL;


/* Raise XMLRefusal(reason, message, offset). Always returns NULL so callers
 * can `return xml_fail(...)`. */
static void *
xml_fail(const char *reason, Py_ssize_t offset, const char *format, ...)
{
    PyObject *message;
    PyObject *exception;
    va_list args;

    if (xml_refusal_type == NULL) {
        PyErr_SetString(PyExc_RuntimeError,
                        "wreath._native._core.xml_configure was never called");
        return NULL;
    }
    va_start(args, format);
    message = PyUnicode_FromFormatV(format, args);
    va_end(args);
    if (message == NULL) {
        return NULL;
    }
    exception = PyObject_CallFunction(
        xml_refusal_type, "sOn", reason, message, offset);
    Py_DECREF(message);
    if (exception != NULL) {
        PyErr_SetObject(xml_refusal_type, exception);
        Py_DECREF(exception);
    }
    return NULL;
}


static int
xml_is_space(uint8_t byte)
{
    return byte == 0x20 || byte == 0x09 || byte == 0x0D || byte == 0x0A;
}


/* XML 1.0 fifth edition NameStartChar. Spelled out rather than approximated so
 * the pure twin can implement exactly these ranges. */
static int
xml_is_name_start(uint32_t cp)
{
    if (cp < 0x80) {
        return cp == ':' || cp == '_' || (cp >= 'A' && cp <= 'Z') ||
               (cp >= 'a' && cp <= 'z');
    }
    return (cp >= 0xC0 && cp <= 0xD6) || (cp >= 0xD8 && cp <= 0xF6) ||
           (cp >= 0xF8 && cp <= 0x2FF) || (cp >= 0x370 && cp <= 0x37D) ||
           (cp >= 0x37F && cp <= 0x1FFF) || (cp >= 0x200C && cp <= 0x200D) ||
           (cp >= 0x2070 && cp <= 0x218F) || (cp >= 0x2C00 && cp <= 0x2FEF) ||
           (cp >= 0x3001 && cp <= 0xD7FF) || (cp >= 0xF900 && cp <= 0xFDCF) ||
           (cp >= 0xFDF0 && cp <= 0xFFFD) || (cp >= 0x10000 && cp <= 0xEFFFF);
}


static int
xml_is_name_char(uint32_t cp)
{
    if (cp < 0x80) {
        return xml_is_name_start(cp) || cp == '-' || cp == '.' ||
               (cp >= '0' && cp <= '9');
    }
    if (cp == 0xB7) {
        return 1;
    }
    return xml_is_name_start(cp) || (cp >= 0x300 && cp <= 0x36F) ||
           (cp >= 0x203F && cp <= 0x2040);
}


/* Codepoint and width at `index`; the buffer has already been validated. */
static uint32_t
xml_decode_at(XmlParser *parser, Py_ssize_t index, int *width)
{
    const uint8_t *data = parser->data;
    uint8_t byte = data[index];
    if (byte < 0x80) {
        *width = 1;
        return byte;
    }
    if (byte < 0xE0) {
        *width = 2;
        return (uint32_t)((byte & 0x1F) << 6) | (uint32_t)(data[index + 1] & 0x3F);
    }
    if (byte < 0xF0) {
        *width = 3;
        return (uint32_t)((byte & 0x0F) << 12) |
               (uint32_t)((data[index + 1] & 0x3F) << 6) |
               (uint32_t)(data[index + 2] & 0x3F);
    }
    *width = 4;
    return (uint32_t)((byte & 0x07) << 18) |
           (uint32_t)((data[index + 1] & 0x3F) << 12) |
           (uint32_t)((data[index + 2] & 0x3F) << 6) |
           (uint32_t)(data[index + 3] & 0x3F);
}


/* UTF-8 well-formedness and the legal XML character range, in one pass before
 * anything else looks at the buffer. Doing it up front means no later stage
 * can be handed bytes that decode two ways. */
static int
xml_validate_bytes(XmlParser *parser)
{
    const uint8_t *data = parser->data;
    Py_ssize_t length = parser->len;
    Py_ssize_t i = 0;

    while (i < length) {
        uint8_t byte = data[i];
        int width;
        uint32_t cp;
        uint32_t lowest;

        if (byte < 0x80) {
            if (byte < 0x20 && byte != 0x09 && byte != 0x0A && byte != 0x0D) {
                xml_fail("control-character", i,
                         "control character 0x%02X is not permitted in XML", byte);
                return -1;
            }
            i++;
            continue;
        }
        if (byte >= 0xC2 && byte <= 0xDF) {
            width = 2;
            cp = byte & 0x1Fu;
        }
        else if (byte >= 0xE0 && byte <= 0xEF) {
            width = 3;
            cp = byte & 0x0Fu;
        }
        else if (byte >= 0xF0 && byte <= 0xF4) {
            width = 4;
            cp = byte & 0x07u;
        }
        else {
            xml_fail("encoding", i,
                     "byte 0x%02X does not start a valid UTF-8 sequence", byte);
            return -1;
        }
        if (i + width > length) {
            xml_fail("encoding", i, "the document ends inside a UTF-8 sequence");
            return -1;
        }
        for (int offset = 1; offset < width; offset++) {
            uint8_t cont = data[i + offset];
            if ((cont & 0xC0) != 0x80) {
                xml_fail("encoding", i + offset,
                         "byte 0x%02X is not a UTF-8 continuation byte", cont);
                return -1;
            }
            cp = (cp << 6) | (uint32_t)(cont & 0x3F);
        }
        lowest = width == 2 ? 0x80u : (width == 3 ? 0x800u : 0x10000u);
        if (cp < lowest) {
            xml_fail("encoding", i,
                     "U+%04X is written as an overlong UTF-8 sequence", cp);
            return -1;
        }
        if (cp >= 0xD800 && cp <= 0xDFFF) {
            xml_fail("encoding", i,
                     "U+%04X is a surrogate and cannot appear in UTF-8", cp);
            return -1;
        }
        if (cp > 0x10FFFF || cp == 0xFFFE || cp == 0xFFFF) {
            xml_fail("encoding", i, "U+%04X is not a valid XML character", cp);
            return -1;
        }
        i += width;
    }
    return 0;
}


static void
xml_skip_space(XmlParser *parser)
{
    while (parser->pos < parser->len && xml_is_space(parser->data[parser->pos])) {
        parser->pos++;
    }
}


/* Read one XML Name, returning it as a new str. */
static PyObject *
xml_read_name(XmlParser *parser)
{
    Py_ssize_t start = parser->pos;
    int first = 1;

    while (parser->pos < parser->len) {
        uint8_t byte = parser->data[parser->pos];
        int width;
        uint32_t cp;

        if (byte < 0x80) {
            cp = byte;
            width = 1;
            if (!(first ? xml_is_name_start(cp) : xml_is_name_char(cp))) {
                break;
            }
        }
        else {
            cp = xml_decode_at(parser, parser->pos, &width);
            if (!(first ? xml_is_name_start(cp) : xml_is_name_char(cp))) {
                return xml_fail("invalid-name", parser->pos,
                                "U+%04X cannot appear in an XML name", cp);
            }
        }
        first = 0;
        parser->pos += width;
    }
    if (parser->pos == start) {
        return xml_fail("invalid-name", parser->pos,
                        "an XML name was expected here");
    }
    return PyUnicode_DecodeUTF8((const char *)parser->data + start,
                                parser->pos - start, "strict");
}


/* Split `name` into prefix and local part. Both outputs are new references. */
static int
xml_split_name(XmlParser *parser, PyObject *name, PyObject **prefix, PyObject **local)
{
    Py_ssize_t size;
    const char *utf8 = PyUnicode_AsUTF8AndSize(name, &size);
    const char *colon;

    if (utf8 == NULL) {
        return -1;
    }
    colon = memchr(utf8, ':', (size_t)size);
    if (colon == NULL) {
        *prefix = PyUnicode_FromString("");
        *local = Py_NewRef(name);
        return *prefix == NULL ? -1 : 0;
    }
    {
        Py_ssize_t head = colon - utf8;
        Py_ssize_t tail = size - head - 1;
        if (tail <= 0 || memchr(colon + 1, ':', (size_t)tail) != NULL) {
            xml_fail("invalid-name", parser->pos,
                     "%R is not a valid qualified name", name);
            return -1;
        }
        *prefix = PyUnicode_DecodeUTF8(utf8, head, "strict");
        if (*prefix == NULL) {
            return -1;
        }
        *local = PyUnicode_DecodeUTF8(colon + 1, tail, "strict");
        if (*local == NULL) {
            Py_CLEAR(*prefix);
            return -1;
        }
    }
    return 0;
}


/* Consume one `&...;` and append what it stands for to `parts`. */
static int
xml_read_reference(XmlParser *parser, PyObject *parts)
{
    const uint8_t *data = parser->data;
    Py_ssize_t start = parser->pos;
    PyObject *piece;

    parser->pos++;
    if (parser->pos < parser->len && data[parser->pos] == '#') {
        Py_ssize_t digits_start;
        int hexadecimal;
        unsigned long cp = 0;
        int any = 0;

        parser->pos++;
        hexadecimal = parser->pos < parser->len &&
                      (data[parser->pos] == 'x' || data[parser->pos] == 'X');
        if (hexadecimal) {
            parser->pos++;
        }
        digits_start = parser->pos;
        while (parser->pos < parser->len && data[parser->pos] != ';') {
            parser->pos++;
        }
        if (parser->pos >= parser->len) {
            xml_fail("character-reference", start,
                     "the document ends inside a character reference");
            return -1;
        }
        for (Py_ssize_t i = digits_start; i < parser->pos; i++) {
            uint8_t digit = data[i];
            unsigned long value;
            if (digit >= '0' && digit <= '9') {
                value = (unsigned long)(digit - '0');
            }
            else if (hexadecimal && digit >= 'a' && digit <= 'f') {
                value = (unsigned long)(digit - 'a' + 10);
            }
            else if (hexadecimal && digit >= 'A' && digit <= 'F') {
                value = (unsigned long)(digit - 'A' + 10);
            }
            else {
                any = 0;
                break;
            }
            if (cp > 0x7FFFFFFUL) {   /* saturate rather than wrap */
                cp = 0x7FFFFFFUL;
            }
            else {
                cp = cp * (hexadecimal ? 16UL : 10UL) + value;
            }
            any = 1;
        }
        if (!any) {
            PyObject *digits = PyUnicode_DecodeUTF8(
                (const char *)data + digits_start, parser->pos - digits_start,
                "replace");
            if (digits == NULL) {
                return -1;
            }
            xml_fail("character-reference", start,
                     "%R is not a valid character reference", digits);
            Py_DECREF(digits);
            return -1;
        }
        parser->pos++;
        if (cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF) ||
            (cp < 0x20 && cp != 0x09 && cp != 0x0A && cp != 0x0D) ||
            cp == 0xFFFE || cp == 0xFFFF) {
            xml_fail("character-reference", start,
                     "character reference U+%04X is not a valid XML character",
                     (unsigned int)cp);
            return -1;
        }
        piece = PyUnicode_FromOrdinal((int)cp);
        if (piece == NULL) {
            return -1;
        }
        if (PyList_Append(parts, piece) < 0) {
            Py_DECREF(piece);
            return -1;
        }
        Py_DECREF(piece);
        return 0;
    }
    {
        const uint8_t *semicolon = memchr(data + parser->pos, ';',
                                          (size_t)(parser->len - parser->pos));
        Py_ssize_t name_len;
        const char *name;
        const char *expansion = NULL;

        if (semicolon == NULL) {
            xml_fail("entity-reference", start,
                     "the document ends inside an entity reference");
            return -1;
        }
        name = (const char *)data + parser->pos;
        name_len = semicolon - (data + parser->pos);
        if (name_len == 2 && memcmp(name, "lt", 2) == 0) {
            expansion = "<";
        }
        else if (name_len == 2 && memcmp(name, "gt", 2) == 0) {
            expansion = ">";
        }
        else if (name_len == 3 && memcmp(name, "amp", 3) == 0) {
            expansion = "&";
        }
        else if (name_len == 4 && memcmp(name, "quot", 4) == 0) {
            expansion = "\"";
        }
        else if (name_len == 4 && memcmp(name, "apos", 4) == 0) {
            expansion = "'";
        }
        if (expansion == NULL) {
            PyObject *label = PyUnicode_DecodeUTF8(name, name_len, "replace");
            if (label == NULL) {
                return -1;
            }
            parser->pos = (semicolon - data) + 1;
            xml_fail("entity-reference", start,
                     "entity reference &%U; is not one of the five XML "
                     "predefined entities, and this parser declares none", label);
            Py_DECREF(label);
            return -1;
        }
        parser->pos = (semicolon - data) + 1;
        piece = PyUnicode_FromString(expansion);
        if (piece == NULL) {
            return -1;
        }
        if (PyList_Append(parts, piece) < 0) {
            Py_DECREF(piece);
            return -1;
        }
        Py_DECREF(piece);
    }
    return 0;
}


/* Append data[run:stop] to `parts` as a str. */
static int
xml_flush_run(XmlParser *parser, PyObject *parts, Py_ssize_t run, Py_ssize_t stop)
{
    PyObject *piece;
    if (stop <= run) {
        return 0;
    }
    piece = PyUnicode_DecodeUTF8((const char *)parser->data + run, stop - run,
                                 "strict");
    if (piece == NULL) {
        return -1;
    }
    if (PyList_Append(parts, piece) < 0) {
        Py_DECREF(piece);
        return -1;
    }
    Py_DECREF(piece);
    return 0;
}


static PyObject *
xml_join(PyObject *parts)
{
    PyObject *empty = PyUnicode_FromString("");
    PyObject *joined;
    if (empty == NULL) {
        return NULL;
    }
    joined = PyUnicode_Join(empty, parts);
    Py_DECREF(empty);
    return joined;
}


static PyObject *
xml_read_attribute_value(XmlParser *parser)
{
    const uint8_t *data = parser->data;
    uint8_t quote = parser->pos < parser->len ? data[parser->pos] : 0;
    PyObject *parts;
    PyObject *value;
    Py_ssize_t start;
    Py_ssize_t run;

    if (quote != '"' && quote != '\'') {
        return xml_fail("attribute-syntax", parser->pos,
                        "an attribute value must be quoted");
    }
    parser->pos++;
    start = parser->pos;
    run = parser->pos;
    parts = PyList_New(0);
    if (parts == NULL) {
        return NULL;
    }
    for (;;) {
        uint8_t byte;
        if (parser->pos >= parser->len) {
            Py_DECREF(parts);
            return xml_fail("unexpected-end", start,
                            "the document ends inside an attribute value");
        }
        byte = data[parser->pos];
        if (byte == quote) {
            if (xml_flush_run(parser, parts, run, parser->pos) < 0) {
                Py_DECREF(parts);
                return NULL;
            }
            parser->pos++;
            break;
        }
        if (byte == '<') {
            Py_DECREF(parts);
            return xml_fail("attribute-syntax", parser->pos,
                            "'<' is not permitted in an attribute value");
        }
        if (byte == '&') {
            if (xml_flush_run(parser, parts, run, parser->pos) < 0 ||
                xml_read_reference(parser, parts) < 0) {
                Py_DECREF(parts);
                return NULL;
            }
            run = parser->pos;
            continue;
        }
        if (byte == 0x09 || byte == 0x0A || byte == 0x0D) {
            /* Attribute-value normalization: literal whitespace becomes a
             * space, and a character reference does not -- which is the whole
             * reason the escape exists. */
            PyObject *space;
            if (xml_flush_run(parser, parts, run, parser->pos) < 0) {
                Py_DECREF(parts);
                return NULL;
            }
            space = PyUnicode_FromString(" ");
            if (space == NULL || PyList_Append(parts, space) < 0) {
                Py_XDECREF(space);
                Py_DECREF(parts);
                return NULL;
            }
            Py_DECREF(space);
            parser->pos++;
            if (byte == 0x0D && parser->pos < parser->len &&
                data[parser->pos] == 0x0A) {
                parser->pos++;
            }
            run = parser->pos;
            continue;
        }
        parser->pos++;
    }
    value = xml_join(parts);
    Py_DECREF(parts);
    if (value == NULL) {
        return NULL;
    }
    if (parser->pos - start > parser->limits.max_attribute_bytes) {
        Py_DECREF(value);
        return xml_fail("attribute-size", start,
                        "attribute value exceeds the %zd-byte limit",
                        parser->limits.max_attribute_bytes);
    }
    return value;
}


/* Refuse everything spelled `<!...` or `<?...` at the current position. */
static int
xml_reject_markup_declaration(XmlParser *parser)
{
    const uint8_t *rest = parser->data + parser->pos;
    Py_ssize_t available = parser->len - parser->pos;

    if (available >= 4 && memcmp(rest, "<!--", 4) == 0) {
        xml_fail("comment", parser->pos,
                 "comments are refused: a comment splits a text node, and two "
                 "readings of one value is how a signed assertion is truncated");
        return -1;
    }
    if (available >= 9 && memcmp(rest, "<![CDATA[", 9) == 0) {
        xml_fail("cdata", parser->pos,
                 "CDATA sections are refused: they are a second spelling of "
                 "text, and one value with two spellings is an ambiguity a "
                 "signature cannot resolve");
        return -1;
    }
    if (available >= 9 && memcmp(rest, "<!DOCTYPE", 9) == 0) {
        xml_fail("doctype", parser->pos,
                 "a document type declaration is refused: it is the only way "
                 "to declare an entity, and therefore the only way to reach an "
                 "expander or an external resolver");
        return -1;
    }
    if (available >= 2 && memcmp(rest, "<!", 2) == 0) {
        xml_fail("markup-declaration", parser->pos,
                 "markup declarations are refused");
        return -1;
    }
    if (available >= 2 && memcmp(rest, "<?", 2) == 0) {
        xml_fail("processing-instruction", parser->pos,
                 "processing instructions are refused; only an XML declaration "
                 "at the start of the document is accepted");
        return -1;
    }
    return 0;
}


/* Read one pseudo-attribute out of the XML declaration body. Returns a new
 * str, or NULL with no exception set when absent. */
static PyObject *
xml_pseudo_attribute(const char *body, Py_ssize_t size, const char *name)
{
    Py_ssize_t name_len = (Py_ssize_t)strlen(name);
    const char *found = NULL;
    Py_ssize_t i = 0;
    char quote;
    const char *close;

    while (i + name_len <= size) {
        if (memcmp(body + i, name, (size_t)name_len) == 0) {
            found = body + i + name_len;
            break;
        }
        i++;
    }
    if (found == NULL) {
        return NULL;
    }
    while (found < body + size && xml_is_space((uint8_t)*found)) {
        found++;
    }
    if (found >= body + size || *found != '=') {
        return NULL;
    }
    found++;
    while (found < body + size && xml_is_space((uint8_t)*found)) {
        found++;
    }
    if (found >= body + size || (*found != '"' && *found != '\'')) {
        return NULL;
    }
    quote = *found;
    found++;
    close = memchr(found, quote, (size_t)(body + size - found));
    if (close == NULL) {
        return NULL;
    }
    return PyUnicode_DecodeUTF8(found, close - found, "replace");
}


static int
xml_read_declaration(XmlParser *parser)
{
    const uint8_t *data = parser->data;
    const uint8_t *terminator;
    PyObject *version;
    PyObject *encoding;
    Py_ssize_t body_len;

    if (parser->len < 5 || memcmp(data, "<?xml", 5) != 0) {
        return 0;
    }
    if (parser->len > 5 && !xml_is_space(data[5])) {
        return 0;   /* `<?xmlfoo` is a processing instruction, refused later */
    }
    /* wreath_memmem, not a hand-rolled scan: it is already written,
     * already differentially tested, and already dispatches per call. */
    terminator = wreath_memmem(data + 5, parser->len - 5,
                               (const uint8_t *)"?>", 2);
    if (terminator == NULL) {
        xml_fail("unexpected-end", 0, "the XML declaration is not terminated");
        return -1;
    }
    body_len = terminator - (data + 5);
    parser->pos = (terminator - data) + 2;

    version = xml_pseudo_attribute((const char *)data + 5, body_len, "version");
    if (version == NULL && PyErr_Occurred()) {
        return -1;
    }
    if (version == NULL || PyUnicode_CompareWithASCIIString(version, "1.0") != 0) {
        PyObject *shown = version != NULL ? version : Py_NewRef(Py_None);
        xml_fail("version", 0,
                 "XML version %R is not supported; this parser is XML 1.0", shown);
        Py_DECREF(shown);
        return -1;
    }
    Py_DECREF(version);

    encoding = xml_pseudo_attribute((const char *)data + 5, body_len, "encoding");
    if (encoding == NULL) {
        return PyErr_Occurred() ? -1 : 0;
    }
    {
        PyObject *lowered = PyObject_CallMethod(encoding, "lower", NULL);
        int mismatch;
        if (lowered == NULL) {
            Py_DECREF(encoding);
            return -1;
        }
        mismatch = PyUnicode_CompareWithASCIIString(lowered, "utf-8") != 0;
        Py_DECREF(lowered);
        if (mismatch) {
            xml_fail("encoding", 0,
                     "declared encoding %R is refused; this parser reads UTF-8",
                     encoding);
            Py_DECREF(encoding);
            return -1;
        }
    }
    Py_DECREF(encoding);
    return 0;
}


/* Resolve `prefix` against `scope`. Returns a new reference to the URI str. */
/* Expand `prefix` against `scope`.
 *
 * Only ever called with an empty prefix for an *element* name: an unprefixed
 * attribute is in no namespace, so the caller answers that case without
 * asking. An `element` flag used to select between the two here and was dead
 * on one arm. */
static PyObject *
xml_resolve(XmlParser *parser, PyObject *prefix, PyObject *scope,
            Py_ssize_t offset)
{
    PyObject *uri;

    if (PyUnicode_CompareWithASCIIString(prefix, "xml") == 0) {
        return PyUnicode_FromString(XML_NS_URI);
    }
    if (PyUnicode_GET_LENGTH(prefix) == 0) {
        uri = PyDict_GetItemWithError(scope, prefix);
        if (uri == NULL) {
            return PyErr_Occurred() ? NULL : PyUnicode_FromString("");
        }
        return Py_NewRef(uri);
    }
    uri = PyDict_GetItemWithError(scope, prefix);
    if (uri == NULL) {
        if (PyErr_Occurred()) {
            return NULL;
        }
        return xml_fail("unbound-prefix", offset,
                        "unbound namespace prefix %R", prefix);
    }
    return Py_NewRef(uri);
}


static PyObject *xml_read_element(XmlParser *parser, PyObject *inherited,
                                  Py_ssize_t depth);


/* Read element content up to the matching end tag.
 *
 * `text_out` receives this element's leading character data; each child tuple
 * is appended to `children` with its tail already folded in. */
static int
xml_read_content(XmlParser *parser, PyObject *name, PyObject *scope,
                 Py_ssize_t depth, PyObject **text_out, PyObject *children)
{
    const uint8_t *data = parser->data;
    PyObject *text_parts = PyList_New(0);
    PyObject *target;
    Py_ssize_t run = parser->pos;
    Py_ssize_t pending_index = -1;
    int status = -1;

    if (text_parts == NULL) {
        return -1;
    }
    target = text_parts;   /* borrowed; switches to a tail list after a child */

    for (;;) {
        uint8_t byte;
        if (parser->pos >= parser->len) {
            xml_fail("mismatched-end-tag", parser->pos,
                     "the document ends before </%U>", name);
            goto done;
        }
        byte = data[parser->pos];
        if (byte == '&') {
            if (xml_flush_run(parser, target, run, parser->pos) < 0 ||
                xml_read_reference(parser, target) < 0) {
                goto done;
            }
            run = parser->pos;
            continue;
        }
        if (byte == 0x0D) {
            PyObject *newline;
            if (xml_flush_run(parser, target, run, parser->pos) < 0) {
                goto done;
            }
            newline = PyUnicode_FromString("\n");
            if (newline == NULL || PyList_Append(target, newline) < 0) {
                Py_XDECREF(newline);
                goto done;
            }
            Py_DECREF(newline);
            parser->pos++;
            if (parser->pos < parser->len && data[parser->pos] == 0x0A) {
                parser->pos++;
            }
            run = parser->pos;
            continue;
        }
        if (byte != '<') {
            parser->pos++;
            continue;
        }
        if (xml_flush_run(parser, target, run, parser->pos) < 0) {
            goto done;
        }
        if (parser->len - parser->pos >= 2 &&
            memcmp(data + parser->pos, "</", 2) == 0) {
            Py_ssize_t closing = parser->pos;
            PyObject *end_name;
            int same;

            parser->pos += 2;
            end_name = xml_read_name(parser);
            if (end_name == NULL) {
                goto done;
            }
            xml_skip_space(parser);
            if (parser->pos >= parser->len || data[parser->pos] != '>') {
                Py_DECREF(end_name);
                xml_fail("tag-syntax", parser->pos,
                         "an end tag must finish with '>'");
                goto done;
            }
            parser->pos++;
            same = PyUnicode_Compare(end_name, name) == 0;
            if (PyErr_Occurred()) {
                Py_DECREF(end_name);
                goto done;
            }
            if (!same) {
                xml_fail("mismatched-end-tag", closing,
                         "end tag </%U> does not match <%U>", end_name, name);
                Py_DECREF(end_name);
                goto done;
            }
            Py_DECREF(end_name);
            break;
        }
        if (xml_reject_markup_declaration(parser) < 0) {
            goto done;
        }
        {
            PyObject *child = xml_read_element(parser, scope, depth + 1);
            PyObject *tail_parts;
            if (child == NULL) {
                goto done;
            }
            if (PyList_Append(children, child) < 0) {
                Py_DECREF(child);
                goto done;
            }
            Py_DECREF(child);
            tail_parts = PyList_New(0);
            if (tail_parts == NULL) {
                goto done;
            }
            /* The child's tail is character data that follows it, so it is not
             * known until the next sibling or this element's end tag. Park the
             * accumulating list in the child tuple's tail slot and join it in
             * place once the content ends.
             *
             * Mutating a tuple is only legal because this one was built here
             * and has not been handed to anything else yet -- it is reachable
             * only through `children`, which this function owns. */
            pending_index = PyList_GET_SIZE(children) - 1;
            {
                PyObject *slot = PyList_GET_ITEM(children, pending_index);
                PyObject *previous = PyTuple_GET_ITEM(slot, 3);
                PyTuple_SET_ITEM(slot, 3, tail_parts);   /* steals tail_parts */
                Py_DECREF(previous);
            }
            target = tail_parts;   /* borrowed from the tuple slot */
            run = parser->pos;
        }
    }

    /* No flush here: the run was already flushed on reaching the '<' that
     * began the end tag, and parser->pos has since moved past it. Flushing
     * again would append the character data a second time with the end tag
     * itself trailing it. */

    /* Fold every parked tail list into the str it stands for. */
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(children); i++) {
        PyObject *child = PyList_GET_ITEM(children, i);
        PyObject *parked = PyTuple_GET_ITEM(child, 3);
        PyObject *joined;
        if (!PyList_Check(parked)) {
            continue;
        }
        joined = xml_join(parked);
        if (joined == NULL) {
            goto done;
        }
        PyTuple_SET_ITEM(child, 3, joined);
        Py_DECREF(parked);
    }
    *text_out = xml_join(text_parts);
    status = *text_out == NULL ? -1 : 0;

done:
    Py_DECREF(text_parts);
    return status;
}


/* Parse one element, returning the node tuple
 * (tag, attrib, text, tail, span, nsdecl, qualified, prefix, local, children). */
static PyObject *
xml_read_element(XmlParser *parser, PyObject *inherited, Py_ssize_t depth)
{
    const uint8_t *data = parser->data;
    Py_ssize_t start = parser->pos;
    PyObject *name = NULL;
    PyObject *raw_names = NULL;
    PyObject *raw_values = NULL;
    PyObject *raw_offsets = NULL;
    PyObject *declarations = NULL;
    PyObject *scope = NULL;
    PyObject *attrib = NULL;
    PyObject *qualified = NULL;
    PyObject *children = NULL;
    PyObject *text = NULL;
    PyObject *prefix = NULL;
    PyObject *local = NULL;
    PyObject *uri = NULL;
    PyObject *tag = NULL;
    PyObject *node = NULL;
    PyObject *span = NULL;
    int empty = 0;

    if (depth > parser->limits.max_depth) {
        return xml_fail("depth", parser->pos,
                        "nesting depth exceeds the %zd limit",
                        parser->limits.max_depth);
    }
    parser->elements++;
    if (parser->elements > parser->limits.max_elements) {
        return xml_fail("elements", parser->pos,
                        "element count exceeds the %zd limit",
                        parser->limits.max_elements);
    }
    parser->pos++;   /* '<' */
    name = xml_read_name(parser);
    if (name == NULL) {
        return NULL;
    }

    raw_names = PyList_New(0);
    raw_values = PyList_New(0);
    raw_offsets = PyList_New(0);
    declarations = PyList_New(0);
    if (raw_names == NULL || raw_values == NULL || raw_offsets == NULL ||
        declarations == NULL) {
        goto error;
    }

    for (;;) {
        Py_ssize_t before = parser->pos;
        uint8_t byte;
        PyObject *attribute_name;
        PyObject *value;
        PyObject *offset_object;

        xml_skip_space(parser);
        if (parser->pos >= parser->len) {
            xml_fail("unexpected-end", start,
                     "the document ends inside a start tag");
            goto error;
        }
        byte = data[parser->pos];
        if (byte == '>') {
            parser->pos++;
            break;
        }
        if (byte == '/') {
            if (parser->pos + 1 >= parser->len || data[parser->pos + 1] != '>') {
                xml_fail("tag-syntax", parser->pos, "'/' must be followed by '>'");
                goto error;
            }
            parser->pos += 2;
            empty = 1;
            break;
        }
        if (parser->pos == before) {
            xml_fail("tag-syntax", parser->pos,
                     "whitespace is required between attributes");
            goto error;
        }
        attribute_name = xml_read_name(parser);
        if (attribute_name == NULL) {
            goto error;
        }
        xml_skip_space(parser);
        if (parser->pos >= parser->len || data[parser->pos] != '=') {
            Py_DECREF(attribute_name);
            xml_fail("attribute-syntax", parser->pos, "an attribute needs a value");
            goto error;
        }
        parser->pos++;
        xml_skip_space(parser);
        value = xml_read_attribute_value(parser);
        if (value == NULL) {
            Py_DECREF(attribute_name);
            goto error;
        }
        offset_object = PyLong_FromSsize_t(before);
        if (offset_object == NULL ||
            PyList_Append(raw_names, attribute_name) < 0 ||
            PyList_Append(raw_values, value) < 0 ||
            PyList_Append(raw_offsets, offset_object) < 0) {
            Py_DECREF(attribute_name);
            Py_DECREF(value);
            Py_XDECREF(offset_object);
            goto error;
        }
        Py_DECREF(attribute_name);
        Py_DECREF(value);
        Py_DECREF(offset_object);
        if (PyList_GET_SIZE(raw_names) > parser->limits.max_attributes) {
            xml_fail("attributes", parser->pos,
                     "attribute count exceeds the %zd limit",
                     parser->limits.max_attributes);
            goto error;
        }
    }

    /* Namespace declarations, in document order, before anything is resolved. */
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(raw_names); i++) {
        PyObject *attribute_name = PyList_GET_ITEM(raw_names, i);
        PyObject *value = PyList_GET_ITEM(raw_values, i);
        Py_ssize_t offset = PyLong_AsSsize_t(PyList_GET_ITEM(raw_offsets, i));
        Py_ssize_t size;

        /* This module boxed the offset itself, so the sentinel cannot mean an
         * error here -- but PyLong_As* signals through an ordinary value, and
         * an unchecked one is the same shape as a real defect. */
        if (offset < 0 && PyErr_Occurred()) {
            goto error;
        }
        const char *utf8 = PyUnicode_AsUTF8AndSize(attribute_name, &size);
        PyObject *entry;
        PyObject *declared_prefix;

        if (utf8 == NULL) {
            goto error;
        }
        if (size == 5 && memcmp(utf8, "xmlns", 5) == 0) {
            declared_prefix = PyUnicode_FromString("");
        }
        else if (size > 6 && memcmp(utf8, "xmlns:", 6) == 0) {
            Py_ssize_t tail = size - 6;
            if (memchr(utf8 + 6, ':', (size_t)tail) != NULL) {
                xml_fail("invalid-name", parser->pos,
                         "%R is not a valid declaration", attribute_name);
                goto error;
            }
            declared_prefix = PyUnicode_DecodeUTF8(utf8 + 6, tail, "strict");
            if (declared_prefix == NULL) {
                goto error;
            }
            {
                int reserved =
                    PyUnicode_CompareWithASCIIString(declared_prefix, "xmlns") == 0 ||
                    PyUnicode_CompareWithASCIIString(declared_prefix, "xml") == 0;
                int correct_xml =
                    PyUnicode_CompareWithASCIIString(declared_prefix, "xml") == 0 &&
                    PyUnicode_CompareWithASCIIString(value, XML_NS_URI) == 0;
                if (reserved && !correct_xml) {
                    xml_fail("reserved-prefix", offset,
                             "the %R prefix is reserved and cannot be rebound",
                             declared_prefix);
                    Py_DECREF(declared_prefix);
                    goto error;
                }
            }
            if (PyUnicode_GET_LENGTH(value) == 0) {
                xml_fail("empty-prefix-uri", offset,
                         "a prefix cannot be bound to the empty namespace; "
                         "undeclaring %R is an XML 1.1 feature", declared_prefix);
                Py_DECREF(declared_prefix);
                goto error;
            }
        }
        else {
            continue;
        }
        if (declared_prefix == NULL) {
            goto error;
        }
        entry = PyTuple_Pack(2, declared_prefix, value);
        Py_DECREF(declared_prefix);
        if (entry == NULL || PyList_Append(declarations, entry) < 0) {
            Py_XDECREF(entry);
            goto error;
        }
        Py_DECREF(entry);
    }

    /* The scope this element sees: the inherited one when it declares nothing,
     * a copy with its declarations applied when it does. */
    if (PyList_GET_SIZE(declarations) == 0) {
        scope = Py_NewRef(inherited);
    }
    else {
        scope = PyDict_Copy(inherited);
        if (scope == NULL) {
            goto error;
        }
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(declarations); i++) {
            PyObject *entry = PyList_GET_ITEM(declarations, i);
            PyObject *declared_prefix = PyTuple_GET_ITEM(entry, 0);
            PyObject *value = PyTuple_GET_ITEM(entry, 1);
            if (PyUnicode_GET_LENGTH(value) > 0) {
                if (PyDict_SetItem(scope, declared_prefix, value) < 0) {
                    goto error;
                }
            }
            else if (PyDict_DelItem(scope, declared_prefix) < 0) {
                PyErr_Clear();
            }
        }
    }

    if (xml_split_name(parser, name, &prefix, &local) < 0) {
        goto error;
    }
    uri = xml_resolve(parser, prefix, scope, parser->pos);
    if (uri == NULL) {
        goto error;
    }
    tag = PyUnicode_GET_LENGTH(uri) > 0
              ? PyUnicode_FromFormat("{%U}%U", uri, local)
              : Py_NewRef(local);
    if (tag == NULL) {
        goto error;
    }

    attrib = PyDict_New();
    qualified = PyList_New(0);
    if (attrib == NULL || qualified == NULL) {
        goto error;
    }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(raw_names); i++) {
        PyObject *attribute_name = PyList_GET_ITEM(raw_names, i);
        PyObject *value = PyList_GET_ITEM(raw_values, i);
        Py_ssize_t offset = PyLong_AsSsize_t(PyList_GET_ITEM(raw_offsets, i));
        Py_ssize_t size;

        /* This module boxed the offset itself, so the sentinel cannot mean an
         * error here -- but PyLong_As* signals through an ordinary value, and
         * an unchecked one is the same shape as a real defect. */
        if (offset < 0 && PyErr_Occurred()) {
            goto error;
        }
        const char *utf8 = PyUnicode_AsUTF8AndSize(attribute_name, &size);
        PyObject *attribute_prefix = NULL;
        PyObject *attribute_local = NULL;
        PyObject *attribute_uri = NULL;
        PyObject *key = NULL;
        PyObject *entry = NULL;
        int present;

        if (utf8 == NULL) {
            goto error;
        }
        if ((size == 5 && memcmp(utf8, "xmlns", 5) == 0) ||
            (size > 6 && memcmp(utf8, "xmlns:", 6) == 0)) {
            continue;
        }
        if (xml_split_name(parser, attribute_name, &attribute_prefix,
                           &attribute_local) < 0) {
            goto error;
        }
        if (PyUnicode_GET_LENGTH(attribute_prefix) > 0) {
            attribute_uri = xml_resolve(parser, attribute_prefix, scope, offset);
        }
        else {
            attribute_uri = PyUnicode_FromString("");
        }
        if (attribute_uri == NULL) {
            Py_DECREF(attribute_prefix);
            Py_DECREF(attribute_local);
            goto error;
        }
        key = PyUnicode_GET_LENGTH(attribute_uri) > 0
                  ? PyUnicode_FromFormat("{%U}%U", attribute_uri, attribute_local)
                  : Py_NewRef(attribute_local);
        if (key == NULL) {
            Py_DECREF(attribute_prefix);
            Py_DECREF(attribute_local);
            Py_DECREF(attribute_uri);
            goto error;
        }
        present = PyDict_Contains(attrib, key);
        if (present < 0) {
            goto attribute_error;
        }
        if (present) {
            xml_fail("duplicate-attribute", offset,
                     "duplicate attribute %R on one element", key);
            goto attribute_error;
        }
        if (PyDict_SetItem(attrib, key, value) < 0) {
            goto attribute_error;
        }
        entry = PyTuple_Pack(4, attribute_prefix, attribute_local, attribute_uri,
                             value);
        if (entry == NULL || PyList_Append(qualified, entry) < 0) {
            Py_XDECREF(entry);
            goto attribute_error;
        }
        Py_DECREF(entry);
        Py_DECREF(attribute_prefix);
        Py_DECREF(attribute_local);
        Py_DECREF(attribute_uri);
        Py_DECREF(key);
        continue;

    attribute_error:
        Py_XDECREF(attribute_prefix);
        Py_XDECREF(attribute_local);
        Py_XDECREF(attribute_uri);
        Py_XDECREF(key);
        goto error;
    }

    children = PyList_New(0);
    if (children == NULL) {
        goto error;
    }
    if (empty) {
        text = PyUnicode_FromString("");
        if (text == NULL) {
            goto error;
        }
    }
    else if (xml_read_content(parser, name, scope, depth, &text, children) < 0) {
        goto error;
    }

    span = Py_BuildValue("(nn)", start, parser->pos);
    if (span == NULL) {
        goto error;
    }
    {
        PyObject *declaration_tuple = PyList_AsTuple(declarations);
        PyObject *qualified_tuple = PyList_AsTuple(qualified);
        PyObject *children_tuple = PyList_AsTuple(children);
        PyObject *empty_tail = PyUnicode_FromString("");
        if (declaration_tuple == NULL || qualified_tuple == NULL ||
            children_tuple == NULL || empty_tail == NULL) {
            Py_XDECREF(declaration_tuple);
            Py_XDECREF(qualified_tuple);
            Py_XDECREF(children_tuple);
            Py_XDECREF(empty_tail);
            goto error;
        }
        /* A list, not a tuple, so xml_read_content can park a tail list in
         * slot 3 and replace it in place once the tail is known. */
        node = PyTuple_New(10);
        if (node == NULL) {
            Py_DECREF(declaration_tuple);
            Py_DECREF(qualified_tuple);
            Py_DECREF(children_tuple);
            Py_DECREF(empty_tail);
            goto error;
        }
        PyTuple_SET_ITEM(node, 0, Py_NewRef(tag));
        PyTuple_SET_ITEM(node, 1, Py_NewRef(attrib));
        PyTuple_SET_ITEM(node, 2, Py_NewRef(text));
        PyTuple_SET_ITEM(node, 3, empty_tail);
        PyTuple_SET_ITEM(node, 4, Py_NewRef(span));
        PyTuple_SET_ITEM(node, 5, declaration_tuple);
        PyTuple_SET_ITEM(node, 6, qualified_tuple);
        PyTuple_SET_ITEM(node, 7, Py_NewRef(prefix));
        PyTuple_SET_ITEM(node, 8, Py_NewRef(local));
        PyTuple_SET_ITEM(node, 9, children_tuple);
    }

error:
    Py_XDECREF(name);
    Py_XDECREF(raw_names);
    Py_XDECREF(raw_values);
    Py_XDECREF(raw_offsets);
    Py_XDECREF(declarations);
    Py_XDECREF(scope);
    Py_XDECREF(attrib);
    Py_XDECREF(qualified);
    Py_XDECREF(children);
    Py_XDECREF(text);
    Py_XDECREF(prefix);
    Py_XDECREF(local);
    Py_XDECREF(uri);
    Py_XDECREF(tag);
    Py_XDECREF(span);
    return node;
}


static int
xml_read_limits(PyObject *args, Py_ssize_t offset, XmlLimits *limits)
{
    limits->max_bytes = PyLong_AsSsize_t(PyTuple_GET_ITEM(args, offset));
    limits->max_depth = PyLong_AsSsize_t(PyTuple_GET_ITEM(args, offset + 1));
    limits->max_elements = PyLong_AsSsize_t(PyTuple_GET_ITEM(args, offset + 2));
    limits->max_attributes = PyLong_AsSsize_t(PyTuple_GET_ITEM(args, offset + 3));
    limits->max_attribute_bytes = PyLong_AsSsize_t(PyTuple_GET_ITEM(args, offset + 4));
    if (PyErr_Occurred()) {
        return -1;
    }
    if (limits->max_depth > XML_MAX_DEPTH_CEILING) {
        PyErr_SetString(PyExc_ValueError,
                        "max_depth exceeds the parser's recursion ceiling");
        return -1;
    }
    return 0;
}


/* Parse a whole document: prolog, one root element, trailing whitespace. */
static PyObject *
xml_parse_document(const uint8_t *data, Py_ssize_t len, XmlLimits limits,
                   PyObject *initial_scope)
{
    XmlParser parser;
    PyObject *scope = NULL;
    PyObject *root = NULL;

    parser.data = data;
    parser.len = len;
    parser.pos = 0;
    parser.elements = 0;
    parser.limits = limits;

    if (len == 0) {
        return xml_fail("unexpected-end", 0, "the document is empty");
    }
    if (len > limits.max_bytes) {
        return xml_fail("size", 0,
                        "document size %zd exceeds the %zd-byte limit", len,
                        limits.max_bytes);
    }
    if (len >= 3 && data[0] == 0xEF && data[1] == 0xBB && data[2] == 0xBF) {
        return xml_fail("byte-order-mark", 0,
                        "a byte order mark is outside this profile; the bytes a "
                        "signature covers must be the bytes that were parsed");
    }
    if (xml_validate_bytes(&parser) < 0 || xml_read_declaration(&parser) < 0) {
        return NULL;
    }

    scope = initial_scope != NULL ? Py_NewRef(initial_scope) : PyDict_New();
    if (scope == NULL) {
        return NULL;
    }
    while (parser.pos < parser.len) {
        uint8_t byte = data[parser.pos];
        if (xml_is_space(byte)) {
            parser.pos++;
            continue;
        }
        if (byte != '<') {
            xml_fail(root == NULL ? "content-before-root" : "trailing-content",
                     parser.pos,
                     "character data is not permitted outside the root element");
            goto error;
        }
        if (xml_reject_markup_declaration(&parser) < 0) {
            goto error;
        }
        if (root != NULL) {
            xml_fail("trailing-content", parser.pos,
                     "content is not permitted after the root element");
            goto error;
        }
        root = xml_read_element(&parser, scope, 1);
        if (root == NULL) {
            goto error;
        }
    }
    if (root == NULL) {
        xml_fail("unexpected-end", len, "the document has no root element");
        goto error;
    }
    Py_DECREF(scope);
    return root;

error:
    Py_DECREF(scope);
    Py_XDECREF(root);
    return NULL;
}


PyObject *
wreath_xml_configure(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_XSETREF(xml_refusal_type, Py_NewRef(arg));
    Py_RETURN_NONE;
}


PyObject *
wreath_xml_parse(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer buffer;
    XmlLimits limits;
    PyObject *root;

    if (PyTuple_GET_SIZE(args) != 6) {
        PyErr_SetString(PyExc_TypeError, "xml_parse expects 6 arguments");
        return NULL;
    }
    if (xml_read_limits(args, 1, &limits) < 0) {
        return NULL;
    }
    if (PyObject_GetBuffer(PyTuple_GET_ITEM(args, 0), &buffer, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    root = xml_parse_document((const uint8_t *)buffer.buf, buffer.len, limits, NULL);
    PyBuffer_Release(&buffer);
    return root;
}


/* ------------------------------------------------------------------------ */
/* Exclusive XML Canonicalization 1.0                                        */
/* ------------------------------------------------------------------------ */

static int
xml_write(PyObject *out, const char *text)
{
    PyObject *piece = PyUnicode_FromString(text);
    int status;
    if (piece == NULL) {
        return -1;
    }
    status = PyList_Append(out, piece);
    Py_DECREF(piece);
    return status;
}


static int
xml_write_object(PyObject *out, PyObject *text)
{
    return PyList_Append(out, text);
}


/* Escape per the c14n rules. `attribute` selects the attribute-value set. */
static PyObject *
xml_escape(PyObject *value, int attribute)
{
    static const char *const text_from[] = {"&", "<", ">", "\r"};
    static const char *const text_to[] = {"&amp;", "&lt;", "&gt;", "&#xD;"};
    static const char *const attribute_from[] = {"&", "<", "\"", "\t", "\n", "\r"};
    static const char *const attribute_to[] = {"&amp;", "&lt;", "&quot;",
                                               "&#x9;", "&#xA;", "&#xD;"};
    const char *const *from = attribute ? attribute_from : text_from;
    const char *const *to = attribute ? attribute_to : text_to;
    Py_ssize_t count = attribute ? 6 : 4;
    PyObject *current = Py_NewRef(value);

    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *needle = PyUnicode_FromString(from[i]);
        PyObject *replacement = PyUnicode_FromString(to[i]);
        PyObject *next;
        if (needle == NULL || replacement == NULL) {
            Py_XDECREF(needle);
            Py_XDECREF(replacement);
            Py_DECREF(current);
            return NULL;
        }
        next = PyUnicode_Replace(current, needle, replacement, -1);
        Py_DECREF(needle);
        Py_DECREF(replacement);
        Py_DECREF(current);
        if (next == NULL) {
            return NULL;
        }
        current = next;
    }
    return current;
}


static int
xml_write_escaped(PyObject *out, PyObject *value, int attribute)
{
    PyObject *escaped = xml_escape(value, attribute);
    int status;
    if (escaped == NULL) {
        return -1;
    }
    status = PyList_Append(out, escaped);
    Py_DECREF(escaped);
    return status;
}


/* Sort key for attributes: (namespace uri, local name). */
static PyObject *
xml_attribute_key(PyObject *entry)
{
    return PyTuple_Pack(2, PyTuple_GET_ITEM(entry, 2), PyTuple_GET_ITEM(entry, 1));
}


static int
xml_render(PyObject *node, PyObject *scope, PyObject *rendered,
           PyObject *inclusive, PyObject *out);


/* Compute the scope in force at `node` given its parent's `inherited`. */
static PyObject *
xml_child_scope(PyObject *node, PyObject *inherited)
{
    PyObject *declarations = PyTuple_GET_ITEM(node, 5);
    PyObject *scope;

    if (PyTuple_GET_SIZE(declarations) == 0) {
        return Py_NewRef(inherited);
    }
    scope = PyDict_Copy(inherited);
    if (scope == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(declarations); i++) {
        PyObject *entry = PyTuple_GET_ITEM(declarations, i);
        PyObject *prefix = PyTuple_GET_ITEM(entry, 0);
        PyObject *uri = PyTuple_GET_ITEM(entry, 1);
        if (PyUnicode_GET_LENGTH(uri) > 0) {
            if (PyDict_SetItem(scope, prefix, uri) < 0) {
                Py_DECREF(scope);
                return NULL;
            }
        }
        else if (PyDict_DelItem(scope, prefix) < 0) {
            PyErr_Clear();
        }
    }
    return scope;
}


static int
xml_render(PyObject *node, PyObject *inherited, PyObject *rendered,
           PyObject *inclusive, PyObject *out)
{
    PyObject *scope = xml_child_scope(node, inherited);
    PyObject *qualified = PyTuple_GET_ITEM(node, 6);
    PyObject *prefix = PyTuple_GET_ITEM(node, 7);
    PyObject *local = PyTuple_GET_ITEM(node, 8);
    PyObject *children = PyTuple_GET_ITEM(node, 9);
    PyObject *utilized = NULL;
    PyObject *emitted = NULL;
    PyObject *ordered = NULL;
    PyObject *sorted_attributes = NULL;
    PyObject *child_rendered = NULL;
    PyObject *qualified_name = NULL;
    int status = -1;

    if (scope == NULL) {
        return -1;
    }
    utilized = PySet_New(NULL);
    emitted = PyList_New(0);
    if (utilized == NULL || emitted == NULL) {
        goto done;
    }
    if (PySet_Add(utilized, prefix) < 0) {
        goto done;
    }
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(qualified); i++) {
        PyObject *attribute_prefix = PyTuple_GET_ITEM(PyTuple_GET_ITEM(qualified, i), 0);
        if (PyUnicode_GET_LENGTH(attribute_prefix) > 0 &&
            PySet_Add(utilized, attribute_prefix) < 0) {
            goto done;
        }
    }
    {
        /* Prefixes named in the InclusiveNamespaces PrefixList are rendered
         * when in scope even where the subtree does not visibly utilize them. */
        PyObject *iterator = PyObject_GetIter(inclusive);
        PyObject *item;
        if (iterator == NULL) {
            goto done;
        }
        while ((item = PyIter_Next(iterator)) != NULL) {
            int present = PyDict_Contains(scope, item);
            if (present < 0 || (present && PySet_Add(utilized, item) < 0)) {
                Py_DECREF(item);
                Py_DECREF(iterator);
                goto done;
            }
            Py_DECREF(item);
        }
        Py_DECREF(iterator);
        if (PyErr_Occurred()) {
            goto done;
        }
    }
    {
        PyObject *xml_prefix = PyUnicode_FromString("xml");
        int discarded;
        if (xml_prefix == NULL) {
            goto done;
        }
        discarded = PySet_Discard(utilized, xml_prefix);
        Py_DECREF(xml_prefix);
        if (discarded < 0) {
            goto done;
        }
    }

    ordered = PySequence_List(utilized);
    if (ordered == NULL || PyList_Sort(ordered) < 0) {
        goto done;
    }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(ordered); i++) {
        PyObject *candidate = PyList_GET_ITEM(ordered, i);
        PyObject *uri = PyDict_GetItemWithError(scope, candidate);
        PyObject *previous;
        PyObject *entry;

        if (uri == NULL && PyErr_Occurred()) {
            goto done;
        }
        previous = PyDict_GetItemWithError(rendered, candidate);
        if (previous == NULL && PyErr_Occurred()) {
            goto done;
        }
        {
            Py_ssize_t uri_len = uri == NULL ? 0 : PyUnicode_GET_LENGTH(uri);
            Py_ssize_t previous_len =
                previous == NULL ? 0 : PyUnicode_GET_LENGTH(previous);
            if (uri_len == 0 && previous_len == 0) {
                continue;
            }
            if (uri != NULL && previous != NULL &&
                PyUnicode_Compare(uri, previous) == 0) {
                continue;
            }
            if (PyErr_Occurred()) {
                goto done;
            }
        }
        entry = PyTuple_Pack(2, candidate,
                             uri == NULL ? Py_None : uri);
        if (entry == NULL || PyList_Append(emitted, entry) < 0) {
            Py_XDECREF(entry);
            goto done;
        }
        Py_DECREF(entry);
    }

    qualified_name = PyUnicode_GET_LENGTH(prefix) > 0
                         ? PyUnicode_FromFormat("%U:%U", prefix, local)
                         : Py_NewRef(local);
    if (qualified_name == NULL) {
        goto done;
    }
    if (xml_write(out, "<") < 0 || xml_write_object(out, qualified_name) < 0) {
        goto done;
    }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(emitted); i++) {
        PyObject *entry = PyList_GET_ITEM(emitted, i);
        PyObject *candidate = PyTuple_GET_ITEM(entry, 0);
        PyObject *uri = PyTuple_GET_ITEM(entry, 1);
        PyObject *shown = uri == Py_None ? PyUnicode_FromString("") : Py_NewRef(uri);
        int failed;

        if (shown == NULL) {
            goto done;
        }
        if (PyUnicode_GET_LENGTH(candidate) > 0) {
            failed = xml_write(out, " xmlns:") < 0 ||
                     xml_write_object(out, candidate) < 0;
        }
        else {
            failed = xml_write(out, " xmlns") < 0;
        }
        failed = failed || xml_write(out, "=\"") < 0 ||
                 xml_write_escaped(out, shown, 1) < 0 || xml_write(out, "\"") < 0;
        Py_DECREF(shown);
        if (failed) {
            goto done;
        }
    }

    sorted_attributes = PySequence_List(qualified);
    if (sorted_attributes == NULL) {
        goto done;
    }
    {
        /* Decorate-sort-undecorate: the key is (uri, local), and building it
         * once per attribute beats a comparison function that rebuilds it per
         * comparison. */
        Py_ssize_t count = PyList_GET_SIZE(sorted_attributes);
        PyObject *decorated = PyList_New(count);
        if (decorated == NULL) {
            goto done;
        }
        for (Py_ssize_t i = 0; i < count; i++) {
            PyObject *entry = PyList_GET_ITEM(sorted_attributes, i);
            PyObject *key = xml_attribute_key(entry);
            PyObject *pair;
            if (key == NULL) {
                Py_DECREF(decorated);
                goto done;
            }
            pair = PyTuple_Pack(2, key, entry);
            Py_DECREF(key);
            if (pair == NULL) {
                Py_DECREF(decorated);
                goto done;
            }
            PyList_SET_ITEM(decorated, i, pair);
        }
        if (PyList_Sort(decorated) < 0) {
            Py_DECREF(decorated);
            goto done;
        }
        for (Py_ssize_t i = 0; i < count; i++) {
            PyObject *entry = PyTuple_GET_ITEM(PyList_GET_ITEM(decorated, i), 1);
            PyObject *attribute_prefix = PyTuple_GET_ITEM(entry, 0);
            PyObject *attribute_local = PyTuple_GET_ITEM(entry, 1);
            PyObject *value = PyTuple_GET_ITEM(entry, 3);
            int failed = xml_write(out, " ") < 0;
            if (!failed && PyUnicode_GET_LENGTH(attribute_prefix) > 0) {
                failed = xml_write_object(out, attribute_prefix) < 0 ||
                         xml_write(out, ":") < 0;
            }
            failed = failed || xml_write_object(out, attribute_local) < 0 ||
                     xml_write(out, "=\"") < 0 ||
                     xml_write_escaped(out, value, 1) < 0 ||
                     xml_write(out, "\"") < 0;
            if (failed) {
                Py_DECREF(decorated);
                goto done;
            }
        }
        Py_DECREF(decorated);
    }
    if (xml_write(out, ">") < 0) {
        goto done;
    }

    child_rendered = PyDict_Copy(rendered);
    if (child_rendered == NULL) {
        goto done;
    }
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(emitted); i++) {
        PyObject *entry = PyList_GET_ITEM(emitted, i);
        PyObject *uri = PyTuple_GET_ITEM(entry, 1);
        PyObject *shown = uri == Py_None ? PyUnicode_FromString("") : Py_NewRef(uri);
        int failed;
        if (shown == NULL) {
            goto done;
        }
        failed = PyDict_SetItem(child_rendered, PyTuple_GET_ITEM(entry, 0), shown) < 0;
        Py_DECREF(shown);
        if (failed) {
            goto done;
        }
    }

    if (xml_write_escaped(out, PyTuple_GET_ITEM(node, 2), 0) < 0) {
        goto done;
    }
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(children); i++) {
        PyObject *child = PyTuple_GET_ITEM(children, i);
        if (xml_render(child, scope, child_rendered, inclusive, out) < 0 ||
            xml_write_escaped(out, PyTuple_GET_ITEM(child, 3), 0) < 0) {
            goto done;
        }
    }
    if (xml_write(out, "</") < 0 || xml_write_object(out, qualified_name) < 0 ||
        xml_write(out, ">") < 0) {
        goto done;
    }
    status = 0;

done:
    Py_XDECREF(scope);
    Py_XDECREF(utilized);
    Py_XDECREF(emitted);
    Py_XDECREF(ordered);
    Py_XDECREF(sorted_attributes);
    Py_XDECREF(child_rendered);
    Py_XDECREF(qualified_name);
    return status;
}


PyObject *
wreath_xml_c14n(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer buffer;
    XmlLimits limits;
    Py_ssize_t start;
    Py_ssize_t end;
    PyObject *inherited_pairs;
    PyObject *inclusive_input;
    PyObject *scope = NULL;
    PyObject *inclusive = NULL;
    PyObject *root = NULL;
    PyObject *out = NULL;
    PyObject *rendered = NULL;
    PyObject *joined = NULL;
    PyObject *result = NULL;

    if (PyTuple_GET_SIZE(args) != 10) {
        PyErr_SetString(PyExc_TypeError, "xml_c14n expects 10 arguments");
        return NULL;
    }
    start = PyLong_AsSsize_t(PyTuple_GET_ITEM(args, 1));
    end = PyLong_AsSsize_t(PyTuple_GET_ITEM(args, 2));
    inherited_pairs = PyTuple_GET_ITEM(args, 3);
    inclusive_input = PyTuple_GET_ITEM(args, 4);
    if (PyErr_Occurred() || xml_read_limits(args, 5, &limits) < 0) {
        return NULL;
    }
    if (PyObject_GetBuffer(PyTuple_GET_ITEM(args, 0), &buffer, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    if (start < 0 || end <= start || end > buffer.len) {
        PyBuffer_Release(&buffer);
        PyErr_SetString(PyExc_ValueError, "span does not address the source");
        return NULL;
    }

    scope = PyDict_New();
    inclusive = PySet_New(NULL);
    if (scope == NULL || inclusive == NULL) {
        goto done;
    }
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(inherited_pairs); i++) {
        PyObject *entry = PyTuple_GET_ITEM(inherited_pairs, i);
        if (PyDict_SetItem(scope, PyTuple_GET_ITEM(entry, 0),
                           PyTuple_GET_ITEM(entry, 1)) < 0) {
            goto done;
        }
    }
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(inclusive_input); i++) {
        PyObject *name = PyTuple_GET_ITEM(inclusive_input, i);
        PyObject *normalized;
        int failed;
        /* `#default` is how the PrefixList spells the default namespace. */
        if (PyUnicode_CompareWithASCIIString(name, "#default") == 0) {
            normalized = PyUnicode_FromString("");
        }
        else {
            normalized = Py_NewRef(name);
        }
        if (normalized == NULL) {
            goto done;
        }
        failed = PySet_Add(inclusive, normalized) < 0;
        Py_DECREF(normalized);
        if (failed) {
            goto done;
        }
    }

    root = xml_parse_document((const uint8_t *)buffer.buf + start, end - start,
                              limits, scope);
    if (root == NULL) {
        goto done;
    }
    out = PyList_New(0);
    rendered = PyDict_New();
    if (out == NULL || rendered == NULL) {
        goto done;
    }
    if (xml_render(root, scope, rendered, inclusive, out) < 0) {
        goto done;
    }
    joined = xml_join(out);
    if (joined == NULL) {
        goto done;
    }
    result = PyUnicode_AsUTF8String(joined);

done:
    PyBuffer_Release(&buffer);
    Py_XDECREF(scope);
    Py_XDECREF(inclusive);
    Py_XDECREF(root);
    Py_XDECREF(out);
    Py_XDECREF(rendered);
    Py_XDECREF(joined);
    return result;
}
