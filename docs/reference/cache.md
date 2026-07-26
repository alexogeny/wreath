# `wreath.cache`

The read-mostly application cache (`SnapshotCache`) with atomic snapshot
publication, the bounded LRU/TTL store behind the response cache, and
`invalidate_across_workers`, which carries ORM write announcements to every
worker over the message bus.

::: wreath.cache
