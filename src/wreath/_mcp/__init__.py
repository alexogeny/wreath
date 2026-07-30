"""Implementation of the Model Context Protocol server surface.

Nothing here is public. `wreath.mcp` is the facade, and it is the only import
path that is promised to keep working; the split exists so that a protocol
revision bump stays contained behind one directory, the way `_h2_codec` keeps
HTTP/2 framing away from the rest of the server.
"""
