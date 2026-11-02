// SPDX-License-Identifier: AGPL-3.0-only
// Process-local executable-path adapter. All other readlink calls pass through.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
ssize_t readlink(const char *path, char *buf, size_t size) {
    const char *studio = getenv("H2D_STUDIO_EXECUTABLE");
    if (studio && *studio && strcmp(path, "/proc/self/exe") == 0) {
        if (!size) { errno = EINVAL; return -1; }
        size_t n = strlen(studio);
        if (n > size) n = size;
        memcpy(buf, studio, n); // readlink never appends a NUL byte.
        return (ssize_t)n;
    }
    ssize_t (*original)(const char *, char *, size_t) = dlsym(RTLD_NEXT, "readlink");
    if (!original) { errno = ENOSYS; return -1; }
    return original(path, buf, size);
}
