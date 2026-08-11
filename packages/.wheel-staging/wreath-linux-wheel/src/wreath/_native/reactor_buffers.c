/* Registered provided-buffer ring lifecycle and ownership. */
static void
metal_provided_buffers_clear(MetalUring *uring, MetalProvidedBuffers *pool)
{
    if (pool->registered && uring->fd >= 0) {
        struct io_uring_buf_reg registration;
        memset(&registration, 0, sizeof(registration));
        registration.bgid = pool->group_id;
        syscall(SYS_io_uring_register, uring->fd,
                IORING_UNREGISTER_PBUF_RING, &registration, 1);
    }
    if (pool->ring != NULL && pool->ring != MAP_FAILED) {
        munmap(pool->ring, pool->ring_size);
    }
    if (pool->data != NULL && pool->data != MAP_FAILED) {
        munmap(pool->data, pool->data_size);
    }
    if (pool->tokens != NULL && pool->descriptors != NULL) {
        for (uint16_t buffer_id = 0; buffer_id < pool->entries; buffer_id++) {
            if (pool->tokens[buffer_id] != 0) {
                metal_slab_release(pool->descriptors,
                                   pool->tokens[buffer_id], pool);
            }
        }
    }
    PyMem_Free(pool->in_ring);
    PyMem_Free(pool->tokens);
    memset(pool, 0, sizeof(*pool));
}

static int
metal_provided_buffer_recycle(MetalProvidedBuffers *pool, uint16_t buffer_id)
{
    if (buffer_id >= pool->entries || pool->in_ring[buffer_id]) {
        errno = EINVAL;
        return -1;
    }
    uint16_t index = pool->tail & pool->mask;
    struct io_uring_buf *buffer = &pool->ring->bufs[index];
    buffer->addr = (uint64_t)(uintptr_t)(
        pool->data + (size_t)buffer_id * pool->buffer_size);
    buffer->len = pool->buffer_size;
    buffer->bid = buffer_id;
    pool->tail++;
    pool->in_ring[buffer_id] = 1;
    return 0;
}

static void
metal_provided_buffers_publish(MetalProvidedBuffers *pool)
{
    __atomic_store_n(&pool->ring->tail, pool->tail, __ATOMIC_RELEASE);
}

static int
metal_provided_buffer_claim(MetalProvidedBuffers *pool, uint16_t buffer_id)
{
    /* The kernel returns a buffer ID, not our descriptor token. `in_ring` is the
     * authoritative lifecycle guard and rejects duplicate/stale CQEs. Descriptor
     * generations remain useful for bounded ownership diagnostics, but validating
     * the same persistent token on every packet adds no independent protection. */
    if (buffer_id >= pool->entries || !pool->in_ring[buffer_id]) {
        pool->descriptors->stale++;
        return 0;
    }
    pool->in_ring[buffer_id] = 0;
    return 1;
}

static int
metal_provided_buffers_init(MetalUring *uring, MetalProvidedBuffers *pool,
                            MetalSlab *descriptors, uint16_t entries,
                            uint32_t buffer_size, uint16_t group_id)
{
    memset(pool, 0, sizeof(*pool));
    if (entries == 0 || (entries & (entries - 1)) != 0) {
        errno = EINVAL;
        return -1;
    }
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) {
        page_size = 4096;
    }
    size_t raw_ring_size = (size_t)entries * sizeof(struct io_uring_buf);
    pool->ring_size = (raw_ring_size + (size_t)page_size - 1) &
                      ~((size_t)page_size - 1);
    pool->data_size = (size_t)entries * buffer_size;
    pool->ring = mmap(NULL, pool->ring_size, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (pool->ring == MAP_FAILED) {
        return -1;
    }
    pool->data = mmap(NULL, pool->data_size, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (pool->data == MAP_FAILED) {
        int saved_errno = errno;
        munmap(pool->ring, pool->ring_size);
        memset(pool, 0, sizeof(*pool));
        errno = saved_errno;
        return -1;
    }
    pool->entries = entries;
    pool->mask = entries - 1;
    pool->group_id = group_id;
    pool->buffer_size = buffer_size;
    pool->descriptors = descriptors;
    pool->tokens = PyMem_Calloc(entries, sizeof(uint64_t));
    pool->in_ring = PyMem_Calloc(entries, sizeof(uint8_t));
    if (pool->tokens == NULL || pool->in_ring == NULL) {
        metal_provided_buffers_clear(uring, pool);
        PyErr_NoMemory();
        return -1;
    }

    struct io_uring_buf_reg registration;
    memset(&registration, 0, sizeof(registration));
    registration.ring_addr = (uint64_t)(uintptr_t)pool->ring;
    registration.ring_entries = entries;
    registration.bgid = group_id;
    if (syscall(SYS_io_uring_register, uring->fd,
                IORING_REGISTER_PBUF_RING, &registration, 1) < 0) {
        int saved_errno = errno;
        metal_provided_buffers_clear(uring, pool);
        errno = saved_errno;
        return -1;
    }
    pool->registered = 1;
    for (uint16_t buffer_id = 0; buffer_id < entries; buffer_id++) {
        uint64_t token = metal_slab_allocate(
            descriptors, pool, buffer_id, METAL_OP_BUFFER);
        if (token == 0) {
            int saved_errno = ENOBUFS;
            metal_provided_buffers_clear(uring, pool);
            errno = saved_errno;
            return -1;
        }
        pool->tokens[buffer_id] = token;
        if (metal_provided_buffer_recycle(pool, buffer_id) < 0) {
            int saved_errno = errno;
            metal_provided_buffers_clear(uring, pool);
            errno = saved_errno;
            return -1;
        }
    }
    metal_provided_buffers_publish(pool);
    return 0;
}

