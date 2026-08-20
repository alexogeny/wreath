# Wreath gzip kernels

Wreath's native encoder and decoder are compiled into `wreath._native._core`.
`CompressionPolicy` owns reusable encoder workspace for its application. The
native HTTP servers call that workspace through Wreath's C API, so negotiation,
format selection, encoding, and response-header rewriting do not cross through
a Python compressor callback. Compressed-input streams likewise own reusable
decoder workspace; gRPC keeps one for the lifetime of each `Unframer`, retaining
validated native table state between messages.

The two halves intentionally share no parser or Huffman implementation. They
exchange only standard RFC 1951 deflate wrapped in RFC 1952 gzip. Content-format
hints select parser/table policies but never alter the wire format.

All source is MPL-2.0. The implementation follows RFC 1951 and RFC 1952;
zlib-ng and libdeflate are differential-test and benchmark oracles.
