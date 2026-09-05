import hashlib

import pytest

from wreath._devtools import dup_scan


class SourcePath:
    def __init__(self, source):
        self.source = source

    def read_text(self, *, encoding):
        assert encoding == "utf-8"
        return self.source


def test_native_line_count_ranges_are_bounded_by_source_size():
    scanned = []

    class Source(str):
        def count(self, needle, start=0, end=None):
            stop = len(self) if end is None else end
            scanned.append(stop - start)
            return super().count(needle, start, stop)

    source = Source(
        "\n".join(
            f"static int\nfunction_{index}(int value)\n{{\n    return value + {index};\n}}\n"
            for index in range(100)
        )
    )
    bodies = dup_scan._native_bodies(SourcePath(source), "sample.c", 1)
    assert len(bodies) == 100
    assert sum(scanned) <= len(source) * 3


@pytest.mark.parametrize("normalization", ["shape", "alpha"])
@pytest.mark.parametrize("build_structure", [False, True])
@pytest.mark.parametrize("min_lines", [1, 3])
def test_native_sites_keep_prefix_oracle_lines_and_body_images(
    normalization, build_structure, min_lines
):
    source = (
        "/* π and blank lines */\n\nstatic int\nfirst(\n    int value)\n{\n"
        '    const char *text = "}";\n    if (value) { value++; }\n'
        "    return value;\n}\n\nif (ignored) {\n}\n"
        "static int tiny(void) { return 0; }\n\n"
        "static int\nlast(void)\n{\n    /* { */\n    int n = 1;\n    return n;\n}"
    )
    bodies = dup_scan._native_bodies(
        SourcePath(source), "sample.c", min_lines, normalization, build_structure=build_structure
    )
    assert [body.site.name for body in bodies] == (
        ["first", "tiny", "last"] if min_lines == 1 else ["first"]
    )
    for body in bodies:
        name_offset = source.index(body.site.name + "(")
        brace = source.index("{", name_offset)
        end = (
            source.index("}", brace) if body.site.name == "tiny" else source.index("\n}", brace) + 1
        )
        fragment = source[brace + 1 : end]
        shape, lines = dup_scan._c_shape(fragment, normalization)
        expected_shape = bytes(shape) if build_structure else b""
        assert body.site.line == len(source[:name_offset].split("\n"))
        assert body.site.body_start == len(source[:brace].split("\n"))
        assert body.site.body_end == len(source[:end].split("\n"))
        assert body.site.lines == lines
        assert body.fragment_source == fragment.encode()
        assert body.fragment_start == body.site.body_start
        assert body.shape == expected_shape
        assert body.digest == hashlib.blake2s(expected_shape, digest_size=12).hexdigest()


def test_native_file_newline_translation_and_unterminated_body(tmp_path):
    path = tmp_path / "source.c"
    path.write_bytes(b"/* header */\r\nstatic int\r\nopen_body(void)\r\n{\r\n    return 1;\r\n")
    (body,) = dup_scan._native_bodies(path, "source.c", 1)
    assert (body.site.line, body.site.body_start, body.site.body_end) == (3, 4, 6)
